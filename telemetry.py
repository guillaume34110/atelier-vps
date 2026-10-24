"""Small, public, optional monitoring data; never used to admit a calculation."""
import json
import fcntl
import math
import os
from pathlib import Path
import re
import tempfile
import time

PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')
HISTORY_LIMIT = 8
ACTIVITY_LIMIT = 16384


def public_label(value, default):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError('public label must be text')
    return ' '.join(''.join(c for c in value if c.isprintable()).split())[:64] or default


def public_record(value):
    if not isinstance(value, dict) or not re.fullmatch(r'[0-9a-f]{32}', str(value.get('id', ''))):
        return None
    if value.get('status') not in {'running', 'passed', 'failed'}:
        return None
    dates = {key: value.get(key) for key in ['started', 'ended']}
    for key, date in dates.items():
        if date is not None and (type(date) not in {int, float} or not math.isfinite(date) or not 0 <= date < 1e11):
            return None
    if dates['started'] is None:
        return None
    try:
        runner = value.get('runner')
        return {'id': value['id'], 'status': value['status'], **dates,
                'runner': runner if isinstance(runner, str) and re.fullmatch(r'[0-9]{1,8}:[0-9]{1,20}', runner) else None,
                'agent': public_label(value.get('agent'), 'Agent non identifié'),
                'project': public_label(value.get('project'), 'Projet non identifié')}
    except ValueError:
        return None


def history(root):
    try:
        path = root / '.activity.json'
        if path.is_symlink():
            return []
        with path.open('rb') as source:
            raw = source.read(ACTIVITY_LIMIT + 1)
        if len(raw) > ACTIVITY_LIMIT:
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return [r for value in data[:HISTORY_LIMIT] if (r := public_record(value)) is not None]
    except (OSError, ValueError, UnicodeError):
        return []


def record_activity(root, state):
    try:
        with (root / '.activity.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _record_activity(root, state)
    except OSError:
        return False


def _record_activity(root, state):
    entry = public_record(state)
    if entry is None:
        return False
    tmp = None
    try:
        records = [entry] + [r for r in history(root) if r['id'] != entry['id']]
        data = json.dumps(records[:HISTORY_LIMIT], ensure_ascii=True)
        assert len(data) <= ACTIVITY_LIMIT
        fd, name = tempfile.mkstemp(prefix='.activity-', suffix='.tmp', dir=root)
        tmp = Path(name)
        with os.fdopen(fd, 'w') as target:
            target.write(data)
        tmp.replace(root / '.activity.json')
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def counter(*names):
    for name in names:
        try:
            value = int((CGROUP / name).read_text())
            if 0 <= value < 2**60:
                return value
        except (OSError, ValueError):
            pass
    return None


def process_token(pid):
    try:
        fields = (PROC / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return str(pid) + ':' + fields[19]
    except (OSError, ValueError, IndexError):
        return None


def snapshot(root, busy):
    result = {'sample_time': time.monotonic(), 'arch': os.uname().machine,
              'cpu_count': os.cpu_count(), 'activity': history(root)}
    try:
        ticks = [int(x) for x in (PROC / 'stat').read_text().splitlines()[0].split()[1:9]]
        if len(ticks) != 8 or any(x < 0 for x in ticks):
            raise ValueError('invalid CPU counters')
        result.update(cpu_total=sum(ticks), cpu_idle=ticks[3] + ticks[4])
    except (OSError, ValueError, IndexError):
        pass
    try:
        fields = dict(line.split(':', 1) for line in (PROC / 'meminfo').read_text().splitlines())
        result['host_total_ram'] = int(fields['MemTotal'].split()[0]) * 1024
    except (OSError, ValueError, KeyError, IndexError):
        pass
    result['worker_ram'] = counter('memory.current', 'memory/memory.usage_in_bytes')
    result['worker_ram_limit'] = counter('memory.max', 'memory/memory.limit_in_bytes')
    try:
        values = dict(line.split() for line in (CGROUP / 'cpu.stat').read_text().splitlines())
        result['worker_cpu_usec'] = int(values['usage_usec'])
    except (OSError, ValueError, KeyError):
        value = counter('cpuacct/cpuacct.usage', 'cpu,cpuacct/cpuacct.usage')
        result['worker_cpu_usec'] = value // 1000 if value is not None else None
    try:
        disk = os.statvfs('/')
        result.update(system_disk_total=disk.f_blocks * disk.f_frsize,
                      system_disk_free=disk.f_bavail * disk.f_frsize)
    except OSError:
        pass
    active = next((r for r in result['activity'] if r['status'] == 'running' and r['runner']
                   and process_token(int(r['runner'].split(':')[0])) == r['runner']), None)
    result['active'] = active if busy else None
    return result
