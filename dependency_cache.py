"""Private, reproducible dependency caches; immutable tools and job files stay out."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time

import resources

RETENTION_SECONDS = 30 * 86400
PACKAGE_CACHES = {'pip', 'npm', 'pnpm', 'uv', 'gradle/caches', 'gradle/wrapper/dists'}


def package_bytes(path):
    total = 0
    for base, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            try:
                total += (Path(base) / name).lstat().st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def scope_path(root, request):
    key = hashlib.sha256(json.dumps([request.get('project'), request.get('dependency_digest')],
                                   sort_keys=True).encode()).hexdigest()
    return root / 'cache' / 'managed' / key


def scope(root, request):
    # Commands sharing a project/lockfile reuse downloads, including failed retries.
    base = root / 'cache'
    if base.is_symlink():
        raise ValueError('unsafe dependency cache')
    base.mkdir(exist_ok=True)
    managed = base / 'managed'
    if managed.is_symlink():
        raise ValueError('unsafe dependency cache')
    managed.mkdir(exist_ok=True)
    path = scope_path(root, request)
    if path.is_symlink():
        raise ValueError('unsafe dependency cache')
    path.mkdir(exist_ok=True)
    touch(path)
    return path


def touch(path):
    resources.write_json(path / '.retention.json', {'version': 1, 'last_used': time.time()})


def last_used(path):
    marker = path / '.retention.json'
    if path.is_symlink() or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 256:
        return None
    try:
        data = json.loads(marker.read_text())
        if not isinstance(data, dict):
            return None
        stamp = data.get('last_used')
        if data.get('version') == 1 and type(stamp) in {int, float} and math.isfinite(stamp) and stamp >= 0:
            return stamp
    except (OSError, ValueError):
        pass
    return None


def maintain(root, *, scheduled=False):
    """Run hourly while idle; the same exclusive lock protects deploys and jobs."""
    previous = status(root) if scheduled else None
    if (previous and 0 <= time.time() - previous['checked'] < 3600
            and not (previous['removed_scopes'] and previous.get('package_bytes', 0) > previous.get('budget_bytes', 0))):
        return {'status': 'not_due', **previous}
    try:
        with (root / '.queue.lock').open('a') as queue:
            fcntl.flock(queue, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if resources.leases(root):
                return {'status': 'busy', 'removed_scopes': 0, 'reclaimed_bytes': 0}
            base = root / 'cache' / 'managed'
            if (root / 'cache').is_symlink() or base.is_symlink():
                raise ValueError('unsafe managed cache path')
            now = time.time()
            volume = os.statvfs(root)
            budget = volume.f_blocks * volume.f_frsize // 10
            candidates = []
            for path in base.iterdir() if base.is_dir() else []:
                if not re.fullmatch('[0-9a-f]{64}', path.name) or not path.is_dir():
                    continue
                stamp = last_used(path)
                if stamp is None:
                    continue
                # XDG caches may also contain models or profiles; only package-manager namespaces are disposable.
                packages = []
                for name in sorted(PACKAGE_CACHES):
                    parts = name.split('/')
                    child = path.joinpath(*parts)
                    if (child.is_dir() and not any(path.joinpath(*parts[:i]).is_symlink()
                                                  for i in range(1, len(parts) + 1))):
                        packages.append((child, package_bytes(child)))
                candidates.append((stamp, path, packages))
            total = sum(size for _, _, packages in candidates for _, size in packages)
            removed = reclaimed = 0
            for stamp, path, packages in sorted(candidates, key=lambda value: value[0]):
                expired = now - stamp >= RETENTION_SECONDS
                if not expired and not packages:
                    continue
                if not expired and (total <= budget or now - stamp < 3600):
                    continue
                for child, size in packages:
                    shutil.rmtree(child)
                    total -= size
                    reclaimed += size
                if all(child.name == '.retention.json' for child in path.iterdir()):
                    shutil.rmtree(path)
                if packages or not path.exists():
                    removed += 1
                # Bound each idle pass; new work keeps its usual admission timeout.
                if removed >= 8:
                    break
            resources.write_json(root / '.cache-retention.json',
                                 {'days': 30, 'checked': now, 'removed_scopes': removed,
                                  'evicted_package_bytes': reclaimed,
                                  'reclaimed_bytes': max(0, (os.statvfs(root).f_bavail - volume.f_bavail) * volume.f_frsize),
                                  'package_bytes': total, 'budget_bytes': budget})
            return {'status': 'completed', **status(root)}
    except BlockingIOError:
        return {'status': 'busy', 'removed_scopes': 0, 'reclaimed_bytes': 0}
    except (OSError, ValueError, RuntimeError):
        return {'status': 'error', 'error': 'Managed cache maintenance could not finish; preserve existing data.'}


def status(root):
    try:
        path = root / '.cache-retention.json'
        if path.is_symlink() or path.stat().st_size > 512:
            return None
        data = json.loads(path.read_text())
        if (isinstance(data, dict) and data.get('days') == 30 and type(data.get('checked')) in {int, float}
                and math.isfinite(data['checked']) and 0 <= data['checked'] < 1e11
                and type(data.get('removed_scopes')) is int and 0 <= data['removed_scopes'] <= 8):
            values = {k: data[k] for k in ['days', 'checked', 'removed_scopes']}
            for key in ['reclaimed_bytes', 'evicted_package_bytes', 'package_bytes', 'budget_bytes']:
                if key in data:
                    if type(data[key]) is not int or not 0 <= data[key] < 2**63:
                        return None
                    values[key] = data[key]
            return values
    except (OSError, ValueError):
        pass
    return None
