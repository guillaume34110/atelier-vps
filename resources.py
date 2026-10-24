"""Atomic resource reservations for trusted jobs sharing one bounded worker."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time

from telemetry import counter, process_token, public_label

GIB = 1024 ** 3
MIB = 1024 ** 2
OVERHEAD = 256 * MIB
MAX_JOBS = 4


def cpu_capacity():
    available = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
    # Affinity may be a borrowed subset in a job; the supervisor publishes the pool.
    available = int(os.environ.get('USEVPS_CPU_CAPACITY', available))
    quota = available
    try:
        raw = (Path('/sys/fs/cgroup') / 'cpu.max').read_text().split()
        if raw[0] != 'max':
            quota = max(1, int(raw[0]) // int(raw[1]))
    except (OSError, ValueError, IndexError):
        try:
            limit = counter('cpu/cpu.cfs_quota_us')
            period = counter('cpu/cpu.cfs_period_us')
            if limit and period:
                quota = max(1, limit // period)
        except ZeroDivisionError:
            pass
    return max(1, min(available, quota, int(os.environ.get('USEVPS_MAX_CPUS', '4'))))


def memory_capacity():
    measured = counter('memory.max', 'memory/memory.limit_in_bytes')
    configured = int(float(os.environ.get('USEVPS_POOL_RAM_GIB', '5')) * GIB)
    return max(0, min(measured if measured is not None else configured, configured) - OVERHEAD)


def write_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.lease-', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def resource_lock(root):
    with (root / '.resources.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def leases(root):
    result = []
    for path in sorted((root / '.leases').glob('*.json')):
        if len(result) >= MAX_JOBS:
            raise RuntimeError('too many resource reservations')
        try:
            if path.is_symlink():
                raise RuntimeError('invalid reservation file')
            with path.open('rb') as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise RuntimeError('oversized reservation file')
            value = json.loads(raw)
            if (not re.fullmatch(r'[a-f0-9]{32}', value['id']) or path.stem != value['id']
                    or not re.fullmatch(r'[0-9]+:[0-9]+', value['runner'])
                    or any(type(value[k]) is not int or value[k] < 0
                           for k in ('cpus', 'ram_bytes', 'disk_bytes', 'ram_used', 'disk_used'))):
                raise RuntimeError('invalid resource reservation')
            pid = int(value['runner'].split(':')[0])
            # A killed supervisor may leave detached children: fail closed until restart.
            value['orphaned'] = process_token(pid) != value['runner']
            result.append(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('cannot verify resource reservations') from exc
    return result


def view(root, capacity):
    active = leases(root)
    cpu_total = cpu_capacity()
    ram_total = memory_capacity()
    cpu_reserved = sum(r['cpus'] for r in active)
    ram_reserved = sum(r['ram_bytes'] for r in active)
    disk_reserved = sum(max(0, r['disk_bytes'] - r.get('disk_used', 0)) for r in active)
    unspent_ram = sum(max(0, r['ram_bytes'] - r.get('ram_used', 0)) for r in active)
    reserve_ram = int(float(os.environ.get('USEVPS_HOST_RAM_RESERVE_GIB', '2')) * GIB)
    available_ram = max(0, min(ram_total - ram_reserved,
                             capacity['host_available_ram'] - reserve_ram - OVERHEAD - unspent_ram))
    available_disk = max(0, min(capacity['workspace_free_bytes'] - MIB,
                              capacity['free_bytes'] - capacity['reserve_bytes'] - MIB) - disk_reserved)
    return {'max_jobs': MAX_JOBS, 'cpu_total': cpu_total, 'cpu_reserved': cpu_reserved,
            'cpu_available': max(0, cpu_total - cpu_reserved), 'ram_total': ram_total,
            'ram_reserved': ram_reserved, 'ram_available': available_ram,
            'disk_reserved': disk_reserved, 'disk_available': available_disk,
            'active': active, 'active_count': len(active)}


def fits(pool, cpus, ram, disk):
    return (not any(r['orphaned'] for r in pool['active'])
            and pool['active_count'] < pool['max_jobs'] and cpus <= pool['cpu_available']
            and ram <= pool['ram_available'] and disk <= pool['disk_available'])


def allocations(active, cores):
    """Reserve distinct cores, then lend unused cores without overbooking claims."""
    available = list(cores)
    result = {}
    for item in active:
        count = min(item['cpus'], len(available))
        result[item['id']] = available[:count]
        del available[:count]
    if active:
        for index, core in enumerate(available):
            result[active[index % len(active)]['id']].append(core)
    return result


class Lease:
    def __init__(self, root, value, cores):
        self.root, self.value, self.cores = root, value, cores
        self.path = root / '.leases' / (value['id'] + '.json')

    def update(self, ram_used, disk_used, pids, cache_used):
        with resource_lock(self.root):
            active = leases(self.root)
            current = next(r for r in active if r['id'] == self.value['id'])
            self.value.update(cache_bytes=current.get('cache_bytes', 0),
                              ram_used=ram_used, disk_used=disk_used + current.get('cache_bytes', 0))
            write_json(self.path, self.value)
            state_path = self.root / '.cache-accounting.json'
            previous = json.loads(state_path.read_text()) if state_path.exists() else cache_used
            growth = max(0, cache_used - previous)
            active = leases(self.root)
            # Shared downloads benefit all active jobs; divide their growth once.
            for index, item in enumerate(active):
                share = growth // len(active) + (index < growth % len(active))
                item['cache_bytes'] = item.get('cache_bytes', 0) + share
                item['disk_used'] += share
                write_json(self.root / '.leases' / (item['id'] + '.json'), item)
                if item['id'] == self.value['id']:
                    self.value = item
            write_json(state_path, max(previous, cache_used))
            active = leases(self.root)
            mask = allocations(active, self.cores).get(self.value['id'], [])
        if mask and hasattr(os, 'sched_setaffinity'):
            for pid in pids:
                try:
                    tasks = (Path('/proc') / str(pid) / 'task').iterdir()
                    for task in tasks:
                        os.sched_setaffinity(int(task.name), mask)
                except (OSError, ValueError):
                    pass
        return mask


@contextmanager
def reserve(root, ident, request, capacity):
    cpus = request.get('cpus', 1)
    ram = request.get('ram_bytes', GIB)
    disk = request.get('disk_bytes', GIB)
    if type(cpus) is not int or not 1 <= cpus <= cpu_capacity():
        raise ValueError('CPU reservation must fit the worker pool')
    if type(ram) is not int or not 64 * MIB <= ram <= memory_capacity():
        raise ValueError('RAM reservation must be at least 64 MiB and fit the worker pool')
    if type(disk) is not int or disk <= 0:
        raise ValueError('disk reservation must be positive')
    core_ids = sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else list(range(cpu_capacity()))
    core_ids = core_ids[:cpu_capacity()]
    end = time.monotonic() + float(os.environ.get('USEVPS_QUEUE_WAIT_SECONDS', '30'))
    path = root / '.leases' / (ident + '.json')
    with (root / '.queue.lock').open('a') as queue:
        claimed = False
        try:
            while True:
                try:
                    fcntl.flock(queue, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    shared = True
                except BlockingIOError:
                    shared = False
                if shared:
                    with resource_lock(root):
                        pool = view(root, capacity())
                        if fits(pool, cpus, ram, disk):
                            path.parent.mkdir(exist_ok=True)
                            value = {'id': ident, 'runner': process_token(os.getpid()), 'status': 'running',
                                     'started': time.time(), 'cpus': cpus, 'ram_bytes': ram,
                                     'disk_bytes': disk, 'ram_used': 0, 'disk_used': 0, 'cache_bytes': 0,
                                     'agent': public_label(request.get('agent'), 'Agent non identifié'),
                                     'project': public_label(request.get('project'), 'Projet non identifié')}
                            if value['runner'] is None:
                                raise RuntimeError('cannot identify the reservation owner')
                            write_json(path, value)
                            claimed = True
                            lease = Lease(root, value, core_ids)
                            break
                    fcntl.flock(queue, fcntl.LOCK_UN)
                if time.monotonic() >= end:
                    raise RuntimeError('resources busy: CPU, RAM or disk reservation unavailable')
                time.sleep(0.2)
            yield lease
        finally:
            if claimed:
                with resource_lock(root):
                    path.unlink(missing_ok=True)
                    if not leases(root):
                        (root / '.cache-accounting.json').unlink(missing_ok=True)
            fcntl.flock(queue, fcntl.LOCK_UN)
