"""Real desktop API/RFB checks; only non-sensitive counters are public evidence."""
import hashlib
import json
from pathlib import Path
import re
import time

import vps


def relocate_cache(state, workspace, owner, remote_host, command, restore=False):
    """Rename one identified software scope; profiles never enter this scope."""
    if not state or not remote_host:
        return
    relative = state['relative']
    if not re.fullmatch('cache/managed/[a-f0-9]{64}', relative):
        raise ValueError('invalid desktop software scope')
    data = {'work': str(workspace), 'owner': str(owner), 'relative': relative,
            'restore': restore, 'spec': state['spec']}
    script = '''
import json,os,subprocess,sys
from pathlib import Path
value=INPUT
work=Path(value['work']);owner=Path(value['owner']);rel=Path(value['relative'])
assert work.parent==owner and work.name.startswith('.usevps-qa-') and not work.is_symlink()
assert (work/'.volume-id').read_text().strip()=='ci-volume'
base=json.loads(Path('/etc/usevps-gateway.json').read_text())
assert Path(base['workspace'])==owner and (owner/'.volume-id').read_text().strip()==base['volume_id']
assert owner.stat().st_dev==work.stat().st_dev
sys.path.insert(0,'/usr/local/lib/usevps')
from gateway import WORKER_EXEC
current=json.loads(subprocess.check_output(['docker','exec','--user','10001:10001',base['container'],*WORKER_EXEC,'status'],timeout=20))
assert current['ok'] and current['maintenance'] and current['pool']['active_count']==0, 'hold your canonical idle-worker maintenance lock'
for base in [owner,work]:
 for tail in ['cache','cache/managed']:
  assert not (base/tail).is_symlink()
os.setgroups([]);os.setgid(10001);os.setuid(10001)
source,destination=(work/rel,owner/rel) if value['restore'] else (owner/rel,work/rel)
if source.exists():
 assert source.is_dir() and not source.is_symlink() and source.stat().st_uid==10001
 assert source.resolve().is_relative_to(source.parents[2].resolve())
 marker=source/'.retention.json'
 assert marker.is_file() and not marker.is_symlink() and marker.stat().st_size<=256
 assert json.loads(marker.read_text()).get('version')==1
 installed=source/'desktop/installed.json'
 assert not installed.is_symlink()
 if installed.exists():
  assert installed.is_file() and installed.stat().st_size<=262144
  assert json.loads(installed.read_text())['spec']==value['spec']
 assert not destination.exists() and not destination.is_symlink(), 'both cache locations preserved; inspect before cleanup'
 destination.parent.mkdir(parents=True,exist_ok=True)
 source.rename(destination)
 print(json.dumps({'software_cache_renamed':True,'restored':value['restore']}))
else:
 print(json.dumps({'software_cache_renamed':False,'restored':value['restore']}))
'''.replace('INPUT', repr(data))
    command(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
             '-o', 'ServerAliveInterval=15', remote_host, 'python3', '-'], input=script.encode(), timeout=30)


def verify_desktops(root, name, workspace, deployment, remote_host, command, execute, state):
    metadata = json.loads(command(['docker', 'exec', name, 'python3', '-c',
        'import desktop_runtime,json;from pathlib import Path;p,s=desktop_runtime.location(Path("/workspace"));'
        'print(json.dumps({"relative":p.parent.relative_to("/workspace").as_posix(),"spec":s}))']))
    state.update(metadata)  # Keep recovery coordinates even after an interrupted SSH response.
    owner = Path(deployment['workspace']) if remote_host else None
    relocate_cache(state, workspace, owner, remote_host, command)
    evidence = root / 'evidence'
    for provider in ['chatgpt', 'claude']:
        started = json.loads(execute(name, 'desktop', json.dumps({'action': 'start', 'provider': provider}).encode() + b'\n'))
        ident = started['id']; assert re.fullmatch('[a-f0-9]{32}', ident)
        error = None
        try:
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                current = json.loads(execute(name, 'desktop', b'{"action":"status"}\n'))
                assert current['id'] == ident and current['provider'] == provider
                if not current['active']:
                    raise RuntimeError(provider + ' closed before its GUI became ready')
                if current['phase'] == 'ready':
                    break
                time.sleep(2)
            else:
                raise TimeoutError(provider + ' did not become ready within the GUI setup budget')
            pool = json.loads(execute(name, 'status'))['pool']
            assert any(job['id'] == ident for job in pool['active'])
            again = json.loads(execute(name, 'desktop', json.dumps({'action': 'start', 'provider': provider}).encode() + b'\n'))
            assert again['id'] == ident, 'reopening must reuse the existing reservation'
            command(['docker', 'exec', '--user', '10001:10001', name, 'python3',
                     '/app/desktop_probe.py', '--capture-session', ident], timeout=90)
        except Exception as exc:
            error = exc
            (root / ('desktop-' + provider + '-failure.txt')).write_text(type(exc).__name__ + ': ' + str(exc)[:2000])
        finally:
            current = json.loads(execute(name, 'desktop', b'{"action":"status"}\n'))
            if current.get('id') == ident and current.get('active'):
                execute(name, 'desktop', json.dumps({'action': 'stop', 'id': ident}).encode() + b'\n')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                closed = json.loads(execute(name, 'desktop', b'{"action":"status"}\n'))
                if not closed['active']:
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('owned CI desktop did not stop; preserve its workspace')
        paths = ['receipt.json', 'stdout.log']
        if error is None:
            paths += ['work/desktop.png', 'work/desktop-proof.json']
        archive = root / ('desktop-' + provider + '.tar')
        archive.write_bytes(execute(name, 'fetch', json.dumps({'id': ident, 'paths': paths}).encode() + b'\n'))
        private = root / ('desktop-' + provider + '-private')
        vps.unpack_result(archive, private)
        receipt = json.loads((private / 'receipt.json').read_text())
        if error is None:
            assert receipt['status'] == 'passed', receipt
            proof = json.loads((private / 'work/desktop-proof.json').read_text())
            assert proof['status'] == 'passed' and proof['provider'] == provider and proof['vnc_password_required']
            assert proof['transport'] == 'worker desktop stdio bridge' and not proof['provider_sign_in_verified']
            assert hashlib.sha256((private / 'work/desktop.png').read_bytes()).hexdigest() == proof['image_sha256']
            proof['ram_peak_bytes'] = receipt['resources']['ram_peak_bytes']
            proof['ram_reserved_bytes'] = receipt['reservation']['ram_bytes']
            # Welcome windows and raw logs stay private; never upload account screens.
            (evidence / ('desktop-' + provider + '.json')).write_text(json.dumps(proof, indent=2))
        execute(name, 'release', json.dumps({'id': ident}).encode() + b'\n')
        archive.unlink()
        if error is not None:
            raise RuntimeError(provider + ' failed; its verified private receipt is preserved in ' + str(private)) from error
        print(provider + ': real program startup, reserved resources, authenticated RFB bridge and clean stop passed', flush=True)
