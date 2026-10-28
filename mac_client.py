#!/usr/bin/env python3
"""Submit one private, isolated task to the owner's available Mac."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tarfile
import tempfile
import time
import uuid

import mac_protocol as wire
import mac_relay
import vps


class TaskRefused(RuntimeError):
    """The Mac declined the request before execution began."""


def sha256_file(file):
    digest = hashlib.sha256()
    while chunk := file.read(65536):
        digest.update(chunk)
    return digest.hexdigest()


def source_archive(project, archive):
    count = 0
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for file, name in vps.inputs(project):
            count += 1
            if count > 10000:
                raise ValueError('source file count limit')
            tar.add(file, arcname=name, recursive=False)
    size = archive.tell()
    if not 1 <= size <= wire.SOURCE_LIMIT:
        raise ValueError('source archive exceeds limit')
    archive.seek(0)
    digest = sha256_file(archive)
    archive.seek(0)
    return size, digest


def unpack_results(archive, output):
    received = {}
    with tarfile.open(fileobj=archive, mode='r:') as tar:
        for member in tar:
            path = wire.result_path(member.name)
            if (not member.isfile() or member.size < 0 or len(received) >= 10002
                    or str(path) in received or path.parts[0] not in {'receipt.json', 'stdout.log', 'results'}
                    or path.parts[0] != 'results' and len(path.parts) != 1):
                raise ValueError('invalid result archive member')
            received[str(path)] = (member.size, member)
        if sum(size for size, _ in received.values()) > wire.RESULT_LIMIT:
            raise ValueError('result archive exceeds limit')
        if 'receipt.json' not in received or 'stdout.log' not in received:
            raise ValueError('incomplete result archive')
        if received['receipt.json'][0] > 2 * wire.MIB or received['stdout.log'][0] > 20 * wire.MIB:
            raise ValueError('task metadata exceeds limit')
        for name, (size, member) in received.items():
            destination = output.joinpath(*wire.result_path(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, destination.open('xb') as target:
                digest = wire.copy_exact(source, target, size, wire.RESULT_LIMIT)
            received[name] = (size, digest)
    receipt = json.loads((output / 'receipt.json').read_text())
    if (not isinstance(receipt, dict) or receipt.get('status') not in {'passed', 'failed'}
            or type(receipt.get('returncode')) is not int or not isinstance(receipt.get('results'), dict)):
        raise ValueError('invalid task receipt')
    expected = {'receipt.json', 'stdout.log'}
    for name, info in receipt['results'].items():
        wire.result_path(name)
        key = 'results/' + name
        if (not isinstance(info, dict) or set(info) != {'bytes', 'sha256'}
                or type(info['bytes']) is not int or info['bytes'] < 0
                or not isinstance(info['sha256'], str)):
            raise ValueError('invalid result manifest')
        if received.get(key) != (info['bytes'], info['sha256']):
            raise ValueError('result digest mismatch')
        expected.add(key)
    if set(received) != expected:
        raise ValueError('undeclared results in archive')
    return receipt


def receive_result(reader, writer, job_id, output):
    response = wire.read_frame(reader)
    if (response.get('state') != 'completed' or response.get('job_id') != job_id
            or type(response.get('archive_bytes')) is not int
            or not isinstance(response.get('archive_sha256'), str)):
        raise RuntimeError('Mac task result unavailable; do not automatically retry')
    with tempfile.TemporaryFile() as result:
        digest = wire.copy_exact(reader, result, response['archive_bytes'], wire.RESULT_LIMIT)
        if digest != response['archive_sha256']:
            raise ValueError('result archive digest mismatch')
        result.seek(0)
        receipt = unpack_results(result, output)
    wire.write_frame(writer, {'state': 'received', 'job_id': job_id, 'archive_sha256': digest})
    final = wire.read_frame(reader)
    if final != {'state': 'released', 'job_id': job_id}:
        raise RuntimeError('result verified but Mac cleanup is uncertain; do not retry')
    return receipt


def execute(connect, project, output, request):
    wire.request({**request, 'source_bytes': 1, 'source_sha256': '0' * 64})
    with tempfile.TemporaryFile() as archive:
        request['source_bytes'], request['source_sha256'] = source_archive(project, archive)
        try:
            connection = connect()
        except OSError as error:
            raise TaskRefused('private Mac relay unavailable') from error
        with connection, connection.makefile('rb', buffering=0) as reader, \
                connection.makefile('wb', buffering=0) as writer:
            wire.write_frame(writer, request)
            response = wire.read_frame(reader)
            if response.get('state') != 'accepted':
                if response.get('state') == 'refused':
                    raise TaskRefused('Mac task refused: ' + str(response.get('reason', 'unavailable'))[:256])
                raise RuntimeError('Mac admission outcome unknown')
            wire.copy_exact(archive, writer, request['source_bytes'], wire.SOURCE_LIMIT)
            return receive_result(reader, writer, request['job_id'], output)


def recover(connection, job_id, output):
    reader = connection.makefile('rb', buffering=0)
    writer = connection.makefile('wb', buffering=0)
    try:
        wire.write_frame(writer, {'recover': job_id})
        return receive_result(reader, writer, job_id, output)
    finally:
        reader.close()
        writer.close()


def connect_relay(relay, timeout):
    info = relay.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('private Mac relay unavailable')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout + 180)
        connection.connect(str(relay))
        return connection
    except Exception:
        connection.close()
        raise


def wait_for_relay(root, timeout):
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('recovery wait must be between 1 and 3600 seconds')
    deadline = time.monotonic() + timeout
    while True:
        status = mac_relay.status(root)
        if status and status['available']:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Mac remains unavailable; the task is still preserved')
        time.sleep(min(2, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--result', action='append', default=[])
    parser.add_argument('--cpu', type=int)
    parser.add_argument('--ram-mib', type=int)
    parser.add_argument('--disk-mib', type=int)
    parser.add_argument('--timeout', default=600, type=int)
    parser.add_argument('--recover')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.status:
        print(json.dumps(mac_relay.status(Path(os.environ.get('USEVPS_ROOT', '/workspace')))
                         or {'available': False}))
        return 0
    if args.output is None:
        raise ValueError('a new output directory is required')
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    root = Path.cwd().resolve()
    output = args.output
    if output.is_absolute() or '..' in output.parts or not output.parts:
        raise ValueError('output must be a new relative directory')
    target = (root / output).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError('output must stay inside the current task')
    if not args.recover and (args.source is None or None in (args.cpu, args.ram_mib, args.disk_mib) or not argv):
        raise ValueError('a new task requires source, CPU, RAM, disk and a command')
    job_id = args.recover or uuid.uuid4().hex
    request = {'job_id': job_id, 'argv': argv, 'paths': args.result, 'cpu': args.cpu,
               'ram_mib': args.ram_mib, 'disk_mib': args.disk_mib, 'timeout': args.timeout}
    if not args.recover:
        wire.request({**request, 'source_bytes': 1, 'source_sha256': '0' * 64})
    target.mkdir(parents=False, exist_ok=False, mode=0o700)
    try:
        relay = Path(os.environ.get('USEVPS_ROOT', '/workspace')) / '.mac-relay/worker.sock'
        if args.recover:
            wait_for_relay(relay.parent.parent, args.timeout)
            with connect_relay(relay, args.timeout) as connection:
                receipt = recover(connection, job_id, target)
        else:
            status = mac_relay.status(relay.parent.parent)
            if not status or not status['available']:
                raise TaskRefused('private Mac relay unavailable')
            receipt = execute(lambda: connect_relay(relay, args.timeout), args.source, target, request)
    except Exception as error:
        if target.is_dir() and not target.is_symlink() and not any(target.iterdir()):
            target.rmdir()
        print(json.dumps({'job_id': job_id, 'status': 'refused' if isinstance(error, TaskRefused) else 'unknown',
                          'output': str(target),
                          'reason': str(error)[:512]}), file=sys.stderr)
        return 1
    print(json.dumps({'job_id': job_id, 'status': receipt['status'], 'output': str(target)}))
    return 0 if receipt['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'status': 'unknown', 'reason': str(error)[:512]}), file=sys.stderr)
        sys.exit(1)
