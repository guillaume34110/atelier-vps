"""Small, public, optional monitoring data; never used to admit a calculation."""
import json
import fcntl
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

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
        record = {'id': value['id'], 'status': value['status'], **dates,
                'runner': runner if isinstance(runner, str) and re.fullmatch(r'[0-9]{1,8}:[0-9]{1,20}', runner) else None,
                'agent': public_label(value.get('agent'), 'Agent non identifié'),
                'project': public_label(value.get('project'), 'Projet non identifié')}
        if 'direction' in value:
            if (value['direction'] not in {'upload', 'download'}
                    or type(value.get('bytes')) is not int or not 0 <= value['bytes'] < 2**40
                    or type(value.get('updated')) not in {int, float}
                    or not math.isfinite(value['updated']) or not 0 <= value['updated'] < 1e11):
                return None
            record.update(direction=value['direction'], bytes=value['bytes'], updated=value['updated'])
            job = value.get('job')
            if isinstance(job, str) and re.fullmatch('[0-9a-f]{32}', job):
                record['job'] = job
        return record
    except ValueError:
        return None


def history(root, kind='activity'):
    try:
        path = root / ('.' + kind + '.json')
        if path.is_symlink():
            return []
        with path.open('rb') as source:
            raw = source.read((32768 if kind == 'transfers' else ACTIVITY_LIMIT) + 1)
        if len(raw) > (32768 if kind == 'transfers' else ACTIVITY_LIMIT):
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        limit = 16 if kind == 'transfers' else HISTORY_LIMIT
        records = [r for value in data[:limit] if (r := public_record(value)) is not None]
        if kind == 'transfers':
            records = [r for r in records if 'direction' in r]
            for record in records:
                token = record['runner']
                if record['status'] == 'running' and (not token or process_token(int(token.split(':')[0])) != token):
                    record.update(status='failed', ended=record['updated'])
        return records
    except (OSError, ValueError, UnicodeError):
        return []


def record_activity(root, state, kind='activity'):
    try:
        with (root / ('.' + kind + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _record_activity(root, state, kind)
    except OSError:
        return False


def _record_activity(root, state, kind):
    entry = public_record(state)
    if entry is None:
        return False
    tmp = None
    try:
        records = [entry] + [r for r in history(root, kind) if r['id'] != entry['id']]
        limit = HISTORY_LIMIT
        if kind == 'transfers':
            records.sort(key=lambda r: (r['status'] == 'running', r.get('updated', 0)), reverse=True)
            limit = 16
        data = json.dumps(records[:limit], ensure_ascii=True)
        assert len(data) <= (32768 if kind == 'transfers' else ACTIVITY_LIMIT)
        fd, name = tempfile.mkstemp(prefix='.activity-', suffix='.tmp', dir=root)
        tmp = Path(name)
        with os.fdopen(fd, 'w') as target:
            target.write(data)
        tmp.replace(root / ('.' + kind + '.json'))
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class Transfer:
    """Count actual SSH archive bytes without adding a service or retaining paths."""
    def __init__(self, root, stream, direction, job, agent, project):
        self.root, self.stream = root, stream
        self.state = dict(id=uuid.uuid4().hex, job=job, agent=agent, project=project,
                          direction=direction, bytes=0, status='running',
                          started=time.time(), ended=None, runner=process_token(os.getpid()))
        self.last_sample = 0

    def __enter__(self):
        self.sample(force=True)
        return self

    def sample(self, force=False):
        if force or time.monotonic() - self.last_sample >= 1:
            self.state['updated'] = time.time()
            record_activity(self.root, self.state, 'transfers')
            self.last_sample = time.monotonic()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.state['bytes'] += len(data)
        self.sample()
        return data

    def write(self, data):
        written = self.stream.write(data)
        self.state['bytes'] += written
        self.sample()
        return written

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __exit__(self, kind, *_):
        self.state.update(status='failed' if kind else 'passed', ended=time.time())
        self.sample(force=True)


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
              'cpu_count': os.cpu_count(), 'activity': history(root),
              'transfers': history(root, 'transfers')}
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
