"""Run SSH enrollment in a small temporary container, without ports or host writes."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import uuid


def verify_enrollment(root, command, image, remote_host=None):
    repo = Path(__file__).resolve().parents[1]
    name = 'atelier-enrollment-check-' + uuid.uuid4().hex[:10]
    mount_paths = ['/usr', '/bin', '/sbin', '/lib']
    if remote_host:
        extra = command(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', remote_host,
                         'python3', '-c', '"import os; print(int(os.path.exists(\'/lib64\')))"']).strip()
        if extra == b'1': mount_paths.append('/lib64')
    elif Path('/lib64').exists():
        mount_paths.append('/lib64')
    mounts = [arg for path in mount_paths for arg in ['--volume', path + ':' + path + ':ro']]
    tmpfs = [arg for path, size in [('/etc', 16), ('/usr/local', 16), ('/run', 4), ('/tmp', 32), ('/var', 8), ('/home', 8)]
             for arg in ['--tmpfs', f'{path}:rw,nosuid,nodev,{"exec" if path in {"/tmp", "/usr/local"} else "noexec"},size={size}m']]
    # sudo's root transition is the behavior under test; the compute worker keeps no-new-privileges.
    capabilities = [arg for cap in ['SETUID', 'SETGID', 'SYS_CHROOT', 'CHOWN', 'FOWNER', 'DAC_OVERRIDE', 'AUDIT_WRITE']
                    for arg in ['--cap-add', cap]]
    try:
        command(['docker', 'run', '-d', '--rm', '--name', name, '--user', '0:0', '--read-only', '--network', 'none',
                 '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--cpu-shares', '128', '--oom-score-adj', '500',
                 '--pids-limit', '64', '--cap-drop', 'ALL',
                 *capabilities, *mounts, *tmpfs, '--entrypoint', '/usr/bin/python3', image,
                 '-c', 'import time; time.sleep(180)'])
        files = ['team.py', 'team_settings.py', 'gateway.py', 'deploy_lock.py', 'invitations.py', 'install_enrollment.py']
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode='w') as tar:
            for filename in files:
                tar.add(repo / 'ops' / filename, arcname=filename)
            tar.add(repo / 'scripts/enrollment_probe.py', arcname='enrollment_probe.py')
        unpack = ('import sys,tarfile; from pathlib import Path; p=Path("/usr/local/lib/usevps"); '
                  'p.mkdir(parents=True); t=tarfile.open(fileobj=sys.stdin.buffer,mode="r|*"); t.extractall(p,filter="data")')
        command(['docker', 'exec', '-i', name, 'python3', '-c', unpack], input=bundle.getvalue())
        proof = command(['docker', 'exec', name, 'python3', '/usr/local/lib/usevps/enrollment_probe.py'], timeout=130)
        value = json.loads(proof)
        assert value['real_ssh_enrollment'] and value['one_use_replay_denied'] and value['primary_access_preserved']
        (root / 'evidence/enrollment-auth.json').write_text(json.dumps(value, indent=2))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
