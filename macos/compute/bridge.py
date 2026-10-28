"""Keep a private SSH listener idle until a server agent requests a Mac task."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import client_config
import mac_protocol as wire
import admission


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('private Mac worker directory required')


def lock_file(path, create=True):
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PermissionError('private Mac worker lock required')
    return os.fdopen(fd, 'a')


def node_id(project):
    path = project / 'node-id'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(uuid.uuid4().hex)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as source:
        info = os.fstat(source.fileno())
        value = source.read(33)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not re.fullmatch('[a-f0-9]{32}', value)):
        raise PermissionError('invalid private Mac worker identity')
    return value


def temporary_mib(request):
    return request['disk_mib'] + math.ceil(2 * request['source_bytes'] / wire.MIB) + 2 * 256 + 80


def storage_available(project, budget, temporary):
    guard = budget.parent / 'storage-guard.py'
    try:
        result = subprocess.run([sys.executable, str(guard), '--volume', str(project),
                                 '--require-gib', str(temporary / 1024)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_json(path, limit=4096):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise ValueError('invalid private task record')
        return json.load(source)


def valid_response(response, job_id):
    return (isinstance(response, dict) and set(response) == {'state', 'job_id', 'archive_bytes', 'archive_sha256'}
            and response['state'] == 'completed' and response['job_id'] == job_id
            and type(response['archive_bytes']) is int and 0 < response['archive_bytes'] <= wire.RESULT_LIMIT
            and isinstance(response['archive_sha256'], str)
            and re.fullmatch('[a-f0-9]{64}', response['archive_sha256']) is not None)


def cleanup(workspace, job_id, response):
    info = workspace.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('invalid task workspace')
    owner = read_json(workspace / 'owner.json')
    ack = read_json(workspace / 'ack.json')
    if (owner.get('job_id') != job_id or read_json(workspace / 'result-record.json') != response
            or ack != {'state': 'received', 'job_id': job_id, 'archive_sha256': response['archive_sha256']}):
        raise ValueError('task release evidence does not match')
    shutil.rmtree(workspace)


def cleanup_refused(workspace, job_id):
    info = workspace.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('invalid task workspace')
    if read_json(workspace / 'owner.json').get('job_id') != job_id:
        raise ValueError('task ownership does not match')
    if {p.name for p in workspace.iterdir()} != {'.task.lock', 'owner.json', 'stdout.log', 'tmp'}:
        raise ValueError('refused task contains work to preserve')
    if (workspace / 'tmp').is_symlink() or any((workspace / 'tmp').iterdir()):
        raise ValueError('refused task contains temporary data')
    shutil.rmtree(workspace)


def recover(reader, writer, workspace, job_id):
    if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
        raise ValueError('invalid recovery identity')
    owner = read_json(workspace / 'owner.json')
    if owner.get('job_id') != job_id or type(owner.get('guard_pid')) is not int or owner['guard_pid'] <= 1:
        raise ValueError('unknown preserved task')
    with lock_file(workspace / '.task.lock', create=False) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.kill(owner['guard_pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError('task is still being processed')
        response = read_json(workspace / 'result-record.json')
        if not valid_response(response, job_id):
            raise ValueError('invalid preserved task result')
        fd = os.open(workspace / 'result.tar', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as archive:
            info = os.fstat(archive.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_size != response['archive_bytes']):
                raise ValueError('preserved result verification failed')
            digest = hashlib.sha256()
            while chunk := archive.read(65536):
                digest.update(chunk)
            archive.seek(0)
            if digest.hexdigest() != response['archive_sha256']:
                raise ValueError('preserved result verification failed')
            wire.write_frame(writer, response)
            digest = wire.copy_exact(archive, writer, response['archive_bytes'], wire.RESULT_LIMIT)
            if digest != response['archive_sha256']:
                raise ValueError('preserved result changed during transfer')
        acknowledgement = wire.read_frame(reader)
        if acknowledgement != {'state': 'received', 'job_id': job_id,
                               'archive_sha256': response['archive_sha256']}:
            raise ValueError('result acknowledgement mismatch')
        ack_path = workspace / 'ack.json'
        try:
            fd = os.open(ack_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(acknowledgement, output)
        except FileExistsError:
            if read_json(ack_path) != acknowledgement:
                raise ValueError('preserved acknowledgement does not match')
        cleanup(workspace, job_id, response)
        wire.write_frame(writer, {'state': 'released', 'job_id': job_id})


def transfer_result(reader, writer, control, request, process, workspace):
    response = wire.read_frame(control)
    if not valid_response(response, request['job_id']):
        raise ValueError('invalid task response')
    wire.write_frame(writer, response)
    wire.copy_exact(control, writer, response['archive_bytes'], wire.RESULT_LIMIT)
    acknowledgement = wire.read_frame(reader)
    if acknowledgement != {'state': 'received', 'job_id': request['job_id'],
                           'archive_sha256': response['archive_sha256']}:
        raise ValueError('result acknowledgement mismatch')
    wire.write_frame(control, acknowledgement)
    output, _ = process.communicate(timeout=30)
    try:
        guard = json.loads(output)
    except (TypeError, ValueError):
        guard = {}
    if process.returncode != 0 or guard.get('exit_code') != 0 or guard.get('budget_stop') is not None:
        raise RuntimeError('task output preserved after budget or execution failure')
    cleanup(workspace, request['job_id'], response)
    wire.write_frame(writer, {'state': 'released', 'job_id': request['job_id']})


def handle(reader, writer, project, assets, budget):
    request = wire.read_frame(reader)
    with lock_file(project / '.worker.lock') as lock:
        if set(request) == {'recover'}:
            if not isinstance(request['recover'], str) or not re.fullmatch('[a-f0-9]{32}', request['recover']):
                raise ValueError('invalid recovery identity')
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                wire.write_frame(writer, {'state': 'refused', 'reason': 'Mac busy'})
                return
        workspace = project / 'output/task-tmp'
        if (project / 'output').is_symlink() or workspace.is_symlink():
            wire.write_frame(writer, {'state': 'refused', 'reason': 'unsafe Mac workspace'})
            return
        if set(request) == {'recover'}:
            try:
                recover(reader, writer, workspace, request['recover'])
            except (OSError, ValueError, RuntimeError):
                wire.write_frame(writer, {'state': 'refused', 'reason': 'preserved result unavailable'})
            return
        request = wire.request(request)
        if workspace.exists() and any(workspace.iterdir()):
            wire.write_frame(writer, {'state': 'refused', 'reason': 'previous task preserved; recover it first'})
            return
        allowed, reason = admission.check(request)
        temp_mib = temporary_mib(request)
        if not allowed or temp_mib > 5 * 1024:
            wire.write_frame(writer, {'state': 'refused', 'reason': reason or 'temporary budget'})
            return
        if not storage_available(project, budget, temp_mib):
            wire.write_frame(writer, {'state': 'refused', 'reason': 'disk reserve'})
            return
        control_path = project / ('s-' + uuid.uuid4().hex[:10])
        if len(os.fsencode(control_path)) >= 100:
            raise ValueError('Mac worker path is too long for a private socket')
        control_socket = None
        process = None
        refused = False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(control_path))
            identity = control_path.lstat()
            os.chmod(control_path, 0o600)
            listener.listen(1)
            listener.settimeout(20)
            try:
                command = [sys.executable, str(budget), '--temp-gib', str(temp_mib / 1024), 'run',
                           str(project), '--', sys.executable, str(Path(__file__).parent / 'task.py'),
                           str(control_path), *(assets[key] for key in ('kernel', 'initramfs', 'modules',
                                                                       'tools', 'vm', 'mke2fs'))]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT)
                control_socket, _ = listener.accept()
                control_socket.settimeout(3700)
                with control_socket.makefile('rwb', buffering=0) as control:
                    if wire.read_frame(control) != {'state': 'ready'}:
                        raise RuntimeError('Mac worker did not become ready')
                    wire.write_frame(control, request)
                    admission_response = wire.read_frame(control)
                    if admission_response.get('state') != 'accepted':
                        refused = admission_response.get('state') == 'refused'
                        wire.write_frame(writer, admission_response)
                        return
                    wire.write_frame(writer, {'state': 'accepted'})
                    wire.copy_exact(reader, control, request['source_bytes'], wire.SOURCE_LIMIT)
                    transfer_result(reader, writer, control, request, process, workspace)
            finally:
                if control_socket:
                    control_socket.close()
                try:
                    current = control_path.lstat()
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        control_path.unlink()
                except FileNotFoundError:
                    pass
                if process and process.poll() is None:
                    process.communicate()
                if refused and process and process.returncode == 1:
                    cleanup_refused(workspace, request['job_id'])


class PipeReader:
    def __init__(self, stream, timeout):
        self.fd, self.timeout = stream.fileno(), timeout

    def read(self, size):
        if not select.select([self.fd], [], [], self.timeout)[0]:
            raise TimeoutError('private relay timed out')
        return os.read(self.fd, size)


def permitted(alias, scope):
    return any(row['ssh_host'] == alias and row.get('scope') == scope
               for row in client_config.active_connections())


def authenticated(alias, scope, hello):
    if not isinstance(hello, dict) or set(hello) != {'scope', 'server_id'} or hello['scope'] != scope:
        return False
    if hello['server_id'] is not None and (not isinstance(hello['server_id'], str)
            or not re.fullmatch('[a-f0-9]{64}', hello['server_id'])):
        return False
    return any(row['ssh_host'] == alias and row.get('scope') == scope
               and (not row.get('server_id') or row['server_id'] == hello['server_id'])
               for row in client_config.active_connections())


def listen(alias, identity, project, assets, budget, scope, stop):
    while not stop.is_set():
        try:
            if not permitted(alias, scope):
                return
            command = ['ssh', *client_config.ssh_options(alias), '-T', '-o', 'BatchMode=yes',
                       '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', alias, 'mac']
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, bufsize=0) as process:
                process.stdin.write(json.dumps({'action': 'listen', 'node_id': identity}).encode() + b'\n')
                reader = PipeReader(process.stdout, 70)
                if wire.read_exact(reader, 1) != b'R':
                    raise ConnectionError('private relay unavailable')
                hello = wire.read_frame(reader)
                if not authenticated(alias, scope, hello):
                    raise PermissionError('worker identity changed')
                event = wire.read_exact(reader, 1)
                if event == b'J':
                    if authenticated(alias, scope, hello):
                        handle(reader, process.stdin, project, assets, budget)
                    else:
                        wire.write_frame(process.stdin, {'state': 'refused', 'reason': 'connection disabled'})
                elif event != b'T':
                    raise ConnectionError('private relay interrupted')
        except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError):
            stop.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.home() / '.atelier-vps/mac-worker')
    parser.add_argument('--budget', type=Path, default=Path.home() / '.local/bin/workspace-budget.py')
    parser.add_argument('--scope', default='owner', help='only connect this Mac to this developer scope')
    for name in ('kernel', 'initramfs', 'modules', 'tools', 'vm', 'mke2fs'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if not re.fullmatch('[a-z][a-z0-9-]{0,31}', args.scope):
        raise ValueError('invalid developer scope')
    private_directory(args.project)
    identity = node_id(args.project)
    assets = {name: str(Path(getattr(args, name)).resolve(strict=True))
              for name in ('kernel', 'initramfs', 'modules', 'tools', 'vm', 'mke2fs')}
    budget = args.budget.resolve(strict=True)
    stop = threading.Event()
    threads = {}
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                rows = [row for row in client_config.active_connections() if row.get('scope') == args.scope]
            except (OSError, ValueError):
                rows = []
            allowed = {row['ssh_host'] for row in rows}
            for alias in list(threads):
                if alias not in allowed and not threads[alias].is_alive():
                    del threads[alias]
            for row in rows:
                alias = row['ssh_host']
                if alias not in threads or not threads[alias].is_alive():
                    threads[alias] = threading.Thread(target=listen, args=(alias, identity, args.project,
                                                                          assets, budget, args.scope, stop), daemon=True)
                    threads[alias].start()
            stop.wait(10)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for thread in threads.values():
            thread.join()


if __name__ == '__main__':
    main()
