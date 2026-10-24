"""Real private mounts, ext4 quotas and cgroup OOM containment in disposable fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import vps


def verify_team_isolation(root, dev, remote_host, volume, command, execute, payload):
    token = uuid.uuid4().hex[:10]
    base = (volume if remote_host else root) / ('.usevps-team-qa-' + token)
    names = ['avps-qa-' + label + '-' + token for label in ['alice', 'bob']]
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    def host(code):
        if remote_host:
            return command(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', remote_host, 'python3', '-'], input=code.encode())
        return command([*prefix, sys.executable, '-c', code])
    setup = f'''
from pathlib import Path
import os,subprocess
p=Path({str(base)!r}); p.mkdir(); (p/'owned').write_text('atelier-vps-team-ci')
image=p/'quota.img'; subprocess.run(['truncate','-s','768M',str(image)],check=True)
subprocess.run(['mkfs.ext4','-q','-F',str(image)],check=True)
subprocess.run(['tune2fs','-O','project,quota','-Q','prjquota',str(image)],check=True)
m=p/'volume';m.mkdir();subprocess.run(['mount','-o','loop,prjquota',str(image),str(m)],check=True)
for number,name in enumerate(['alice','bob'],1):
 w=m/name;w.mkdir();subprocess.run(['chattr','-p',str(number),'+P',str(w)],check=True)
 subprocess.run(['setquota','-P',str(number),'0','16384','0','4096',str(m)],check=True)
 (w/'.volume-id').write_text('ci-team');os.chown(w,10001,10001)
probe=m/'probe';probe.mkdir();probe.chmod(0)
'''
    try:
        host(setup)
        mounts = []
        for destination, source in {'/usr': 'usr', '/bin': 'usr/bin', '/sbin': 'usr/sbin', '/lib': 'usr/lib',
                                    '/lib64': 'usr/lib64', '/etc': 'etc', '/opt': 'opt'}.items():
            mounts.extend(['--volume', str(dev / source) + ':' + destination + ':ro'])
        for label, name in zip(['alice', 'bob'], names):
            command(['docker', 'run', '--detach', '--name', name, '--network=none', '--read-only',
                     '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--memory=128m',
                     '--memory-swap=128m', '--cpus=0.5', '--pids-limit=64', *mounts,
                     '--volume', str(base / 'volume' / label) + ':/workspace',
                     '--volume', str(base / 'volume/probe') + ':/volume-stat:ro',
                     '--env', 'USEVPS_VOLUME_ID=ci-team', '--env', 'USEVPS_VOLUME_STATS=/volume-stat',
                     '--env', 'USEVPS_MODE=allocated', '--env', 'USEVPS_TENANT=' + label,
                     '--env', 'USEVPS_MAX_JOBS=1', '--env', 'USEVPS_CPU_LIMIT=0.5', 'usevps:checks'])
            for _ in range(40):
                ready = subprocess.run(['docker', 'exec', name, 'python3', '-c', "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"], capture_output=True)
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError('isolated worker health unavailable')
        own = json.loads(execute(names[0], 'status'))
        assert own['scope']['id'] == 'alice' and 'host_total_ram' not in own['telemetry'], own
        assert own['budget_bytes'] <= 16 * 1024**2, own
        assert own['telemetry']['worker_cpu_limit'] == 0.5, own
        forbidden = str(base / 'volume/bob')
        check = ('from pathlib import Path; import os,fcntl,errno\nassert not Path(' + repr(forbidden)
                 + ").exists(); assert not Path('/workspace/../bob').exists()\n"
                 + "Path('quota-protected').touch()\n"
                 + "for path,request,size in [('quota-protected',0x401c5820,28),('/workspace',0x40086602,8)]:\n"
                 + " fd=os.open(path,os.O_RDONLY)\n try: fcntl.ioctl(fd,request,bytes(size))\n"
                 + " except OSError as error: assert error.errno==errno.EPERM,error\n"
                 + " else: raise AssertionError('quota attributes were writable')\n finally: os.close(fd)\n")
        private = json.loads(execute(names[0], 'run', payload(['python3', '-c', check], ram_bytes=64 * 1024**2, disk_bytes=1024**2)))
        assert private['status'] == 'passed', private
        execute(names[0], 'release', json.dumps({'id': private['id']}).encode() + b'\n')
        # Exercise the kernel quota independently of the earlier sampled job guard.
        disk_probe = """from pathlib import Path
