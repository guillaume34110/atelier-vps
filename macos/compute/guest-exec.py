"""Run one request inside the isolated guest and export only declared files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import stat
import subprocess
import sys
import time

ROOT = Path('/job/source')
WORK = Path('/job/control')
CGROUP = Path('/sys/fs/cgroup/atelier')
LOG_LIMIT = 18 * 1024 ** 2
RESULT_LIMIT = 256 * 1024 ** 2
PRIVATE = {'.git', '.ssh', '.config', '.codex', '.claude', '.agents', '.aws', '.kube',
           '.env', '.npmrc', '.pypirc', '.netrc', 'auth.json', 'credentials.json'}


def safe_name(name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('invalid result path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or any(
        part.casefold() == 'claude.md' or part in PRIVATE or part.startswith(('.env.', 'id_ed25519', 'id_rsa'))
        or part.endswith(('.pem', '.key')) for part in path.parts
    ):
        raise ValueError('private or unsafe result path')
    return path


def drop_privileges():
    (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


def kill_descendants():
    (CGROUP / 'cgroup.kill').write_text('1')
    deadline = time.monotonic() + 5
    while 'populated 1' in (CGROUP / 'cgroup.events').read_text():
        if time.monotonic() >= deadline:
            raise RuntimeError('guest processes did not stop')
        time.sleep(.05)


def collect(paths):
    target = WORK / 'results'
    target.mkdir(mode=0o700)
    files = {}
    for name in paths:
        rel = safe_name(name)
        source = ROOT.joinpath(*rel.parts)
        component = ROOT
        for part in rel.parts:
            component /= part
            if component.is_symlink():
                raise ValueError('result links are forbidden')
        if not source.exists() or source.is_symlink():
            raise ValueError('requested result is missing or a link')
        for file in ([source] if source.is_file() else source.rglob('*')):
            if file.is_symlink():
                raise ValueError('result links are forbidden')
            if not file.is_file() and not file.is_dir():
                raise ValueError('special result files are forbidden')
            if file.is_file():
                relative = safe_name(file.relative_to(ROOT).as_posix())
                files[str(relative)] = file
    if len(files) > 10000:
        raise ValueError('too many result files')
    total = 0
    manifest = {}
    for name, file in sorted(files.items()):
        info = file.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('result must be a regular file')
        total += info.st_size
        if total > RESULT_LIMIT:
            raise ValueError('result exceeds 256 MiB')
        destination = target.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with file.open('rb') as source, destination.open('xb') as output:
            digest = hashlib.sha256()
            while chunk := source.read(65536):
                digest.update(chunk)
                output.write(chunk)
        manifest[name] = {'bytes': info.st_size, 'sha256': digest.hexdigest()}
    return manifest


def run(request):
    argv = request.get('argv')
    paths = request.get('paths', [])
    timeout = request.get('timeout')
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
            or sum(len(arg.encode()) for arg in argv) > 32768
            or not isinstance(paths, list) or len(paths) > 64
            or type(timeout) is not int or not 1 <= timeout <= 3600):
        raise ValueError('invalid guest request')
    for name in paths:
        safe_name(name)
    process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=drop_privileges,
        start_new_session=True)
    deadline = time.monotonic() + timeout
    written = 0
    reason = None
    try:
        with (WORK / 'stdout.log').open('xb') as output:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'timeout'
                    break
                if not select.select([process.stdout], [], [], min(1, remaining))[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                if written + len(chunk) > LOG_LIMIT:
                    reason = 'log limit'
                    break
                output.write(chunk)
                written += len(chunk)
        if not reason:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = 'timeout'
    finally:
        kill_descendants()
        process.wait(timeout=5)
    present = paths if process.returncode == 0 and not reason else [
        name for name in paths if (ROOT / safe_name(name)).exists() or (ROOT / safe_name(name)).is_symlink()
    ]
    manifest = collect(present)
    return {'status': 'passed' if process.returncode == 0 and not reason else 'failed',
            'returncode': process.returncode, 'reason': reason, 'results': manifest}


def main():
    try:
        request = json.loads((WORK / 'request.json').read_text())
        result = run(request)
    except Exception as error:
        result = {'status': 'failed', 'returncode': 1, 'reason': str(error), 'results': {}}
    tmp = WORK / 'receipt.tmp'
    tmp.write_text(json.dumps(result))
    tmp.replace(WORK / 'receipt.json')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
