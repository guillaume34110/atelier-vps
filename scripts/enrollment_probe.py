"""Real SSH authentication inside a disposable, network-isolated test container."""
import json
import os
from pathlib import Path
import subprocess
import time

import invitations as invites
import install_enrollment as installer
import team
import team_settings as settings


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=15, **kwargs).stdout.strip()


def probe():
    for directory in ['/etc/ssh', '/etc/sudoers.d', '/etc/pam.d', '/run/sshd', '/usr/local/libexec', '/tmp/keys', '/etc/default', '/var/log', '/var/mail']:
        Path(directory).mkdir(parents=True, exist_ok=True)
    for filename, text in {
        'passwd': 'root:x:0:0:root:/root:/bin/sh\nsshd:x:74:74:sshd:/run/sshd:/usr/sbin/nologin\n',
        'group': 'root:x:0:\nsshd:x:74:\n',
        'shadow': 'root:!:19000:0:99999:7:::\nsshd:!:19000:0:99999:7:::\n',
        'gshadow': 'root:!::\nsshd:!::\n', 'nsswitch.conf': 'passwd: files\ngroup: files\nshadow: files\nhosts: files\n',
        'hosts': '127.0.0.1 localhost\n', 'machine-id': 'a' * 32 + '\n',
        'login.defs': 'UID_MIN 1000\nUID_MAX 60000\nGID_MIN 1000\nGID_MAX 60000\nUSERGROUPS_ENAB yes\n',
        'sudoers': 'root ALL=(ALL:ALL) ALL\n@includedir /etc/sudoers.d\n',
    }.items():
        path = Path('/etc') / filename; path.write_text(text)
        path.chmod(0o600 if filename in {'shadow', 'gshadow'} else 0o440 if filename == 'sudoers' else 0o644)
    Path('/etc/pam.d/sudo').write_text('auth required pam_permit.so\naccount required pam_permit.so\nsession required pam_permit.so\n')
    for uid, user in [(10001, 'usevps'), (10002, 'avps-admin')]:
        run(['useradd', '--user-group', '--uid', str(uid), '--create-home', '--shell', '/bin/sh', user])
    for name in ['host', 'primary', 'device']:
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', '/tmp/keys/' + name])
    # Unlock the key-only fixture account; ordinary password authentication stays disabled.
    password_hash = run(['openssl', 'passwd', '-6', '-stdin'], input='unusable-fixture-login\n')
    run(['usermod', '--password', password_hash, 'usevps'])
    # The fixture exercises real password/key authentication and forced commands.
    # Its ordinary gateway is a constant status stub, not a second compute worker.
    gateway = Path('/usr/local/libexec/usevps-gateway')
    gateway.write_text('#!/bin/sh\n[ "$SSH_ORIGINAL_COMMAND" = status ] || exit 1\nprintf \'{"authenticated":true}\\n\'\n')
    gateway.chmod(0o755)
    config = Path('/etc/ssh/sshd_config')
    config.write_text('Port 2222\nListenAddress 127.0.0.1\nHostKey /tmp/keys/host\n'
                      'UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                      'PubkeyAuthentication yes\nPermitRootLogin no\nPidFile /run/test-sshd.pid\nLogLevel ERROR\n')
    pool = {'cpus': 4, 'ram_bytes': 5 * team.GIB, 'disk_bytes': 20 * team.GIB, 'slots': 4}
    policy = {'mode': 'opportunistic', 'pool': pool, 'users': [
        {'id': 'owner', 'cpu_percent': 100, 'ram_percent': 100, 'disk_percent': 100, 'slots': 4}]}
    primary = Path('/tmp/keys/primary.pub').read_text().strip()
    public = Path('/tmp/keys/device.pub').read_text().strip()
    public = team.public_key(public)
    state = {'policy': policy, 'owner_public_key': team.public_key(primary),
             'tenants': {'owner': {'enabled': True, 'ssh_user': 'usevps'}}}
    settings.save(settings.STATE, state)
    original_command = settings.command
    def no_systemd(argv, **kwargs):
        if argv == ['systemctl', 'reload', 'ssh']:
            return ''
        return original_command(argv, **kwargs)
    settings.command = no_systemd
    installer.install()
    settings.identity('usevps', primary, existing=True)
    own_keys = Path('/home/usevps/.ssh/authorized_keys')
    primary_line = own_keys.read_text()
    before_state = settings.STATE.read_bytes()
    invitation = invites.issue(state, 'owner', team.revision(policy))
    host_key = ' '.join(Path('/tmp/keys/host.pub').read_text().split()[:2])
    known = Path('/tmp/keys/known_hosts'); known.write_text('test-enrollment ' + host_key + '\n')
    askpass = Path('/tmp/keys/askpass')
    askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$ATELIER_TEST_PASSWORD"\n'); askpass.chmod(0o700)
    env = {**os.environ, 'DISPLAY': 'test', 'SSH_ASKPASS': str(askpass), 'SSH_ASKPASS_REQUIRE': 'force',
           'ATELIER_TEST_PASSWORD': invitation['password']}
    ssh = ['ssh', '-F', '/dev/null', '-S', 'none', '-T', '-p', '2222',
           '-o', 'HostKeyAlias=test-enrollment', '-o', 'UserKnownHostsFile=' + str(known),
           '-o', 'StrictHostKeyChecking=yes', '-o', 'NumberOfPasswordPrompts=1', '-o', 'LogLevel=ERROR',
           '-o', 'ConnectTimeout=3']
    log = Path('/tmp/sshd.log').open('w')
    daemon = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e'], stdout=log, stderr=log)
    try:
        for _ in range(40):
            ready = subprocess.run(['ssh-keyscan', '-T', '1', '-p', '2222', '127.0.0.1'], capture_output=True, timeout=3)
            if ready.returncode == 0: break
            if daemon.poll() is not None:
                raise RuntimeError('test SSH daemon failed: ' + Path('/tmp/sshd.log').read_text()[-1200:])
            time.sleep(.1)
        else: raise RuntimeError('real SSH service did not start')
        def request(user, operation, body='', key=None):
            auth = ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-i', key] if key else [
                '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password']
            return subprocess.run([*ssh, *auth, user + '@127.0.0.1', operation], env=env,
                                  input=body, capture_output=True, text=True, timeout=12)
        assert request('usevps', 'status', key='/tmp/keys/primary').returncode == 0
        assert request('usevps', 'status', key='/tmp/keys/device').returncode != 0
        assert request(invitation['principal'], 'touch /tmp/unexpected-shell').returncode != 0
        assert not Path('/tmp/unexpected-shell').exists()
        known.write_text('test-enrollment ' + team.public_key(primary) + '\n')
        assert request(invitation['principal'], 'enroll', json.dumps({'public_key': public}) + '\n').returncode != 0
        assert not settings.read(invites.record_path(invitation['principal']))['used']
        known.write_text('test-enrollment ' + host_key + '\n')
        result = request(invitation['principal'], 'enroll', json.dumps({'public_key': public}) + '\n')
        if result.returncode:
            raise AssertionError('real SSH enrollment failed: ' + result.stderr[-1200:] + ' ' + Path('/tmp/sshd.log').read_text()[-1200:])
        assert json.loads(result.stdout)['enrolled'] is True
        assert request('usevps', 'status', key='/tmp/keys/device').returncode == 0
        assert request('usevps', 'status', key='/tmp/keys/primary').returncode == 0
        assert request('usevps', 'touch /tmp/unexpected-shell', key='/tmp/keys/device').returncode != 0
        assert not Path('/tmp/unexpected-shell').exists()
        assert request(invitation['principal'], 'enroll', json.dumps({'public_key': public}) + '\n').returncode != 0
        assert own_keys.read_text().startswith(primary_line)
        assert settings.STATE.read_bytes() == before_state
        installer.verify(invitation['principal'])
        print(json.dumps({'real_ssh_enrollment': True, 'one_use_replay_denied': True, 'ordinary_shell_denied': True,
                          'primary_access_preserved': True, 'existing_ssh_policies_preserved': True,
                          'resource_policy_unchanged': True, 'ordinary_status': 'constant fixture; no compute worker',
                          'private_keys_transferred': False}))
    finally:
        daemon.terminate(); daemon.wait(timeout=10); log.close()


if __name__ == '__main__':
    probe()
