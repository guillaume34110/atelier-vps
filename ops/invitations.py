"""One-device enrollment over the existing SSH listener; never grants a shell."""
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import sys
import time

try:
    from . import team_settings as settings, team
    from .gateway import server_identity
except ImportError:
    import team_settings as settings, team
    from gateway import server_identity

ROOT = Path('/var/lib/atelier-vps-invitations')
DEVICES = Path('/etc/atelier-vps-devices.json')
GROUP = 'atelier-enroll'
ENTRY = '/usr/local/libexec/atelier-enroll-entry'
HELPER = '/usr/local/libexec/atelier-enroll'
TTL = 1800
MAX_DEVICES = 8
CONFIG = Path('/etc/atelier-vps-enrollment.sshd')


def storage_available():
    volume = os.statvfs(ROOT)
    if volume.f_bavail * volume.f_frsize - volume.f_blocks * volume.f_frsize * .1 < 65536:
        raise ValueError('system disk reserve prevents a new enrollment')


def devices():
    value = settings.read(DEVICES) if DEVICES.exists() else {}
    if not isinstance(value, dict) or len(value) > 4:
        raise ValueError('invalid device registry')
    for user, keys in value.items():
        if not re.fullmatch(r'(usevps|avps-[a-z][a-z0-9-]{0,15})', user) or not isinstance(keys, list) or len(keys) > MAX_DEVICES:
            raise ValueError('invalid device registry')
        for key in keys:
            team.public_key(key)
    return value


def additional_keys(user):
    return devices().get(user, [])


def record_path(principal):
    if not re.fullmatch('avpi-[a-f0-9]{24}', principal):
        raise PermissionError('a dedicated enrollment identity is required')
    return ROOT / (principal + '.json')


def verify_principal(principal, record):
    account = pwd.getpwnam(principal)
    if (account.pw_uid != record['uid'] or account.pw_gid != grp.getgrnam(GROUP).gr_gid
            or account.pw_dir != '/nonexistent' or account.pw_shell != ENTRY):
        raise PermissionError('enrollment identity ownership mismatch')
    return account


def cleanup():
    """Only this service's expired enrollment identities; no homes or job data."""
    if not ROOT.is_dir() or ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
        raise ValueError('enrollment storage must be private and root-owned')
    for path in ROOT.glob('avpi-*.json'):
        record = settings.read(path)
        if not record.get('used') and record['expires'] > time.time():
            continue
        principal = path.stem; record_path(principal)
        try:
            account = verify_principal(principal, record)
        except KeyError:
            path.unlink(); continue
        settings.command(['usermod', '--lock', principal])
        # The SSH process returning the enrollment receipt must finish normally.
        live = False
        for process in Path('/proc').iterdir():
            try:
                live = live or (process.name.isdigit() and process.stat().st_uid == account.pw_uid)
            except FileNotFoundError:
                pass
        if not live:
            settings.command(['userdel', principal])
            path.unlink()


