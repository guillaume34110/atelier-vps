"""A private, owner-scoped stdio relay for an on-demand workstation worker."""
import fcntl
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time

import resources

WAIT_SECONDS = 45
COPY_LIMIT = 64 * 1024
MAX_INPUT = 82 * 1024 ** 2
MAX_OUTPUT = 304 * 1024 ** 2
MAX_SESSION = 3660


def _directory(root, create=False):
    base = Path(root) / '.mac-relay'
    if create:
        base.mkdir(mode=0o700, exist_ok=True)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('private relay directory required')
    return base


def _lock(base):
    fd = os.open(base / '.listener.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise PermissionError('invalid relay lock')
    return os.fdopen(fd, 'a')


def _peer_user(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _write(fd, data):
    deadline = time.monotonic() + 10
    while data:
        if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
            raise TimeoutError('relay write timed out')
        try:
            written = os.write(fd, data)
        except BlockingIOError:
            continue
        if written <= 0:
            raise ConnectionError('relay write failed')
        data = data[written:]


def _validate(request):
    if set(request) != {'action', 'node_id'} or request['action'] != 'listen':
        raise ValueError('invalid Mac relay request')
    if not isinstance(request['node_id'], str) or not re.fullmatch('[a-f0-9]{32}', request['node_id']):
        raise ValueError('invalid Mac identity')


def listen(root, request, incoming, outgoing, wait=WAIT_SECONDS, scope='owner', server_id=None):
    """Offer one owner-only connection; no network port or unbounded queue."""
    _validate(request)
    if (not isinstance(scope, str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', scope)
            or server_id is not None and (not isinstance(server_id, str) or not re.fullmatch('[a-f0-9]{64}', server_id))):
        raise ValueError('invalid authenticated worker identity')
    if not 1 <= wait <= WAIT_SECONDS:
        raise ValueError('invalid relay wait')
    base = _directory(root, create=True)
    path = base / 'worker.sock'
    with _lock(base) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError('invalid stale relay socket')
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o600)
            socket_identity = path.lstat()
            listener.listen(1)
            os.set_blocking(outgoing, False)
            status_identity = None
            try:
                resources.write_json(base / 'status.json', {'node_id': request['node_id'],
                    'updated': time.time(), 'busy': False})
                status_identity = (base / 'status.json').lstat()
                hello = json.dumps({'scope': scope, 'server_id': server_id}, separators=(',', ':')).encode()
                _write(outgoing, b'R' + struct.pack('!I', len(hello)) + hello)
                ready, _, _ = select.select([listener, incoming], [], [], wait)
                if not ready:
                    _write(outgoing, b'T')
                    return
                if incoming in ready:
                    if os.read(incoming, 1):
                        raise ValueError('unexpected idle relay data')
                    return
                with listener.accept()[0] as peer:
                    if _peer_user(peer) != os.getuid():
                        raise PermissionError('relay is private to this developer')
                    peer.settimeout(10)
                    listener.close()
                    current_socket = path.lstat()
                    if (current_socket.st_dev, current_socket.st_ino) == (socket_identity.st_dev, socket_identity.st_ino):
                        path.unlink()
                    with (Path(root) / '.queue.lock').open('a') as lease:
                        fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        resources.write_json(base / 'status.json', {'node_id': request['node_id'],
                            'updated': time.time(), 'busy': True})
                        status_identity = (base / 'status.json').lstat()
                        _write(outgoing, b'J')
                        deadline = time.monotonic() + MAX_SESSION
                        next_update = time.monotonic() + 30
                        sent = received = 0
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError('relay session expired')
                            if time.monotonic() >= next_update:
                                resources.write_json(base / 'status.json', {'node_id': request['node_id'],
                                    'updated': time.time(), 'busy': True})
                                status_identity = (base / 'status.json').lstat()
                                next_update = time.monotonic() + 30
                            readable, _, _ = select.select([peer, incoming], [], [], min(30, remaining))
                            if not readable:
                                continue
                            for source in readable:
                                chunk = peer.recv(COPY_LIMIT) if source is peer else os.read(incoming, COPY_LIMIT)
                                if not chunk:
                                    return
                                if source is peer:
                                    sent += len(chunk)
                                    if sent > MAX_INPUT:
                                        raise ValueError('relay input limit exceeded')
                                    _write(outgoing, chunk)
                                else:
                                    received += len(chunk)
                                    if received > MAX_OUTPUT:
                                        raise ValueError('relay output limit exceeded')
                                    peer.sendall(chunk)
            finally:
                for candidate, identity in [(path, socket_identity), (base / 'status.json', status_identity)]:
                    try:
                        current = candidate.lstat()
                        if identity and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                            candidate.unlink()
                    except FileNotFoundError:
                        pass


def status(root):
    """Return only fresh presence; capacity is checked again on the Mac."""
    try:
        base = _directory(root)
        fd = os.open(base / 'status.json', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_size > 1024:
                return None
            value = json.load(source)
        if (not isinstance(value['node_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['node_id'])
                or type(value['updated']) not in (int, float) or type(value['busy']) is not bool
                or not 0 <= time.time() - value['updated'] <= 60):
            return None
        if not value['busy']:
            info = (base / 'worker.sock').lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                return None
        return {'node_id': value['node_id'], 'available': not value['busy']}
    except (OSError, ValueError, TypeError, KeyError):
        return None
