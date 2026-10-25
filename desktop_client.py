"""Use the system screen-sharing app over a private, authenticated SSH stream."""
import json
import math
import os
import re
import select
import signal
import socket
import subprocess
import sys

import client_config
import vps


def profile(server):
    values = client_config.active_connections()
    if server:
        value = client_config.connection(server)
    elif len(values) == 1:
        value = values[0]
    else:
        raise ValueError('select a server for the remote desktop')
    if not value.get('enabled', True):
        raise ValueError('this server connection is disabled')
    rc, state = vps.json_request(value['ssh_host'], 'status', timeout=25)
    vps.validate_status(state)
    if rc or value.get('server_id') and value['server_id'] != state.get('server_id'):
        raise PermissionError('the authenticated server identity does not match this connection')
    if value.get('scope') and value['scope'] != state.get('scope', {}).get('id'):
        raise PermissionError('the authenticated team member does not match this connection')
    if not state.get('capabilities', {}).get('desktop_sessions'):
        raise ValueError('this server needs the remote desktop update before it can open agents')
    return value


def request(server, action, ident=None, provider='chatgpt', ram_gib=None):
    data = {'action': action}
    if action == 'start':
        if provider not in {'chatgpt', 'claude'}:
            raise ValueError('select ChatGPT/Codex or Claude Desktop')
        data['provider'] = provider
        if ram_gib is not None:
            if not isinstance(ram_gib, (int, float)) or isinstance(ram_gib, bool) or not math.isfinite(ram_gib) or not 0 < ram_gib <= 5:
                raise ValueError('declare a finite positive known RAM peak within 5 GiB')
            data['ram_bytes'] = math.ceil(ram_gib * vps.GIB)
    if ident is not None:
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            raise ValueError('invalid desktop identity')
        data['id'] = ident
    value = profile(server)
    rc, result = vps.json_request(value['ssh_host'], 'desktop', data, timeout=45 if action == 'start' else 25)
    if rc:
        raise RuntimeError('the remote desktop request was refused; check its resources and private failure details')
    return value, result


def relay(connection, peer, ident):
    argv = ['ssh', *client_config.ssh_options(connection['ssh_host']), '-T', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=2', connection['ssh_host'], 'desktop']
    # The password and desktop traffic never enter command arguments or log files.
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            proc.stdin.write(json.dumps({'action': 'stream', 'id': ident}).encode() + b'\n'); proc.stdin.flush()
            while proc.poll() is None:
                readable, _, _ = select.select([peer, proc.stdout], [], [], 10)
                for source in readable:
                    part = peer.recv(65536) if source is peer else os.read(proc.stdout.fileno(), 65536)
                    if not part:
                        return
                    if source is peer:
                        proc.stdin.write(part); proc.stdin.flush()
                    else:
                        peer.sendall(part)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=3)


def open_desktop(server, ident):
    if sys.platform != 'darwin':
        raise ValueError('the built-in screen-sharing launcher currently requires macOS')
    def disconnect(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, disconnect)
    connection, ticket = request(server, 'ticket', ident)
    password = ticket.get('password', '')
    if ticket.get('id') != ident or not isinstance(password, str) or not re.fullmatch('[A-Za-z0-9]{8}', password):
        raise ValueError('invalid private desktop ticket')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(60)
        port = listener.getsockname()[1]
        url = 'vnc://:' + password + '@127.0.0.1:' + str(port)
        # NSWorkspace opens the private URL via stdin; no secret is passed to `open`.
        script = """ObjC.import('AppKit');ObjC.import('Foundation');
var input=$.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
var text=$.NSString.alloc.initWithDataEncoding(input,$.NSUTF8StringEncoding).js;
if(!$.NSWorkspace.sharedWorkspace.openURL($.NSURL.URLWithString(text)))throw Error('screen sharing unavailable');"""
        result = subprocess.run(['osascript', '-l', 'JavaScript', '-e', script], input=url.encode(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            raise RuntimeError('the system screen-sharing application could not be opened')
        print(json.dumps({'opened': True, 'server': connection['id'], 'id': ident}), flush=True)
        try:
            peer, _ = listener.accept()
        except socket.timeout:
            return
        with peer:
            relay(connection, peer, ident)
