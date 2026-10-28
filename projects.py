"""Versioned project sources inside one developer's dedicated workspace."""
import hashlib
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

LIMIT = 64 * 1024 ** 2


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', value):
        raise ValueError('invalid project identifier')
    return value


def revision(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('invalid project revision')
    return value


def open_relative(root, name):
    parts = PurePosixPath(name).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def fingerprint(files, check=None, root=None):
    entries = []
    total = 0
    since_check = 0
    for file, name in sorted(files, key=lambda item: item[1]):
        rel = PurePosixPath(name)
        private = {'.ssh', '.config', '.codex', '.claude', '.agents', '.desktops', '.agent-auth', '.aws', '.kube'}
        if (not name or rel.is_absolute() or '..' in rel.parts or '\x00' in name
                or any(part.casefold() == 'claude.md' or part in private for part in rel.parts)
                or rel.name in {'.env', '.npmrc', '.pypirc', '.netrc', 'credentials.json', 'auth.json', 'deployment.local.json'}
                or rel.name.startswith(('.env.', 'id_ed25519', 'id_rsa', 'ssh_host_'))
                or rel.name.endswith(('.pem', '.key'))):
            raise ValueError('unsafe project source path')
        try:
            fd = open_relative(root, name) if root is not None else os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError('project source links are forbidden') from exc
            raise
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('project sources must be regular files')
            if len(entries) >= 10000 or total + info.st_size > LIMIT:
                raise ValueError('project source exceeds transfer limits')
            digest = hashlib.sha256()
            while chunk := source.read(4 * 1024 ** 2):
                digest.update(chunk)
                since_check += len(chunk)
                if check and since_check >= 4 * 1024 ** 2:
                    check(); since_check = 0
        entries.append({'path': name, 'bytes': info.st_size, 'sha256': digest.hexdigest(),
                        'executable': bool(info.st_mode & 0o111)})
        total += info.st_size
    raw = json.dumps(entries, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    if len(raw) > 2 * 1024 ** 2:
        raise ValueError('project manifest exceeds its limit')
    return hashlib.sha256(raw).hexdigest(), entries


def tree_files(root):
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(base) / name).is_symlink():
                raise ValueError('project source links are forbidden')
        for name in files:
            path = Path(base) / name
            yield path, path.relative_to(root).as_posix()


def directory(root, project, *, create=False):
    project = identifier(project)
    parent = Path(root) / '.atelier-managed-projects'
    target = parent / project
    versions = target / 'versions'
    for path, marker in ((parent, {'version': 1}), (target, {'version': 1, 'id': project})):
        created = False
        if create:
            try:
                path.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
        if path.is_symlink() or not path.is_dir():
            raise ValueError('project storage must use real directories')
        ownership = path / '.managed.json'
        if created:
            with ownership.open('x') as output:
                json.dump(marker, output, sort_keys=True)
        if (ownership.is_symlink() or not ownership.is_file() or ownership.stat().st_nlink != 1
                or ownership.stat().st_size > 256 or json.loads(ownership.read_text()) != marker):
            raise ValueError('refusing an unowned project directory')
    if create:
        versions.mkdir(mode=0o700, exist_ok=True)
    if versions.is_symlink() or not versions.is_dir():
        raise ValueError('project versions must use a real directory')
    return target


def verify(root, project, expected, check=None):
    expected = revision(expected)
    target = directory(root, project) / 'versions' / expected
    source = target / 'source'
    meta = target / 'manifest.json'
    if target.is_symlink() or source.is_symlink() or meta.is_symlink() or not source.is_dir() or not meta.is_file():
        raise ValueError('project version is unavailable or unsafe')
    if meta.stat().st_size > 2 * 1024 ** 2:
        raise ValueError('project manifest exceeds its limit')
    saved = json.loads(meta.read_text())
    digest, entries = fingerprint(tree_files(source), check, source)
    if digest != expected or saved != entries:
        raise ValueError('project version failed integrity verification')
    return source, entries


def store(root, project, expected, work, job_id, check=None):
    target = directory(root, project, create=True)
    expected = revision(expected)
    digest, entries = fingerprint(tree_files(work), check, work)
    if digest != expected:
        raise ValueError('project changed while sources were transferred')
    versions = target / 'versions'
    destination = versions / expected
    if destination.is_symlink():
        raise ValueError('project version must not be a link')
    if destination.exists():
        verify(root, project, expected, check)
        return {'id': project, 'revision': expected, 'bytes': sum(x['bytes'] for x in entries), 'created': False}
    staging = versions / ('.staging-' + job_id)
    staging.mkdir(mode=0o700)
    work.rename(staging / 'source')
    with (staging / 'manifest.json').open('x') as output:
        json.dump(entries, output, sort_keys=True, separators=(',', ':'))
        output.flush(); os.fsync(output.fileno())
    staging.rename(destination)
    work.mkdir()
    verify(root, project, expected, check)
    return {'id': project, 'revision': expected, 'bytes': sum(x['bytes'] for x in entries), 'created': True}


def materialize(root, project, expected, work, peak, check):
    source, entries = verify(root, project, expected, check)
    if sum(item['bytes'] for item in entries) > peak:
        raise ValueError('project sources exceed the declared disk peak')
    check()
    since_check = 0
    for item in entries:
        dest = work.joinpath(*PurePosixPath(item['path']).parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = open_relative(source, item['path'])
        with os.fdopen(fd, 'rb') as inp, dest.open('xb') as out:
            if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                raise ValueError('project sources must be regular files')
            while chunk := inp.read(4 * 1024 ** 2):
                out.write(chunk)
                since_check += len(chunk)
                if since_check >= 4 * 1024 ** 2:
                    check(); since_check = 0
        dest.chmod(0o755 if item['executable'] else 0o644)
    check()
    digest, copied = fingerprint(tree_files(work), check, work)
    if digest != expected or copied != entries:
        raise ValueError('project changed while materializing a task')
