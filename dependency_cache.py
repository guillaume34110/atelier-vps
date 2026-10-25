"""Private, reproducible dependency caches; immutable tools and job files stay out."""
import fcntl
import hashlib
import json
import math
import re
import shutil
import time

import resources

RETENTION_SECONDS = 30 * 86400


def scope(root, request):
    # Commands sharing a project/lockfile reuse downloads, including failed retries.
    key = hashlib.sha256(json.dumps([request.get('project'), request.get('dependency_digest')],
                                   sort_keys=True).encode()).hexdigest()
    base = root / 'cache'
    if base.is_symlink():
        raise ValueError('unsafe dependency cache')
    base.mkdir(exist_ok=True)
    managed = base / 'managed'
    if managed.is_symlink():
        raise ValueError('unsafe dependency cache')
    managed.mkdir(exist_ok=True)
    path = managed / key
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


def maintain(root):
    """Run hourly while idle; the same exclusive lock protects deploys and jobs."""
    try:
        with (root / '.queue.lock').open('a') as queue:
            fcntl.flock(queue, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with resources.resource_lock(root):
                if resources.leases(root):
                    return
                base = root / 'cache' / 'managed'
                if (root / 'cache').is_symlink() or base.is_symlink():
                    return
                now = time.time()
                removed = 0
                for path in base.iterdir() if base.is_dir() else []:
                    if not re.fullmatch('[0-9a-f]{64}', path.name) or not path.is_dir():
                        continue
                    stamp = last_used(path)
                    if stamp is not None and now - stamp >= RETENTION_SECONDS:
                        shutil.rmtree(path)
                        removed += 1
                    # Bound each idle pass; new work keeps its usual admission timeout.
                    if removed >= 8:
                        break
                resources.write_json(root / '.cache-retention.json',
                                     {'days': 30, 'checked': now, 'removed_scopes': removed})
    except (OSError, ValueError, RuntimeError):
        pass  # Cache housekeeping never replaces health, admission or job results.


def status(root):
    try:
        path = root / '.cache-retention.json'
        if path.is_symlink() or path.stat().st_size > 512:
            return None
        data = json.loads(path.read_text())
        if (isinstance(data, dict) and data.get('days') == 30 and type(data.get('checked')) in {int, float}
                and math.isfinite(data['checked']) and 0 <= data['checked'] < 1e11
                and type(data.get('removed_scopes')) is int and 0 <= data['removed_scopes'] <= 8):
            return {k: data[k] for k in ['days', 'checked', 'removed_scopes']}
    except (OSError, ValueError):
        pass
    return None
