#!/usr/bin/python3
"""Root-only, revision-checked settings. No Docker authority reaches workers."""
from contextlib import ExitStack
import csv
import fcntl
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time

try:
    from . import team
except ImportError:
    import team

STATE = Path('/etc/atelier-vps-team.json')
BASE = Path('/etc/usevps-gateway.json')
PENDING = Path('/etc/atelier-vps-team.pending.json')


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def read(path):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 262144:
        raise ValueError('administrator configuration ownership or size mismatch')
    return json.loads(path.read_text())


def save(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(value, target, sort_keys=True)
            target.flush(); os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def quota_usage(mount):
    text = command(['repquota', '-P', '-n', '-O', 'csv', mount])
    return {int(row['Project'].lstrip('#')): {'used': int(row['BlockUsed']) * 1024,
            'limit': int(row['BlockHardLimit']) * 1024, 'inodes': int(row['FileUsed'])}
            for row in csv.DictReader(io.StringIO(text))}


def verify_mount(base):
    values = command(['findmnt', '-n', '-o', 'UUID,OPTIONS', '--mountpoint', base['mount']]).split()
    if len(values) != 2 or values[0] != base['volume_id'] or 'prjquota' not in values[1].split(','):
        raise ValueError('dedicated quota volume is not mounted')


def identity(user, public, admin=False, existing=False):
    public = team.public_key(public)
    try:
        account = pwd.getpwnam(user)
        marker = Path(account.pw_dir) / '.atelier-ssh-id'
        owned = marker.is_file() and not marker.is_symlink() and marker.stat().st_uid == 0 and marker.read_text().strip() == user
        if not owned and not (existing and user == 'usevps' and account.pw_uid == 10001):
            raise ValueError('refusing to adopt an existing SSH account')
    except KeyError:
        command(['useradd', '--create-home', '--shell', '/bin/sh', user])
        account = pwd.getpwnam(user)
        marker = Path(account.pw_dir) / '.atelier-ssh-id'
        marker.write_text(user + '\n'); marker.chmod(0o400)
    home = Path(account.pw_dir)
    home.chmod(0o755); os.chown(home, 0, 0)
    ssh = home / '.ssh'; ssh.mkdir(exist_ok=True); ssh.chmod(0o755); os.chown(ssh, 0, 0)
    helper = '/usr/local/libexec/atelier-settings' if admin else '/usr/local/libexec/usevps-gateway'
    authorized = ssh / 'authorized_keys'
    if authorized.is_symlink():
        raise ValueError('authorized keys must be root-owned regular files')
    authorized.write_text('restrict,command="sudo -n ' + helper + '" ' + public + '\n')
    authorized.chmod(0o444); os.chown(authorized, 0, 0)
    target = Path('/etc/sudoers.d/atelier-' + user)
    temporary = target.with_suffix('.tmp')
    temporary.write_text('Defaults:' + user + ' env_keep += "SSH_ORIGINAL_COMMAND"\n' + user + ' ALL=(root) NOPASSWD: ' + helper + ' ""\n')
    temporary.chmod(0o440)
    command(['visudo', '-cf', str(temporary)])
    temporary.replace(target)


def initialize(application, admin_key):
    base = read(BASE); verify_mount(base)
    if not STATE.exists():
        budget = quota_usage(base['mount'])[34110]['limit']
        capacity = {'cpus': min(4, os.cpu_count()), 'ram_bytes': 5 * team.GIB,
                    'disk_bytes': budget, 'slots': 4}
        policy = {'mode': 'opportunistic', 'pool': capacity,
                  'users': [{'id': 'owner', 'cpu_percent': 100, 'ram_percent': 100,
                             'disk_percent': 100, 'slots': 4}]}
        line = Path('/home/usevps/.ssh/authorized_keys').read_text().split()
        index = line.index('ssh-ed25519')
        owner_key = team.public_key(' '.join(line[index:index + 2]))
        save(STATE, {'policy': policy, 'capacity': capacity, 'base_application': application, 'owner_public_key': owner_key,
                     'tenants': {'owner': {**team.allocations(policy, capacity)['owner'],
                        'application': application, 'container': base['container'],
                        'workspace': base['workspace'], 'project_id': 34110, 'network': None}}})
    state = read(STATE)
    if state['base_application'] != application:
        raise ValueError('settings already belong to another Coolify application')
    identity('avps-admin', Path(admin_key).read_text(), admin=True, existing=True)
    return {'initialized': True, 'revision': team.revision(state['policy'])}


def coolify(request):
    script = Path(__file__).with_name('coolify_team.js').read_text()
    script += '\nconfigureTeam(' + json.dumps(request) + ').then(x=>{console.log(JSON.stringify(x)); return prisma.$disconnect()}).catch(async e=>{console.error(e.message); await prisma.$disconnect(); process.exit(1)});'
    return json.loads(command(['docker', 'exec', '-i', 'coolify', 'node', '-'], input=script, timeout=120))


def inspect(container):
    result = subprocess.run(['docker', 'inspect', container], capture_output=True, text=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def preview(state, policy):
    base = read(BASE); verify_mount(base)
    limits = team.allocations(policy, state['capacity'])
    keys = [team.public_key(user.get('public_key') or state['owner_public_key']) for user in policy['users']]
    if len(set(keys)) != len(keys):
        raise ValueError('each developer needs a distinct SSH public key, including the owner')
    affected = team.changes(state['policy'], policy)
    usage = quota_usage(base['mount'])
    for name, tenant in state['tenants'].items():
        current = usage[tenant['project_id']]
        if limits[name]['disk_bytes'] < current['used'] or limits[name]['inodes'] < current['inodes']:
            raise ValueError('quota below existing data: ' + name)
    # Existing tools and preserved results count, including disabled developers.
    physical = os.statvfs(base['mount'])
    reusable = sum(usage[t['project_id']]['used'] for t in state['tenants'].values())
    if sum(v['disk_bytes'] for v in limits.values()) > physical.f_bavail * physical.f_frsize + reusable - 512 * team.MIB:
        raise ValueError('disk allocations would consume the physical safety margin')
    if policy['mode'] == 'allocated':
        available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        current_used = 0
        for tenant in state['tenants'].values():
            info = inspect(tenant['container'])
            if info and info['State']['Running']:
                raw = command(['docker', 'exec', tenant['container'], 'cat', '/sys/fs/cgroup/memory.stat'])
                # MemAvailable already credits reclaimable file cache: add back
                # only anonymous memory that these workers would actually release.
                current_used += int(dict(line.split() for line in raw.splitlines())['anon'])
        if sum(v['ram_bytes'] for v in limits.values() if v['enabled']) > available + current_used - 2 * team.GIB:
            raise ValueError('allocated RAM would consume the 2 GiB host floor; reduce the pool or wait')
    return {'policy': policy, 'allocations': limits, 'affected': affected,
            'revision': team.revision(state['policy']), 'candidate_revision': team.revision(policy)}


def rule(program, chain, args):
    if subprocess.run([program, '-C', chain, *args], capture_output=True).returncode:
        command([program, '-A', chain, *args])


def isolate_network(name, container):
    network = 'atelier-vps-' + name
    bridge = 'avps' + team.revision(name)[:8]
    result = subprocess.run(['docker', 'network', 'inspect', network], capture_output=True, text=True)
    if result.returncode:
        command(['docker', 'network', 'create', '--label', 'atelier-vps.tenant=' + name,
                 '--opt', 'com.docker.network.bridge.name=' + bridge,
                 '--opt', 'com.docker.network.bridge.enable_icc=false', network])
    else:
        existing = json.loads(result.stdout)[0]
        if existing.get('Labels', {}).get('atelier-vps.tenant') != name:
            raise ValueError('refusing an unowned network')
    # Rules match only this worker bridge; no existing project's policy is changed.
    rule('iptables', 'INPUT', ['-i', bridge, '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'REJECT'])
    for subnet in ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                   '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/4', '240.0.0.0/4']:
        rule('iptables', 'DOCKER-USER', ['-i', bridge, '-d', subnet, '-j', 'REJECT'])
    rule('ip6tables', 'INPUT', ['-i', bridge, '-j', 'REJECT'])
    rule('ip6tables', 'FORWARD', ['-i', bridge, '-j', 'REJECT'])
    info = inspect(container)
    if network not in info['NetworkSettings']['Networks']:
        command(['docker', 'network', 'connect', network, container])
    for other in info['NetworkSettings']['Networks']:
        if other != network:
            command(['docker', 'network', 'disconnect', other, container])
    return network


def apply(state, policy, expected):
    if expected != team.revision(state['policy']):
        raise ValueError('settings revision changed; preview again')
    plan = preview(state, policy)
    pending = read(PENDING) if PENDING.exists() else {'candidate': plan['candidate_revision'],
                                                     'affected': plan['affected'], 'tenants': state['tenants']}
    if pending['candidate'] != plan['candidate_revision']:
        raise ValueError('another update is pending; resume that exact candidate before changing settings')
    base = read(BASE)
    with ExitStack() as stack:
        # Acquire every affected workspace before changing any quota, app or access.
        for name in plan['affected']:
            if name in state['tenants']:
                lock = stack.enter_context((Path(state['tenants'][name]['workspace']) / '.queue.lock').open('a'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('active work prevents settings update: ' + name) from None
        save(PENDING, pending)
        for user in policy['users']:
            name = user['id']
            if name not in plan['affected']:
                continue
            previous = state['tenants'].get(name)
            target = pending['tenants'].get(name)
            limits = plan['allocations'][name]
            if target is None:
                created = coolify({'action': 'create', 'base_application': state['base_application']})
                target = {'application': created['id'], 'container': created['id'] + '-worker',
                          'workspace': str(Path(base['mount']) / '.usevps-users' / name),
                          'project_id': 34110 + len(pending['tenants']), 'network': None}
                pending['tenants'][name] = target; save(PENDING, pending)
            workspace = Path(target['workspace'])
            if workspace.is_symlink():
                raise ValueError('workspace must be a real private directory')
            workspace.parent.mkdir(mode=0o700, exist_ok=True)
            workspace.mkdir(mode=0o700, exist_ok=True)
            if not previous:
                command(['chattr', '-p', str(target['project_id']), '+P', str(workspace)])
                marker = workspace / '.volume-id'; marker.write_text(base['volume_id'] + '\n'); marker.chmod(0o444)
            os.chown(workspace, 10001, 10001)
            command(['setquota', '-P', str(target['project_id']), '0', str(limits['disk_bytes'] // 1024),
                     '0', str(limits['inodes']), base['mount']])
            request = {'base_application': state['base_application'], 'application': target['application'],
                       'tenant': name, 'workspace': target['workspace'], 'mount': base['mount'],
                       'volume_id': base['volume_id'], 'limits': limits, 'mode': policy['mode']}
            redeploy = (not previous or state['policy']['mode'] != policy['mode'] or
                        any(previous.get(k) != limits[k] for k in ['cpus', 'cpu_shares', 'ram_bytes', 'slots', 'enabled']))
            if limits['enabled'] and redeploy:
                old = inspect(target['container'])
                coolify({**request, 'action': 'configure'})
                coolify({**request, 'action': 'deploy'})
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    current = inspect(target['container'])
                    if current and (not old or current['Id'] != old['Id']) and current['State'].get('Health', {}).get('Status') == 'healthy':
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError('Coolify deployment not healthy; update remains pending: ' + name)
            elif not limits['enabled'] and (not previous or previous.get('enabled')):
                coolify({**request, 'action': 'stop'})
            if limits['enabled'] and policy['mode'] == 'allocated':
                target['network'] = isolate_network(name, target['container'])
            if name != 'owner' or user.get('public_key'):
                identity(limits['ssh_user'], user['public_key'], existing=bool(previous))
            target.update(limits)
            pending['tenants'][name] = target; save(PENDING, pending)
        updated = {**state, 'policy': policy, 'tenants': pending['tenants']}
        # The gateway refuses affected tenants while the transaction is pending.
        save(STATE, updated); PENDING.unlink()
    return {'applied': True, 'revision': team.revision(policy), 'affected': plan['affected']}


def main():
    if os.geteuid() != 0:
        raise PermissionError('administrator authentication required')
    if len(sys.argv) == 4 and sys.argv[1] == '--initialize':
        if Path(sys.argv[3]).suffix != '.pub':
            raise ValueError('administrator public key must be a .pub file')
        print(json.dumps(initialize(sys.argv[2], sys.argv[3]))); return
    if len(sys.argv) == 3 and sys.argv[1] == '--repair-network':
        state = read(STATE)
        name = sys.argv[2]
        if PENDING.exists() or name not in state['tenants'] or state['policy']['mode'] != 'allocated':
            raise ValueError('network repair requires an allocated developer and no pending transaction')
        tenant = state['tenants'][name]
        with (Path(tenant['workspace']) / '.queue.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            network = isolate_network(name, tenant['container'])
            if network != tenant['network']:
                raise ValueError('unexpected repaired network')
        print(json.dumps({'network_repaired': name})); return
    if os.environ.get('SSH_ORIGINAL_COMMAND') != 'settings':
        raise PermissionError('restricted administrator SSH command required')
    request = sys.stdin.buffer.readline(65537)
    if len(request) > 65536:
        raise ValueError('settings request too large')
    request = json.loads(request)
    with Path('/etc/atelier-vps-team.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read(STATE)
        if request['action'] == 'get':
            result = {'revision': team.revision(state['policy']), 'policy': state['policy'],
                      'capacity': state['capacity'], 'tenants': state['tenants'],
                      'pending': read(PENDING) if PENDING.exists() else None}
        elif request['action'] == 'preview':
            result = preview(state, request['policy'])
        elif request['action'] == 'apply':
            result = apply(state, request['policy'], request['expected_revision'])
        else:
            raise ValueError('settings action must be get, preview or apply')
        print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        sys.exit(1)
