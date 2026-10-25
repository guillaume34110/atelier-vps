"""Private one-use invitations; public keys enroll without exporting private keys."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
import uuid

import client_config as config
from ops.team import public_key

PREFIX = 'atelier-vps://'
LIMIT = 8192


def encode(kind, value):
    raw = json.dumps({'version': 1, **value}, separators=(',', ':'), ensure_ascii=False).encode()
    result = PREFIX + kind + '#' + base64.urlsafe_b64encode(raw).decode().rstrip('=')
    if len(result) > LIMIT:
        raise ValueError('connection link exceeds 8 KiB')
    return result


def decode(link, kind, fields):
    prefix = PREFIX + kind + '#'
    if not isinstance(link, str) or len(link) > LIMIT or not link.startswith(prefix):
        raise ValueError('paste a valid Atelier VPS ' + kind + ' link')
    encoded = link[len(prefix):]
    if not re.fullmatch('[A-Za-z0-9_-]+', encoded):
        raise ValueError('invalid connection link encoding')
    try:
        value = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError):
        raise ValueError('invalid connection link') from None
    if not isinstance(value, dict) or set(value) != {'version', *fields} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('unsupported connection link fields or version')
    return value


def label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('use a short single-line name')
    return value


def fingerprint(key):
    key = public_key(key)
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(key.split()[1])).digest()).decode().rstrip('=')


def endpoint(hostname, port):
    if not isinstance(hostname, str) or len(hostname) > 253:
        raise ValueError('invalid server address')
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if not all(re.fullmatch('[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in hostname.split('.')):
            raise ValueError('invalid server address') from None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid SSH port')
    return hostname, port


def private_file(path, text):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        out.write(text)


def access_request(name):
    name = label(name)
    directory = config.config_path().parent / 'keys'
    if directory.is_symlink():
        raise ValueError('device key directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = directory / 'id_ed25519_atelier_device'
    pub = key.with_suffix('.pub')
    if key.exists() or pub.exists():
        if key.is_symlink() or pub.is_symlink() or not key.is_file() or not pub.is_file() or key.stat().st_mode & 0o077:
            raise ValueError('preserve the existing device key; restore its public sidecar and 0600 permissions')
    else:
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'atelier-vps-device', '-f', str(key)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        key.chmod(0o600); pub.chmod(0o600)
    if pub.stat().st_size>256:raise ValueError('invalid device public key')
    value = public_key(pub.read_text())
    return {'request': encode('request', {'name': name, 'public_key': value}),
            'public_key': value, 'fingerprint': fingerprint(value), 'private_key_transferred': False}


def read_request(link):
    value = decode(link, 'request', {'name', 'public_key'})
    label(value['name']); value['public_key'] = public_key(value['public_key'])
    return value


def bounded_output(argv, limit=65536, timeout=20, capture_errors=False, input=None, env=None):
    """Untrusted onboarding responses stay bounded in memory and time."""
    with subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, env=env,
                          stdout=subprocess.DEVNULL if capture_errors else subprocess.PIPE,
                          stderr=subprocess.PIPE if capture_errors else subprocess.DEVNULL) as proc:
        end = time.monotonic() + timeout
        result = bytearray()
        stream = proc.stderr if capture_errors else proc.stdout
        try:
            if input is not None:
                proc.stdin.write(input); proc.stdin.close()
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
                    raise TimeoutError('server connection timed out')
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > limit:
                    raise ValueError('server response exceeds its bound')
            proc.wait(timeout=max(.1, end - time.monotonic()))
            if proc.returncode:
                raise RuntimeError('SSH authentication failed; verify the invitation and authorized device')
            return bytes(result)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=2)


def status(alias, options=()):
    from vps import validate_status
    config.valid_alias(alias)
    raw = bounded_output(['ssh', *options, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                       '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1', alias, 'status'])
    value = json.loads(raw)
    return validate_status(value)


def add_alias(identifier, name, team, alias, admin_alias=None):
    row = config.validate_connection({'id': identifier, 'name': name, 'team': team, 'ssh_host': alias,
                                     **({'admin_ssh_host': admin_alias} if admin_alias else {})})
    options=config.ssh_options(alias)
    details=bounded_output(['ssh',*options,'-G',alias]).decode().splitlines()
    details=dict(line.split(' ',1) for line in details if ' ' in line)
    if not re.fullmatch(r'(usevps|avps-(?!admin$)[a-z][a-z0-9-]{0,15})', details.get('user', '')):
        raise PermissionError('ordinary work requires a restricted developer SSH account')
    state = status(alias, [*options, '-S', 'none'])
    row['scope'] = state.get('scope', {}).get('id', 'owner')
    if state.get('server_id'):
        row['server_id'] = state['server_id']
    if admin_alias:
        import vps
        rc,admin=vps.json_request(admin_alias,'settings',{'action':'get'})
        if rc or not state.get('server_id') or admin.get('server_id')!=state['server_id']:
            raise PermissionError('update the worker and administrator gateway before binding this administrator')
    return config.save_connection(row)


def invitation(request, member, connection_id):
    """The administrator must have already applied this member's key and quotas."""
    import vps
    device = read_request(request)
    connection = config.connection(connection_id)
    admin = connection.get('admin_ssh_host')
    if not admin:
        raise PermissionError('this server has no separately authenticated administrator alias')
    state = vps.admin_request('get', connection_id)
    users = [u for u in state['policy']['users'] if u['id'] == member and u.get('enabled', True)]
    authorized = users[0].get('public_key') if len(users) == 1 else None
    if member == 'owner' and not authorized:
        authorized = state.get('owner_public_key')
    if state.get('pending') or len(users) != 1 or not authorized or public_key(authorized) != device['public_key']:
        raise ValueError('apply this enabled member and exact public key before creating the link')
    tenant = state['tenants'][member]
    if not tenant.get('enabled'):
        raise ValueError('member is disabled')
    hostname, port, key = trusted_endpoint(connection)
    payload = {'name': connection['name'], 'team': connection['team'], 'hostname': hostname, 'port': port,
               'user': tenant['ssh_user'], 'scope': member, 'host_key': key,
               'device_fingerprint': fingerprint(device['public_key'])}
    return {'invitation': encode('connect', payload), 'member': member, 'grants_access': False,
            'note': 'Only the already authorized local device key grants access; revocation stays on the server.'}


