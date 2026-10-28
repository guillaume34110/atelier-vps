"""Bounded messages for the private, per-developer workstation relay."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import struct

MIB = 1024 ** 2
FRAME_LIMIT = 64 * 1024
SOURCE_LIMIT = 80 * MIB
RESULT_LIMIT = 288 * MIB
PRIVATE = {'.git', '.ssh', '.config', '.codex', '.claude', '.agents', '.aws',
           '.kube', '.env', '.npmrc', '.pypirc', '.netrc', 'auth.json', 'credentials.json'}


def result_path(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or '\0' in value:
        raise ValueError('invalid result path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or any(
        part.casefold() == 'claude.md' or part in PRIVATE
        or part.startswith(('.env.', 'id_ed25519', 'id_rsa'))
        or part.endswith(('.pem', '.key')) for part in path.parts
    ):
        raise ValueError('private or unsafe result path')
    return path


def request(value):
    fields = {'job_id', 'argv', 'paths', 'cpu', 'ram_mib', 'disk_mib', 'timeout',
              'source_bytes', 'source_sha256'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid task fields')
    if not isinstance(value['job_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['job_id']):
        raise ValueError('invalid task identity')
    argv, paths = value['argv'], value['paths']
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(arg, str) or '\0' in arg for arg in argv)
            or sum(len(arg.encode()) for arg in argv) > 32768
            or not isinstance(paths, list) or len(paths) > 64):
        raise ValueError('invalid task command or results')
    for name in paths:
        result_path(name)
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate result paths')
    for key, low, high in [('cpu', 1, 64), ('ram_mib', 256, 49152),
                           ('disk_mib', 64, 4096), ('timeout', 1, 3600),
                           ('source_bytes', 1, SOURCE_LIMIT)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('invalid task resource or source size')
    if not isinstance(value['source_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['source_sha256']):
        raise ValueError('invalid source digest')
    return value


def read_exact(stream, size):
    chunks = []
    while size:
        block = stream.read(min(size, 65536))
        if not block:
            raise EOFError('connection ended before the message was complete')
        chunks.append(block)
        size -= len(block)
    return b''.join(chunks)


def read_frame(stream):
    length = struct.unpack('!I', read_exact(stream, 4))[0]
    if not 1 <= length <= FRAME_LIMIT:
        raise ValueError('invalid message size')
    value = json.loads(read_exact(stream, length))
    if not isinstance(value, dict):
        raise ValueError('message must be an object')
    return value


def write_all(stream, data):
    data = memoryview(data)
    while data:
        written = stream.write(data)
        if written is None or written <= 0:
            raise ConnectionError('connection ended during a write')
        data = data[written:]


def write_frame(stream, value):
    data = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()
    if not 1 <= len(data) <= FRAME_LIMIT:
        raise ValueError('invalid message size')
    write_all(stream, struct.pack('!I', len(data)) + data)
    stream.flush()


def copy_exact(source, destination, size, limit):
    if type(size) is not int or not 0 <= size <= limit:
        raise ValueError('invalid transfer size')
    digest = hashlib.sha256()
    while size:
        chunk = source.read(min(size, 65536))
        if not chunk:
            raise EOFError('connection ended before the transfer was complete')
        write_all(destination, chunk)
        digest.update(chunk)
        size -= len(chunk)
    return digest.hexdigest()