def issue(state, member, expected_revision):
    storage_available()
    if expected_revision != team.revision(state['policy']) or settings.PENDING.exists():
        raise ValueError('reload applied settings before creating an invitation')
    users = [u for u in state['policy']['users'] if u['id'] == member and u.get('enabled', True)]
    tenant = state['tenants'].get(member, {})
    if len(users) != 1 or not tenant.get('enabled'):
        raise ValueError('apply an enabled member and its resource allocation first')
    if len(additional_keys(tenant['ssh_user'])) >= MAX_DEVICES:
        raise ValueError('member device limit reached')
    if not CONFIG.is_file():
        raise ValueError('install the enrollment gateway under the deployment lock first')
    cleanup()
    if len(list(ROOT.glob('avpi-*.json'))) >= 8:
        raise ValueError('at most eight outstanding invitations are allowed; wait for expiration')
    principal = 'avpi-' + secrets.token_hex(12)
    path = record_path(principal)
    try:
        pwd.getpwnam(principal)
        raise ValueError('enrollment identity collision; existing account preserved')
    except KeyError:
        pass
    password = secrets.token_urlsafe(32)
    password_hash = settings.command(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n', timeout=5)
    settings.command(['useradd', '--no-create-home', '--home-dir', '/nonexistent', '--shell', ENTRY,
                      '--gid', GROUP, '--password', '!', principal])
    account = pwd.getpwnam(principal)
    expires = int(time.time()) + TTL
    record = {'uid': account.pw_uid, 'member': member, 'revision': expected_revision,
              'server_id': server_identity(), 'expires': expires, 'used': False, 'public_key': None}
    try:
        settings.save(path, record)
        try:
            from .install_enrollment import verify
        except ImportError:
            from install_enrollment import verify
        verify(principal)
        settings.command(['chpasswd', '--encrypted'], input=principal + ':' + password_hash + '\n', timeout=5)
    except Exception:
        settings.command(['usermod', '--lock', principal])
        raise
    return {'principal': principal, 'password': password, 'expires': expires,
            'member': member, 'user': tenant['ssh_user'], 'server_id': record['server_id']}


def enroll(principal, public):
    storage_available()
    public = team.public_key(public)
    path = record_path(principal); record = settings.read(path)
    verify_principal(principal, record)
    if record['server_id'] != server_identity() or record['expires'] <= time.time():
        raise PermissionError('invitation expired; request another link')
    if record['used'] or record['public_key'] not in (None, public):
        raise PermissionError('invitation already bound to another device')
    state = settings.read(settings.STATE)
    if settings.PENDING.exists() or record['revision'] != team.revision(state['policy']):
        raise PermissionError('team settings changed; request another invitation')
    member = record['member']; tenant = state['tenants'].get(member, {})
    user = next((u for u in state['policy']['users'] if u['id'] == member and u.get('enabled', True)), None)
    if user is None or not tenant.get('enabled'):
        raise PermissionError('member is disabled')
    ledger = devices(); keys = ledger.get(tenant['ssh_user'], [])
    assigned = [state['owner_public_key']]
    assigned += [u['public_key'] for u in state['policy']['users'] if u['id'] != member and u.get('public_key')]
    assigned += [k for name, values in ledger.items() if name != tenant['ssh_user'] for k in values]
    primary = user.get('public_key') or state['owner_public_key']
    if public in assigned and public != primary:
        raise PermissionError('device key belongs to a different member')
    if public not in keys and len(keys) >= MAX_DEVICES:
        raise ValueError('member device limit reached')
    record['public_key'] = public; settings.save(path, record)
    if public not in keys:
        ledger[tenant['ssh_user']] = keys + [public]; settings.save(DEVICES, ledger)
    settings.identity(tenant['ssh_user'], primary, existing=True)
    record['used'] = True; settings.save(path, record)
    settings.command(['usermod', '--lock', principal])
    return {'enrolled': True, 'member': member, 'user': tenant['ssh_user'],
            'server_id': record['server_id'], 'public_key': public}


def main():
    if os.geteuid() != 0 or os.environ.get('SSH_ORIGINAL_COMMAND') != 'enroll':
        raise PermissionError('restricted enrollment command required')
    def timeout(signum, frame):
        raise TimeoutError('enrollment input timed out')
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(10)
    try:
        raw = sys.stdin.buffer.readline(4097)
        signal.alarm(20)
        if len(raw) > 4096:
            raise ValueError('enrollment request too large')
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {'public_key'}:
            raise ValueError('only a public device key is accepted')
        import fcntl
        with Path('/etc/atelier-vps-team.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(json.dumps(enroll(os.environ.get('SUDO_USER', ''), request['public_key'])))
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Enrollment responses never disclose a token, account state or project data.
        print(json.dumps({'error': 'enrollment refused; verify the invitation with its administrator'}), file=sys.stderr)
        sys.exit(1)