def trusted_endpoint(connection):
    options = config.ssh_options(connection['ssh_host'])
    resolved = bounded_output(['ssh', *options, '-G', connection['ssh_host']]).decode().splitlines()
    settings = dict(line.split(' ', 1) for line in resolved if ' ' in line)
    hostname, port = endpoint(settings['hostname'], int(settings['port']))
    if settings.get('proxyjump', 'none') != 'none' or settings.get('proxycommand', 'none') != 'none':
        raise ValueError('direct invitations require a reachable SSH endpoint; keep private jump access as a configured alias')
    scan = bounded_output(['ssh-keyscan', '-T', '5', '-p', str(port), '-t', 'ed25519', hostname], limit=8192)
    keys = {public_key(' '.join(line.split()[1:3])) for line in scan.decode().splitlines() if line and not line.startswith('#')}
    if len(keys) != 1:
        raise ValueError('server must provide one stable ed25519 host key')
    key = keys.pop()
    # SSH checks the existing trusted host; its diagnostic confirms the scanned key.
    argv = ['ssh', *options, '-S', 'none', '-T', '-vv', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ConnectTimeout=5', connection['ssh_host'], 'status']
    debug = bounded_output(argv, capture_errors=True)
    pins = re.findall(rb'Server host key: ssh-ed25519 (SHA256:[A-Za-z0-9+/]+)', debug)
    if not pins or pins[-1].decode() != fingerprint(key):
        raise ValueError('host key does not match the existing trusted SSH connection')
    return hostname, port, key


def accept_connect(link, expected_server=None):
    value = decode(link, 'connect', {'name', 'team', 'hostname', 'port', 'user', 'scope', 'host_key', 'device_fingerprint'})
    label(value['name']); label(value['team']); endpoint(value['hostname'], value['port'])
    key = public_key(value['host_key'])
    if not isinstance(value['device_fingerprint'], str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value['device_fingerprint']):
        raise ValueError('invalid device public-key fingerprint')
    if (not isinstance(value['user'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', value['user'])
            or value['user'] in {'root', 'avps-admin', 'admin'}):
        raise ValueError('invitation must use a restricted developer account')
    if not isinstance(value['scope'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', value['scope']):
        raise ValueError('invalid developer workspace')
    keys = config.config_path().parent / 'keys'
    local = []
    for pub in list(keys.glob('id_ed25519_*.pub'))[:32]:
        if pub.is_symlink() or not pub.is_file() or pub.stat().st_size > 256:
            continue
        if fingerprint(pub.read_text()) == value['device_fingerprint']:
            identity = pub.with_suffix('')
            if identity.is_symlink() or not identity.is_file() or identity.stat().st_mode & 0o077:
                raise ValueError('local device key permissions must be 0600')
            local.append(identity)
    if len(local) != 1:
        raise ValueError('this invitation belongs to another device; send your public access request to the administrator')
    identifier = 's-' + hashlib.sha256((key + ':' + value['scope']).encode()).hexdigest()[:16]
    alias = 'atelier-' + identifier
    parent = config.config_path().parent
    registered = config.connections() if (parent / 'connections.json').exists() or config.config_path().exists() else []
    staging = parent / ('.join-' + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    def quoted(path):
        text = str(path)
        if '\n' in text or '\r' in text:
            raise ValueError('unsupported local configuration path')
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    try:
        known = staging / 'known_hosts'
        private_file(known, alias + ' ' + key + '\n')
        def ssh_text(known_path):
            return (f'Host {alias}\n  HostName {value["hostname"]}\n  Port {value["port"]}\n  User {value["user"]}\n'
                    f'  IdentityFile {quoted(local[0])}\n  IdentitiesOnly yes\n  BatchMode yes\n'
                    f'  ForwardAgent no\n  ClearAllForwardings yes\n  RequestTTY no\n'
                    f'  StrictHostKeyChecking yes\n  HostKeyAlgorithms ssh-ed25519\n  HostKeyAlias {alias}\n'
                    f'  UserKnownHostsFile {quoted(known_path)}\n  GlobalKnownHostsFile /dev/null\n')
        candidate = staging / 'ssh.conf'
        private_file(candidate, ssh_text(known))
        state = status(alias, ['-F', str(candidate), '-S', 'none'])
        if state.get('scope', {}).get('id', 'owner') != value['scope']:
            raise ValueError('authenticated workspace does not match the invitation')
        if expected_server and state.get('server_id') != expected_server:
            raise ValueError('authenticated server does not match the invitation')
        if state.get('server_id'):
            identifier = 's-' + hashlib.sha256((state['server_id'] + ':' + value['scope']).encode()).hexdigest()[:16]
            alias = 'atelier-' + identifier
        if not any(row['id'] == identifier for row in registered) and len(registered) >= config.MAX_CONNECTIONS:
            raise ValueError('connection limit reached')
        destination = config.connection_dir(identifier)
        if destination.is_symlink():
            raise ValueError('connection directory must not be a symlink')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = staging / 'final.conf'
        private_file(final, ssh_text(destination / 'known_hosts'))
        final_known = staging / 'final-known-hosts'
        private_file(final_known, alias + ' ' + key + '\n')
        for name, source in [('known_hosts', final_known), ('ssh.conf', final)]:
            target = destination / name
            if target.is_symlink():
                raise ValueError('connection file must not be a symlink')
            os.replace(source, target)
        profile = {'id': identifier, 'name': value['name'], 'team': value['team'], 'ssh_host': alias,
                   'managed': True, 'scope': value['scope']}
        existing = [row for row in registered if row['id'] == identifier]
        if existing and existing[0].get('admin_ssh_host'):
            profile['admin_ssh_host'] = existing[0]['admin_ssh_host']
        if state.get('server_id'):
            profile['server_id'] = state['server_id']
        return config.save_connection(profile)
    finally:
        shutil.rmtree(staging)


def create_invitation(member, connection_id, expected_revision):
    """Return a private link to the caller's private UI or exclusive 0600 file."""
    import vps
    connection = config.connection(connection_id)
    hostname, port, key = trusted_endpoint(connection)
    value = vps.admin_request('invite', connection_id, member=member, expected_revision=expected_revision)
    if (not re.fullmatch('avpi-[a-f0-9]{24}', value.get('principal', ''))
            or not re.fullmatch('[A-Za-z0-9_-]{43}', value.get('password', ''))
            or not re.fullmatch('[a-f0-9]{64}', value.get('server_id', ''))
            or value.get('member') != member or value.get('user') != ('usevps' if member == 'owner' else 'avps-' + member)
            or type(value.get('expires')) is not int or not time.time() < value['expires'] <= time.time() + 1900):
        raise ValueError('invalid invitation response')
    payload = {'name': connection['name'], 'team': connection['team'], 'hostname': hostname, 'port': port,
               'host_key': key, **value}
    return encode('join', payload), {'member': member, 'server': connection_id, 'expires': value['expires'], 'one_device': True}


def deliver_invitation(member, connection_id, expected_revision, clipboard=False):
    if clipboard and sys.platform != 'darwin':
        raise ValueError('clipboard delivery is available in the Mac widget; use the private file on this machine')
    if connection_id is None:
        rows = config.active_connections()
        if len(rows) != 1:
            raise ValueError('select the server to administer')
        connection_id = rows[0]['id']
    link, result = create_invitation(member, connection_id, expected_revision)
    if clipboard:
        subprocess.run(['/usr/bin/pbcopy'], input=link.encode(), check=True, timeout=5)
        return {**result, 'copied_to_clipboard': True}
    directory = config.config_path().parent / 'invitations'
    if directory.is_symlink():
        raise ValueError('private invitation directory must not be a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / ('invitation-' + uuid.uuid4().hex + '.txt')
    private_file(target, link + '\n')
    return {**result, 'private_file': str(target)}


def accept(link):
    if not isinstance(link, str) or not link.startswith('atelier-vps://join#'):
        return accept_connect(link)
    value = decode(link, 'join', {'name', 'team', 'hostname', 'port', 'host_key', 'principal',
                                  'password', 'expires', 'member', 'user', 'server_id'})
    label(value['name']); label(value['team']); endpoint(value['hostname'], value['port'])
    key = public_key(value['host_key'])
    if (not isinstance(value['principal'], str) or not re.fullmatch('avpi-[a-f0-9]{24}', value['principal'])
            or not isinstance(value['password'], str) or not re.fullmatch('[A-Za-z0-9_-]{43}', value['password'])
            or not isinstance(value['server_id'], str) or not re.fullmatch('[a-f0-9]{64}', value['server_id'])
            or not isinstance(value['member'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,15}', value['member'])
            or value['member'] in {'admin', 'root', 'usevps'}
            or value['user'] != ('usevps' if value['member'] == 'owner' else 'avps-' + value['member'])
            or type(value['expires']) is not int or not time.time() < value['expires'] <= time.time() + 1900):
        raise ValueError('invalid or expired invitation')
    device = access_request('Atelier VPS')
    base = {k: value[k] for k in ['name', 'team', 'hostname', 'port', 'user', 'host_key']}
    base.update(scope=value['member'], device_fingerprint=device['fingerprint'])
    descriptor = encode('connect', base)
    parent = config.config_path().parent
    staging = parent / ('.enroll-' + uuid.uuid4().hex); staging.mkdir(mode=0o700)
    try:
        known = staging / 'known_hosts'; askpass = staging / 'askpass'
        private_file(known, 'atelier-enroll ' + key + '\n')
        private_file(askpass, '#!/bin/sh\nprintf "%s\\n" "$ATELIER_VPS_ENROLL_PASSWORD"\n')
        askpass.chmod(0o700)
        env = {**os.environ, 'SSH_ASKPASS': str(askpass), 'SSH_ASKPASS_REQUIRE': 'force',
               'DISPLAY': 'atelier-enrollment', 'ATELIER_VPS_ENROLL_PASSWORD': value['password']}
        argv = ['ssh', '-F', '/dev/null', '-S', 'none', '-T', '-p', str(value['port']),
                '-l', value['principal'], '-o', 'HostName=' + value['hostname'],
                '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'HostKeyAlias=atelier-enroll', '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlgorithms=ssh-ed25519',
                '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password',
                '-o', 'NumberOfPasswordPrompts=1', '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR',
                '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', 'atelier-enroll', 'enroll']
        try:
            response = json.loads(bounded_output(argv, input=json.dumps({'public_key': device['public_key']}).encode() + b'\n', env=env))
        except (RuntimeError, TimeoutError):
            # A lost receipt can follow successful enrollment. Only the same local key can resume.
            return accept_connect(descriptor, expected_server=value['server_id'])
        if (response.get('enrolled') is not True or response.get('server_id') != value['server_id']
                or response.get('member') != value['member'] or response.get('public_key') != device['public_key']):
            raise ValueError('enrollment response does not match this device and server')
        return accept_connect(descriptor, expected_server=value['server_id'])
    finally:
        shutil.rmtree(staging)