import errno
from quota_protection import protect_quota
protect_quota()
p=Path('/workspace/kernel-quota-probe')
assert not p.exists()
try:
 with p.open('wb') as target:
  for _ in range(32): target.write(b'x'*1024**2)
except OSError as error:
 assert error.errno==errno.EDQUOT,error
 print('kernel quota denied')
else: raise AssertionError('hard project quota was bypassed')
finally: p.unlink(missing_ok=True)
"""
        denied_disk = command(['docker', 'exec', '--user', '10001:10001', names[0], 'python3', '-c', disk_probe])
        assert b'kernel quota denied' in denied_disk, denied_disk[-1000:]
        bob_before = json.loads(command(['docker', 'inspect', names[1]]))[0]['State']['Pid']
        from ops.gateway import WORKER_EXEC
        bob = subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', names[1], *WORKER_EXEC, 'run'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            bob.stdin.write(payload(['python3', '-c', "import time; from pathlib import Path\nwhile not Path('finish').exists(): time.sleep(0.1)\nPath('survived.txt').write_text('ok')"],
                                    agent='Bob private task', ram_bytes=64 * 1024**2, disk_bytes=1024**2, timeout=60))
            bob.stdin.close(); bob.stdin = None
            for _ in range(30):
                active = json.loads(execute(names[1], 'status'))['pool']['active']
                if active:
                    break
                time.sleep(0.1)
            assert active, 'Bob real task did not start'
            denied = subprocess.run(['docker', 'exec', '-i', '--user', '10001:10001', names[0], *WORKER_EXEC, 'fetch'],
                                    input=json.dumps({'id': active[0]['id'], 'paths': ['receipt.json']}).encode() + b'\n', capture_output=True)
            assert denied.returncode != 0 and b'Bob private task' not in denied.stdout + denied.stderr
            oom = subprocess.run(['docker', 'exec', '--user', '10001:10001', names[0], 'python3', '-c',
                                  'x=bytearray(256*1024**2); print(len(x))'], capture_output=True, timeout=30)
            assert oom.returncode != 0, 'Alice exceeded its cgroup without an OOM'
            command(['docker', 'exec', names[1], 'python3', '-c', "from pathlib import Path; Path('/workspace/jobs/" + active[0]['id'] + "/work/finish').touch()"])
            out, errors = bob.communicate(timeout=45)
            assert bob.returncode == 0, errors[-1000:]
            survived = json.loads(out)
            assert survived['status'] == 'passed', survived
            after = json.loads(command(['docker', 'inspect', names[1]]))[0]
            assert after['State']['Running'] and after['State']['Pid'] == bob_before, after['State']
            verified = json.loads(execute(names[1], 'status'))
            assert all(item['agent'] != private['agent'] for item in verified['telemetry']['activity'])
            archive = root / ('team-' + token + '.tar')
            archive.write_bytes(execute(names[1], 'fetch', json.dumps({'id': survived['id'], 'paths': ['work/survived.txt', 'receipt.json']}).encode() + b'\n'))
            destination = root / ('team-' + token)
            assert vps.unpack_result(archive, destination) == 2
            assert (destination / 'work/survived.txt').read_text() == 'ok'
            execute(names[1], 'release', json.dumps({'id': survived['id']}).encode() + b'\n')
            (root / 'evidence/team-isolation.json').write_text(json.dumps({'private_mounts': True,
                'cross_user_fetch_denied': True, 'ext4_quota_bytes': own['budget_bytes'],
                'quota_exceeded': True, 'quota_attribute_changes_denied': True,
                'alice_oom_exit': oom.returncode, 'bob_survived': True,
                'cpu_limit': 0.5, 'verified_result_files': 2}))
            shutil.rmtree(destination); archive.unlink()
        finally:
            if bob.poll() is None:
                bob.terminate(); bob.communicate(timeout=10)
    finally:
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
        cleanup = f'''
from pathlib import Path
import os,shutil,subprocess
p=Path({str(base)!r})
assert p.name.startswith('.usevps-team-qa-') and (p/'owned').read_text()=='atelier-vps-team-ci'
m=p/'volume'
if os.path.ismount(m): subprocess.run(['umount',str(m)],check=True)
shutil.rmtree(p)
'''
        host(cleanup)
