"""One budgeted, isolated Mac VM task, controlled by a private local socket."""
import gzip
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import mac_protocol as wire
import admission


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(65536):
            value.update(chunk)
    return value.hexdigest()


def unpack_source(archive, destination):
    total = count = 0
    with tarfile.open(archive, mode='r:') as tar:
        for member in tar:
            rel = wire.result_path(member.name)
            count += 1
            if count > 10000 or member.size < 0:
                raise ValueError('source file count limit')
            target = destination.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > 64 * wire.MIB:
                    raise ValueError('source exceeds limit')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as output:
                    wire.copy_exact(source, output, member.size, 64 * wire.MIB)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError('source links and special files are forbidden')


def initramfs(workspace, base, init_script=None):
    layer = workspace / 'init-layer'
    layer.mkdir()
    init = layer / 'init'
    init.write_bytes((init_script or Path(__file__).parent / 'guest-init.sh').read_bytes())
    init.chmod(0o755)
    package = subprocess.run(['/usr/bin/cpio', '-o', '-H', 'newc'], cwd=layer,
                             input=b'init\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    path = workspace / 'boot.initrd'
    with path.open('xb') as output, base.open('rb') as source:
        while chunk := source.read(65536):
            output.write(chunk)
        output.write(gzip.compress(package))
    return path


def pack_result(share, workspace, request):
    receipt_path, log = share / 'receipt.json', share / 'stdout.log'
    if receipt_path.is_symlink() or log.is_symlink() or receipt_path.stat().st_size > 2 * wire.MIB:
        raise ValueError('invalid guest result metadata')
    receipt = json.loads(receipt_path.read_text())
    if (not isinstance(receipt, dict) or receipt.get('status') not in {'passed', 'failed'}
            or type(receipt.get('returncode')) is not int or not isinstance(receipt.get('results'), dict)
            or len(receipt['results']) > 10000 or log.stat().st_size > 20 * wire.MIB):
        raise ValueError('invalid guest result')
    expected = []
    total = 0
    result_root = share / 'results'
    if result_root.is_symlink() or not result_root.is_dir():
        raise ValueError('invalid result directory')
    for name, info in receipt['results'].items():
        rel = wire.result_path(name)
        if not any(rel == wire.result_path(item) or rel.is_relative_to(wire.result_path(item))
                   for item in request['paths']):
            raise ValueError('undeclared guest result')
        path = result_root
        for part in rel.parts:
            path /= part
            if path.is_symlink():
                raise ValueError('result links are forbidden')
        entry = path.lstat()
        if (not stat.S_ISREG(entry.st_mode) or not isinstance(info, dict)
                or set(info) != {'bytes', 'sha256'} or type(info['bytes']) is not int
                or info['bytes'] != entry.st_size or digest(path) != info['sha256']):
            raise ValueError('guest result verification failed')
        total += entry.st_size
        if total > 256 * wire.MIB:
            raise ValueError('guest result exceeds limit')
        expected.append((path, 'results/' + name))
    archive = workspace / 'result.tar'
    with tarfile.open(archive, mode='x') as tar:
        for path, name in [(receipt_path, 'receipt.json'), (log, 'stdout.log'), *sorted(expected)]:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('result is not a regular file')
            tar.add(path, arcname=name, recursive=False)
    size = archive.stat().st_size
    if size > wire.RESULT_LIMIT:
        raise ValueError('result archive exceeds limit')
    return {'state': 'completed', 'job_id': request['job_id'],
            'archive_bytes': size, 'archive_sha256': digest(archive)}


def execute(connection, workspace, assets):
    reader = connection.makefile('rb', buffering=0)
    writer = connection.makefile('wb', buffering=0)
    try:
        wire.write_frame(writer, {'state': 'ready'})
        request = wire.request(wire.read_frame(reader))
        (workspace / 'owner.json').write_text(json.dumps({'job_id': request['job_id'], 'guard_pid': os.getppid()}))
        allowed, reason = admission.check(request)
        if not allowed:
            wire.write_frame(writer, {'state': 'refused', 'reason': reason})
            return False
        wire.write_frame(writer, {'state': 'accepted'})
        source = workspace / 'source.tar'
        with source.open('xb') as target:
            source_hash = wire.copy_exact(reader, target, request['source_bytes'], wire.SOURCE_LIMIT)
        if source_hash != request['source_sha256']:
            raise ValueError('source digest mismatch')
        share = workspace / 'share'
        share.mkdir(mode=0o700)
        (share / 'source').mkdir(mode=0o700)
        unpack_source(source, share / 'source')
        (share / 'request.json').write_text(json.dumps({key: request[key] for key in ('argv', 'paths', 'timeout')}))
        (share / 'exec.py').write_bytes((Path(__file__).parent / 'guest-exec.py').read_bytes())
        image = workspace / 'disk.img'
        with image.open('xb') as disk:
            disk.truncate(request['disk_mib'] * wire.MIB)
        subprocess.run([assets['mke2fs'], '-q', '-t', 'ext4', '-F', str(image)], check=True,
                       stdin=subprocess.DEVNULL, timeout=30)
        boot = initramfs(workspace, Path(assets['initramfs']))
        allowed, reason = admission.check(request)
        if not allowed:
            raise RuntimeError('Mac no longer available: ' + reason)
        vm_timeout = min(3600, request['timeout'] + 60)
        command = ['/usr/bin/nice', '-n', '10', assets['vm'], assets['kernel'], str(boot),
                   str(share), assets['tools'], assets['modules'], str(image), str(request['cpu']),
                   str(request['ram_mib'] * wire.MIB), str(vm_timeout)]
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL, timeout=vm_timeout + 15)
        status = (share / 'status').read_text().strip()
        if status not in {'0', '1'}:
            raise ValueError('incomplete guest shutdown')
        response = pack_result(share, workspace, request)
        receipt = json.loads((share / 'receipt.json').read_text())
        if (status == '0') != (receipt['status'] == 'passed'):
            raise ValueError('guest status does not match receipt')
        record = workspace / 'result-record.json'
        record.write_text(json.dumps(response))
        wire.write_frame(writer, response)
        with (workspace / 'result.tar').open('rb') as result:
            wire.copy_exact(result, writer, response['archive_bytes'], wire.RESULT_LIMIT)
        acknowledgement = wire.read_frame(reader)
        if acknowledgement != {'state': 'received', 'job_id': request['job_id'],
                               'archive_sha256': response['archive_sha256']}:
            raise ValueError('result acknowledgement mismatch')
        (workspace / 'ack.json').write_text(json.dumps(acknowledgement))
        return True
    finally:
        reader.close()
        writer.close()


def main():
    if len(sys.argv) != 8:
        raise ValueError('expected control socket and six worker assets')
    assets = dict(zip(('kernel', 'initramfs', 'modules', 'tools', 'vm', 'mke2fs'), sys.argv[2:]))
    workspace = Path(os.environ['TASK_WORKSPACE'])
    fd = os.open(workspace / '.task.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3700)
            connection.connect(sys.argv[1])
            return 0 if execute(connection, workspace, assets) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Mac task preserved:', str(error)[:512], file=sys.stderr)
        sys.exit(1)
