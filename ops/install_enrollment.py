"""Install enrollment for a dedicated SSH group without changing other logins."""
import grp
import os
from pathlib import Path
import pwd

try:
    from . import invitations as invites, team_settings as settings
except ImportError:
    import invitations as invites, team_settings as settings

CONFIG = invites.CONFIG
MAIN = Path('/etc/ssh/sshd_config')
TAIL = '\n# Atelier VPS enrollment (dedicated group only)\nMatch all\nInclude ' + str(CONFIG) + '\n'
CONFIG_TEXT = f'''# Managed by Atelier VPS: temporary enrollment identities only.
Match Group {invites.GROUP}
    AllowUsers avpi-*
    AuthenticationMethods password
    PasswordAuthentication yes
    KbdInteractiveAuthentication no
    PubkeyAuthentication no
    ForceCommand {invites.ENTRY}
    DisableForwarding yes
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    X11Forwarding no
    MaxAuthTries 2
'''


def policy(user):
    text = settings.command(['/usr/sbin/sshd', '-T', '-C', f'user={user},host=localhost,addr=127.0.0.1'])
    return dict(line.split(' ', 1) for line in text.splitlines() if ' ' in line)


def verify(user):
    current = policy(user)
    expected = {'allowusers': 'avpi-*', 'authenticationmethods': 'password', 'passwordauthentication': 'yes',
                'pubkeyauthentication': 'no', 'kbdinteractiveauthentication': 'no',
                'forcecommand': invites.ENTRY, 'disableforwarding': 'yes', 'permittty': 'no',
                'permittunnel': 'no', 'permituserrc': 'no', 'x11forwarding': 'no', 'maxauthtries': '2'}
    if any(current.get(k) != v for k, v in expected.items()):
        raise ValueError('SSH policy does not isolate this enrollment identity')


def install():
    if os.geteuid() != 0:
        raise PermissionError('root installation under the deployment lock required')
    invites.ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if invites.ROOT.is_symlink() or invites.ROOT.stat().st_uid != 0 or invites.ROOT.stat().st_mode & 0o077:
        raise ValueError('invitation directory must be private and root-owned')
    marker = invites.ROOT / 'installation.json'
    try:
        group = grp.getgrnam(invites.GROUP)
        if not marker.exists() or settings.read(marker) != {'gid': group.gr_gid}:
            raise ValueError('refusing to adopt an existing enrollment group')
    except KeyError:
        settings.command(['groupadd', '--system', invites.GROUP])
        group = grp.getgrnam(invites.GROUP)
        settings.save(marker, {'gid': group.gr_gid})
    if group.gr_mem or any(p.pw_gid == group.gr_gid and not p.pw_name.startswith('avpi-') for p in pwd.getpwall()):
        raise ValueError('unexpected account in the enrollment group')
    state = settings.read(settings.STATE) if settings.STATE.exists() else {'tenants': {}}
    users = {'root', 'usevps', 'avps-admin'} | {t['ssh_user'] for t in state['tenants'].values()}
    users |= {p.pw_name for p in pwd.getpwall() if p.pw_shell not in {invites.ENTRY, '/bin/false', '/usr/bin/false'}
              and not p.pw_shell.endswith('/nologin')}
    before = {user: policy(user) for user in users}
    previous = None
    if MAIN.is_symlink() or MAIN.stat().st_uid != 0 or MAIN.stat().st_mode & 0o022:
        raise ValueError('SSH configuration must be a root-owned regular file')
    previous_main = MAIN.read_text()
    if '# Atelier VPS enrollment' in previous_main and not previous_main.endswith(TAIL):
        raise ValueError('enrollment include was modified; existing SSH configuration preserved')
    if CONFIG.exists():
        previous = CONFIG.read_text()
        if CONFIG.is_symlink() or CONFIG.stat().st_uid != 0 or previous != CONFIG_TEXT:
            raise ValueError('refusing to overwrite an unmanaged SSH rule')
    entry = Path(invites.ENTRY)
    entry.write_text('#!/bin/sh\nexec sudo -n ' + invites.HELPER + '\n'); entry.chmod(0o755)
    helper = Path(invites.HELPER)
    helper.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/usr/local/lib/usevps")\n'
                      'import runpy\nrunpy.run_module("invitations",run_name="__main__")\n'); helper.chmod(0o755)
    sudo = Path('/etc/sudoers.d/atelier-enroll')
    temporary = sudo.with_suffix('.tmp')
    temporary.write_text(f'Defaults:%{invites.GROUP} env_keep += "SSH_ORIGINAL_COMMAND"\n'
                         f'%{invites.GROUP} ALL=(root) NOPASSWD: {invites.HELPER} ""\n')
    temporary.chmod(0o440)
    settings.command(['visudo', '-cf', str(temporary)])
    temporary.replace(sudo)
    try:
        CONFIG.write_text(CONFIG_TEXT); CONFIG.chmod(0o644)
        # A Match block in an early sshd_config.d include can invalidate later global directives.
        # Append this include at EOF and verify every existing login before reloading.
        if not previous_main.endswith(TAIL):
            MAIN.write_text(previous_main + TAIL)
        settings.command(['/usr/sbin/sshd', '-t'])
        if {user: policy(user) for user in users} != before:
            raise ValueError('enrollment configuration changed an existing SSH login; installation rolled back')
        # Reload retains all established SSH sessions and running calculations.
        settings.command(['systemctl', 'reload', 'ssh'])
    except Exception:
        if previous is None:
            CONFIG.unlink(missing_ok=True)
        else:
            CONFIG.write_text(previous)
        MAIN.write_text(previous_main)
        raise
    return {'enrollment_installed': True, 'existing_logins_unchanged': sorted(users)}
