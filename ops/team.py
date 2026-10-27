"""Validated allocations shared by the administrator, gateway and tests."""
import base64
import hashlib
import json
import math
import re

GIB = 1024 ** 3
MIB = 1024 ** 2


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def public_key(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError('an ed25519 public key is required')
    parts = value.split()
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, IndexError):
        raise ValueError('invalid ed25519 public key') from None
    if parts[0] != 'ssh-ed25519' or len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('invalid ed25519 public key')
    return ' '.join(parts[:2])


def allocations(policy, capacity):
    if not isinstance(policy, dict) or set(policy) != {'mode', 'pool', 'users'}:
        raise ValueError('settings require mode, pool and users only')
    if policy['mode'] not in {'opportunistic', 'allocated'}:
        raise ValueError('mode must be opportunistic or allocated')
    pool = policy['pool']
    if not isinstance(pool, dict) or set(pool) != {'cpus', 'ram_bytes', 'disk_bytes', 'slots'}:
        raise ValueError('pool requires cpus, ram_bytes, disk_bytes and slots')
    for key in pool:
        value = pool[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > capacity[key]:
            raise ValueError('pool exceeds server capacity: ' + key)
        if key != 'cpus' and type(value) is not int:
            raise ValueError('bytes and slots must be integers')
    users = policy['users']
    if not isinstance(users, list) or not 1 <= len(users) <= 4:
        raise ValueError('configure one to four developers')
    seen = set()
    keys = set()
    totals = {'cpu_percent': 0, 'ram_percent': 0, 'disk_percent': 0, 'slots': 0}
    result = {}
    for user in users:
        if (not isinstance(user, dict) or set(user) - {'id', 'enabled', 'public_key', 'cpu_percent', 'ram_percent', 'disk_percent', 'slots'}
                or not {'id', 'cpu_percent', 'ram_percent', 'disk_percent', 'slots'} <= set(user)):
            raise ValueError('invalid developer settings')
        name = user['id']
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,15}', name) or name in seen or name in {'admin', 'root', 'usevps'}:
            raise ValueError('developer IDs must be unique short slugs')
        seen.add(name)
        enabled = user.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        if name != 'owner' or user.get('public_key'):
            key = public_key(user.get('public_key'))
            if key in keys:
                raise ValueError('each developer needs a distinct SSH public key')
            keys.add(key)
        for key in ['cpu_percent', 'ram_percent', 'disk_percent']:
            value = user[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100:
                raise ValueError('allocation percentages must be finite and positive')
            if enabled or key == 'disk_percent':
                totals[key] += value
        cpus = math.floor(pool['cpus'] * user['cpu_percent'] / 100 * 100000) / 100000
        if cpus < 0.01:
            raise ValueError('CPU allocation must be at least 0.01 vCPU')
        slots = user['slots']
        if type(slots) is not int or not 1 <= slots <= 4:
            raise ValueError('developer slots must be 1..4')
        if slots > max(1, math.ceil(cpus)):
            raise ValueError('developer slots exceed its CPU reservation units')
        totals['slots'] += slots if enabled else 0
        ram = int(pool['ram_bytes'] * user['ram_percent'] / 100) // MIB * MIB
        disk = int(pool['disk_bytes'] * user['disk_percent'] / 100) // MIB * MIB
        if ram < (slots * 64 + 128) * MIB or disk < 64 * MIB:
            raise ValueError('allocation must cover supervisors and a task workspace')
        result[name] = {'enabled': enabled, 'cpus': cpus,
                        'cpu_shares': max(2, int(128 * user['cpu_percent'] / 100)),
                        'ram_bytes': ram, 'disk_bytes': disk, 'slots': slots,
                        'inodes': max(1024, int(500000 * user['disk_percent'] / 100)),
                        'ssh_user': 'usevps' if name == 'owner' else 'avps-' + name}
    if any(totals[k] > 100 for k in ['cpu_percent', 'ram_percent', 'disk_percent']) or totals['slots'] > pool['slots']:
        raise ValueError('allocations exceed the pool; disabled developers retain their disk quota')
    active = [value for value in result.values() if value['enabled']]
    if not active or (policy['mode'] == 'opportunistic' and len(active) != 1):
        raise ValueError('opportunistic mode requires exactly one enabled developer')
    while sum(value['cpu_shares'] for value in active) > 128:
        max(active, key=lambda value: value['cpu_shares'])['cpu_shares'] -= 1
    return result


def cpu_sets(policy, limits, cores):
    """Different workers must not all pin their jobs to the first host cores."""
    if policy['mode'] != 'allocated':
        return {}
    result, offset = {}, 0
    for user in policy['users']:
        limit = limits[user['id']]
        if not limit['enabled']:
            continue
        count = max(1, math.ceil(limit['cpus']))
        if count > len(cores):
            raise ValueError('CPU allocation exceeds the available physical cores')
        result[user['id']] = ','.join(str(cores[(offset + i) % len(cores)]) for i in range(count))
        offset += count
    return result


def changes(previous, candidate):
    old = {user['id']: user for user in previous['users']}
    new = {user['id']: user for user in candidate['users']}
    if not old.keys() <= new.keys():
        raise ValueError('developer removal is refused; disable access and preserve existing data')
    if previous['mode'] != candidate['mode'] or previous['pool'] != candidate['pool']:
        return sorted(new)
    return sorted(name for name, value in new.items() if old.get(name) != value)
