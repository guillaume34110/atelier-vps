"""The complete CI suite, including a real running container and file round trip."""
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import vps
import projects
from scripts import desktop_checks
from ops.gateway import WORKER_EXEC, DESKTOP_TMPFS
from ops.render_compose import bindings
from ops.coolify_cloud import compose as cloud_compose


def command(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(str(argv[:4]) + ": " + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout


def execute(container, action, body=b"", success=True):
    r = subprocess.run(["docker", "exec", "-i", "--user", "10001:10001", container,
                        *WORKER_EXEC, action], input=body, capture_output=True, timeout=180)
    if success and r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace")[-2000:] + r.stdout.decode(errors="replace")[-1000:])
    if not success and r.returncode == 0:
        raise AssertionError("expected operation to fail")
    return r.stdout


def payload(argv, files=None, *, include_default=True, **options):
    request = {"argv": argv, "disk_bytes": 16 * 1024 ** 2, "timeout": 20, **options}
    out = io.BytesIO()
    out.write(json.dumps(request).encode() + b"\n")
    with tarfile.open(fileobj=out, mode="w") as tar:
        if include_default:
            data = b"21\n"
            info = tarfile.TarInfo("input.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, text in (files or {}).items():
            content = text.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    return out.getvalue()


def verify_budgeted_transfers(container, root, evidence):
    with tempfile.TemporaryDirectory(dir=root) as fixture:
        fixture = Path(fixture)
        source = fixture / 'source.bin'
        with source.open('wb') as out:
            out.truncate(65 * 1024 ** 2)
        request = {'argv': ['python3', '-c',
                    "from pathlib import Path; assert Path('source.bin').stat().st_size==65*1024**2; "
                    "f=Path('result.bin').open('wb'); chunk=bytes(1024**2); "
                    "[f.write(chunk) for _ in range(257)]; f.close()"],
                   'disk_bytes': 512 * 1024 ** 2, 'ram_bytes': 512 * 1024 ** 2,
                   'cpus': 1, 'timeout': 120, 'agent': 'CI · grands transferts'}
        incoming = fixture / 'input.tar.gz'
        with incoming.open('wb') as out:
            out.write(json.dumps(request).encode() + b'\n')
            with tarfile.open(fileobj=out, mode='w|gz') as tar:
                tar.add(source, arcname='source.bin')
        with incoming.open('rb') as src:
            result = subprocess.run(['docker', 'exec', '-i', '--user', '10001:10001', container,
                                     *WORKER_EXEC, 'run'], stdin=src, capture_output=True, timeout=180)
        assert result.returncode == 0, result.stderr[-2000:]
        task = json.loads(result.stdout)
        assert task['status'] == 'passed', task
        request = {'id': task['id'], 'paths': ['work/result.bin', 'receipt.json']}
        plan = json.loads(execute(container, 'fetch', json.dumps({**request, 'preview': True}).encode() + b'\n'))
        assert plan['bytes'] > 256 * 1024 ** 2 and plan['disk_bytes'] > 2 * plan['bytes'], plan
        execute(container, 'fetch', json.dumps({**request, 'max_bytes': 256 * 1024 ** 2}).encode() + b'\n', success=False)
        archive = fixture / 'result.tar'
        with archive.open('wb') as out:
            result = subprocess.run(['docker', 'exec', '-i', '--user', '10001:10001', container,
                                     *WORKER_EXEC, 'fetch'], input=json.dumps({**request, 'max_bytes': plan['bytes']}).encode() + b'\n',
                                    stdout=out, stderr=subprocess.PIPE, timeout=180)
        assert result.returncode == 0 and archive.stat().st_size <= plan['archive_bytes'], result.stderr[-2000:]
        target = fixture / 'verified'
        assert vps.unpack_result(archive, target, plan['bytes']) == 2
        expected = hashlib.sha256()
        for _ in range(257):
            expected.update(bytes(1024 ** 2))
        with (target / 'work/result.bin').open('rb') as data:
            actual = hashlib.file_digest(data, 'sha256').hexdigest()
        assert actual == expected.hexdigest()
        (evidence / 'budgeted-transfer.json').write_text(json.dumps({'job': task['id'], 'source_bytes': 65 * 1024 ** 2,
            'result_bytes': 257 * 1024 ** 2, 'sha256': actual, 'plan': plan, 'verified': True}))
        execute(container, 'release', json.dumps({'id': task['id']}).encode() + b'\n')
    print('Source >64 MiB + result >256 MiB with disk budgets and verified SHA256: passed', flush=True)


def ssh_frontends(parent):
    rows = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,comm='], text=True).splitlines()
    entries = [line.split(None, 2) for line in rows if len(line.split(None, 2)) == 3]
    owned = {parent}
    while True:
        new = {int(pid) for pid, ppid, _ in entries if int(ppid) in owned} - owned
        if not new:
            break
        owned.update(new)
    return [int(pid) for pid, _, name in entries if int(pid) in owned and Path(name).name == 'ssh']


def compose_deployments():
    def service(file, env=None):
        return json.loads(command(['docker', 'compose', '-f', file, 'config', '--format', 'json'],
                                  cwd=REPO, env=env))['services']['worker']
    base, cloud = service('docker-compose.yaml'), service('docker-compose.coolify.yaml')
    cloud_defaults = dict(cloud)
    private_owner = service('docker-compose.coolify-team.yaml')
    assert private_owner.pop('network_mode') == 'atelier-vps-owner'
    private_owner.pop('networks', None)
    normal_owner = {k: v for k, v in cloud_defaults.items() if k != 'networks'}
    assert private_owner == normal_owner, 'Cloud team owner must preserve every canonical worker protection'
    mounts = cloud.pop('volumes')
    base.pop('volumes', None)
    base_pids, cloud_pids = base.pop('pids_limit'), cloud.pop('pids_limit')
    assert base_pids == 512 and cloud_pids == 768, 'preserve legacy limit and calibrated Cloud process headroom'
    assert cloud == base, 'Coolify manifest must preserve all canonical worker protections'
    expected = {}
    for value in bindings({'volume_mount': '/mnt/atelier-vps'}):
        source, target, *options = value.split(':')
        expected[target] = ('bind', source, options == ['ro'])
    actual = {v['target']: (v['type'], v['source'], v.get('read_only', False)) for v in mounts}
    assert len(mounts) == 9 and actual == expected, 'nine exact dedicated-volume binds required'
    assert all('$' not in v['source'] for v in mounts), 'Coolify rejects interpolation in bind sources'
    large = service('docker-compose.coolify.yaml', {**os.environ, 'USEVPS_MEMORY_LIMIT': '48g',
                    'USEVPS_POOL_RAM_GIB': '48', 'USEVPS_HOST_RAM_RESERVE_GIB': '8'})
    assert int(large['mem_limit']) == int(large['memswap_limit']) == 48 * 1024 ** 3
    assert str(large['environment']['USEVPS_POOL_RAM_GIB']) == '48'
    assert str(large['environment']['USEVPS_HOST_RAM_RESERVE_GIB']) == '8'
    protected = lambda spec: {k: v for k, v in spec.items() if k not in {'mem_limit', 'memswap_limit', 'environment'}}
    assert protected(large) == protected(cloud_defaults), 'sizing RAM must preserve every other worker protection'
    cpu = service('docker-compose.coolify.yaml', {**os.environ, 'USEVPS_CPU_QUOTA': '600000',
                  'USEVPS_CPU_LIMIT': '6', 'USEVPS_MAX_CPUS': '6'})
    assert cpu['cpu_quota'] == 600000 and cpu['cpu_period'] == 100000 and cpu['cpu_shares'] == 128
    assert str(cpu['environment']['USEVPS_CPU_LIMIT']) == str(cpu['environment']['USEVPS_MAX_CPUS']) == '6'
    cpu['cpu_quota'] = cloud_defaults['cpu_quota']
    for key in ('USEVPS_CPU_LIMIT', 'USEVPS_MAX_CPUS'):
        cpu['environment'][key] = cloud_defaults['environment'][key]
    assert cpu == cloud_defaults, 'sizing CPU must preserve every other worker protection'
    print('Coolify Compose protections and nine literal dedicated-volume binds: passed', flush=True)
    return cloud_pids


def main():
    root = Path(os.environ["TASK_WORKSPACE"])
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(root)
    command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=REPO, env=env)
    print("Complete unit suite: passed", flush=True)
    remote = os.environ.get('DOCKER_HOST', '').startswith('ssh://')
    remote_host = os.environ.get('DOCKER_HOST', '')[len('ssh://'):]
    deployment = json.loads((REPO / 'ops/deployment.local.json').read_text()) if remote else {}
    local = not remote
    volume = Path(deployment['volume_mount']) if remote else None
    dev = Path(deployment['toolchain_root']) if remote else root / "devroot"
    if local:
        if sys.platform != "linux":
            raise RuntimeError("use the existing VPS Docker daemon; toolchain provisioning needs Linux")
        prefix = ["sudo", "-n"] if os.geteuid() else []
        command([*prefix, sys.executable, str(REPO / "ops/provision_env.py"), "--root", str(dev)])
    mounts = []
    for dst, src in {"/usr": "usr", "/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib",
                     "/lib64": "usr/lib64", "/etc": "etc", "/opt": "opt"}.items():
        mounts.extend(["--volume", str(dev / src) + ":" + dst + ":ro"])
    command(["docker", "compose", "-f", "docker-compose.yaml", "config", "--quiet"], cwd=REPO)
    ci_pids = compose_deployments()
    command(["docker", "build", "-t", "usevps:checks", "."], cwd=REPO)
    image = json.loads(command(['docker', 'image', 'inspect', 'usevps:checks']))[0]['Id']
    print("Container build: passed", flush=True)
    workspace = (Path(tempfile.mkdtemp(prefix="container-", dir=root)) if local else
                 Path(deployment['workspace']) / ('.usevps-qa-' + uuid.uuid4().hex))
    capacity = Path(tempfile.mkdtemp(prefix="capacity-", dir=root)) if local else volume / '.usevps-stat'
    if local:
        capacity.chmod(0o000)
        workspace.chmod(0o777)
        (workspace / ".volume-id").write_text("ci-volume\n")
    else:
        setup = ("from pathlib import Path; import os; p=Path(" + repr(str(workspace)) + "); "
                 "assert (p.parent/'.volume-id').read_text().strip()==" + repr(deployment['volume_id']) + "; "
                 "assert os.statvfs(p.parent).f_bavail*os.statvfs(p.parent).f_frsize > 1024**3; "
                 "p.mkdir(mode=0o777); p.chmod(0o777); (p/'.volume-id').write_text('ci-volume')")
        command(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", remote_host, "python3", "-"], input=setup.encode())
    name = "usevps-checks-" + uuid.uuid4().hex[:10]
    process = None
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    desktop_cache = {}
    complete = False
    try:
        process = subprocess.Popen(["docker", "run", "--rm", "--name", name, "--read-only",
                                    "--tmpfs", '/tmp:' + DESKTOP_TMPFS,
                                    "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=3g",
                                    "--memory-swap=3g", "--pids-limit=" + str(ci_pids), "--cpus=2", "--cpu-shares=128", *mounts,
                                    "--publish", "127.0.0.1::8080", "--volume", str(workspace) + ":/workspace",
                                    "--volume", str(capacity) + ":/volume-stat:ro", "--env", "USEVPS_VOLUME_STATS=/volume-stat",
                                    "--env", "USEVPS_VOLUME_ID=ci-volume", image],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(60):
            r = subprocess.run(["docker", "inspect", name], capture_output=True)
            if r.returncode == 0:
                info = json.loads(r.stdout)[0]
                if info.get("State", {}).get("Running"):
                    break
            if process.poll() is not None:
                raise RuntimeError(process.stdout.read(2000).decode(errors="replace"))
            time.sleep(0.5)
        else:
            raise RuntimeError("container failed to start")
        # Query from inside the real service container, also when Docker is remote.
        for _ in range(30):
            r = subprocess.run(["docker", "exec", name, "python3", "-c",
                                "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8080/health').status == 200"], capture_output=True)
            if r.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("real health service unavailable")
        # The running service keeps this exact image referenced during the probe.
        cpu_env = {**os.environ, 'USEVPS_CPU_QUOTA': '50000', 'USEVPS_CPU_LIMIT': '0.5'}
        spec = json.loads(command(['docker', 'compose', '-f', 'docker-compose.yaml', 'config', '--format', 'json'],
                                  cwd=REPO, env=cpu_env))['services']['worker']
        assert spec['tmpfs'] == ['/tmp:' + DESKTOP_TMPFS], spec['tmpfs']
        assert info['HostConfig']['Tmpfs'] == {'/tmp': DESKTOP_TMPFS}
        assert spec['pids_limit'] == 512
        assert info['HostConfig']['PidsLimit'] == ci_pids
        assert spec['cpu_period'] == 100000 and spec['cpu_quota'] == 50000
        cpu_name = 'usevps-cpu-spec-' + uuid.uuid4().hex[:10]
        spec.pop('build', None)
        spec.pop('networks', None)
        spec.update(image=image, container_name=cpu_name, network_mode='none')
        try:
            command(['docker', 'compose', '-p', cpu_name, '-f', '-', 'create'],
                    input=json.dumps({'services': {'worker': spec}}).encode(), cwd=REPO)
            info = json.loads(command(['docker', 'inspect', cpu_name]))[0]
            assert not info['State']['Running'] and info['State']['Pid'] == 0
            assert info['HostConfig']['CpuQuota'] == 50000 and info['HostConfig']['CpuPeriod'] == 100000
        finally:
            inspected = subprocess.run(['docker', 'inspect', cpu_name], capture_output=True)
            if inspected.returncode == 0:
                own = json.loads(inspected.stdout)[0]
                assert own['Config']['Labels']['com.docker.compose.project'] == cpu_name
                assert not own['State']['Running']
                command(['docker', 'rm', cpu_name])
        print('Compose CPU quota: passed', flush=True)
        # Create only: validate local immutable images and kernel limits without starting another worker.
        cloud_name = 'usevps-cloud-spec-' + uuid.uuid4().hex[:10]
        original = json.loads(command(['docker', 'inspect', name]))[0]
        cloud_spec = cloud_compose({'mount':'/volume','workspace':'/volume/member','volume_id':'ci-volume',
            'tenant':'member','mode':'allocated','host_ram_reserve_bytes':8*1024**3,'cpu_set':'',
            'limits':{'cpus':3,'cpu_shares':64,'ram_bytes':256*1024**2,'slots':2}},
            original['Image'], 'service12345', 768)['services']['worker']
        cloud_spec.update(container_name=cloud_name, network_mode='none', volumes=[
            {'type':'bind','source':m['Source'],'target':m['Destination'],'read_only':not m['RW']} for m in original['Mounts']])
        try:
            command(['docker','compose','-p',cloud_name,'-f','-','create'],
                    input=json.dumps({'services':{'worker':cloud_spec}}).encode(),cwd=REPO)
            checked = json.loads(command(['docker','inspect',cloud_name]))[0]
            assert not checked['State']['Running'] and checked['State']['Pid']==0
            assert checked['Image']==original['Image']
            assert checked['HostConfig']['Memory']==checked['HostConfig']['MemorySwap']==256*1024**2
            assert checked['HostConfig']['CpuQuota']==300000 and checked['HostConfig']['PidsLimit']==768
            assert checked['HostConfig']['ReadonlyRootfs'] and checked['Config']['User']=='10001:10001'
        finally:
            result = subprocess.run(['docker','inspect',cloud_name],capture_output=True)
            if result.returncode==0:
                own=json.loads(result.stdout)[0]
                assert own['Config']['Labels']['com.docker.compose.project']==cloud_name and not own['State']['Running']
                command(['docker','rm',cloud_name])
        print('Cloud member immutable image and real Docker limits: passed', flush=True)
        command(["docker", "exec", name, "python3", "-c", "import pip, venv; import socket; assert socket.getaddrinfo('registry.npmjs.org',443)"])
        command(["docker", "exec", name, "node", "--version"])
        (evidence / "toolchain.json").write_bytes(command(["docker", "exec", name, "cat", "/opt/usevps-dev.json"]))
        bad = subprocess.run(["docker", "run", "--rm", "--network=none", "--read-only",
                              "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=64m",
                              "--memory-swap=64m", "--pids-limit=32", "--volume", str(workspace) + ":/workspace",
                              "--env", "USEVPS_VOLUME_ID=ci-volume", *mounts, image], capture_output=True, timeout=60)
        assert bad.returncode != 0 and b"capacity probe is missing" in bad.stdout + bad.stderr, bad.stderr[-1000:]
        capacity_status = json.loads(execute(name, "status"))
        assert capacity_status["admit"] and capacity_status["workspace_free_bytes"] > 0, capacity_status
        metrics = capacity_status['telemetry']
        assert metrics['cpu_total'] > 0 and metrics['host_total_ram'] > 0, metrics
        assert metrics['worker_ram_limit'] == 3 * 1024 ** 3 and metrics['worker_ram'] > 0, metrics
        task = json.loads(execute(name, "run", payload(["python3", "-c",
                           "from pathlib import Path; import os,tempfile; assert Path('/proc/self/oom_score_adj').read_text().strip()=='500'; assert Path('/tmp').stat().st_uid==os.getuid(); s=os.statvfs('/tmp'); assert s.f_blocks*s.f_frsize==8388608; f=tempfile.TemporaryFile(dir='/tmp'); f.write(b'ok'); f.close(); Path('result.txt').write_text(str(int(Path('input.txt').read_text())*2))"],
                           agent='CI · transfert vérifié', project='usevps')))
        assert task["status"] == "passed", task
        recent = json.loads(execute(name, 'status'))['telemetry']['activity'][0]
        assert recent['id'] == task['id'] and recent['agent'] == 'CI · transfert vérifié' and recent['status'] == 'passed', recent
        assert 'argv' not in recent and 'resources' not in recent, recent
        assert task["resources"]["growth_bytes"] <= task["resources"]["declared_disk_bytes"], task
        data = execute(name, "fetch", json.dumps({"id": task["id"], "paths": ["work/result.txt", "receipt.json"]}).encode() + b"\n")
        archive = root / "roundtrip.tar"
        archive.write_bytes(data)
        dest = root / ("result-" + name)
        assert vps.unpack_result(archive, dest) == 2
        assert (dest / "work/result.txt").read_text() == "42"
        transfers = json.loads(execute(name, 'status'))['telemetry']['transfers']
        upload = next(x for x in transfers if x.get('job') == task['id'] and x['direction'] == 'upload')
        download = next(x for x in transfers if x.get('job') == task['id'] and x['direction'] == 'download')
        assert upload['status'] == download['status'] == 'passed' and upload['bytes'] > 0, transfers
        assert download['bytes'] == len(data) and download['agent'] == 'CI · transfert vérifié', transfers
        assert all('argv' not in transfer for transfer in transfers), transfers
        (evidence / 'transfer-proof.json').write_text(json.dumps(transfers))
        print("Running service + input/process/result SHA256 round trip: passed", flush=True)
        execute(name, "release", json.dumps({"id": task["id"]}).encode() + b"\n")
        verify_budgeted_transfers(name, root, evidence)
        with tempfile.TemporaryDirectory(dir=root) as fixture:
            source = Path(fixture); (source / 'input.txt').write_text('21\n')
            (source / 'src').mkdir(); (source / 'src/app.py').write_text('original\n')
            digest, _ = projects.fingerprint(projects.tree_files(source))
            ref = {'id':'demo', 'revision':digest}
            prepared = json.loads(execute(name, 'run', payload(['true'], files={'src/app.py':'original\n'},
                project_create=ref, disk_bytes=32 * 1024**2)))
            assert prepared['status'] == 'passed' and prepared['managed_project']['revision'] == digest, prepared
            execute(name, 'release', json.dumps({'id':prepared['id']}).encode() + b'\n')
            commands = ["from pathlib import Path; import time; Path('src/app.py').write_text('changed'); time.sleep(2)",
                        "from pathlib import Path; import time; assert Path('src/app.py').read_text() == 'original\\n'; time.sleep(2)"]
            jobs = [subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', name, *WORKER_EXEC, 'run'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in commands]
            try:
                for process, code in zip(jobs, commands):
                    process.stdin.write(payload(['python3','-c',code], include_default=False,
                                                 project_from=ref, disk_bytes=32 * 1024**2))
                    process.stdin.close(); process.stdin = None
                results = []
                for process in jobs:
                    out, err = process.communicate(timeout=60)
                    assert process.returncode == 0, err[-1000:]
                    results.append(json.loads(out))
                assert all(result['status'] == 'passed' for result in results), results
                assert max(result['started'] for result in results) < min(result['ended'] for result in results), results
                for result in results:
                    execute(name, 'release', json.dumps({'id':result['id']}).encode() + b'\n')
            finally:
                for process in jobs:
                    if process.poll() is None:
                        process.terminate(); process.wait(timeout=10)
        print('Persistent project version survives release and isolates concurrent jobs: passed', flush=True)
        slow = subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', name, *WORKER_EXEC, 'run'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        body = payload(['true'], files={'large.txt': 'x' * 128 * 1024}, agent='CI · sources en cours', ram_bytes=128 * 1024**2)
        waiter = None
        try:
            slow.stdin.write(body[:10240]); slow.stdin.flush()
            pending = None
            for _ in range(30):
                state = json.loads(execute(name, 'status'))
                pending = next((x for x in state['telemetry']['transfers'] if x['status'] == 'running' and x['agent'] == 'CI · sources en cours'), None)
                if pending:
                    break
                time.sleep(0.1)
            assert pending is not None, state
            waiter = subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', name, *WORKER_EXEC, 'fetch'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            waiter.stdin.write(json.dumps({'id': pending['job'], 'wait_timeout': 30}).encode() + b'\n')
            waiter.stdin.close(); waiter.stdin = None
            time.sleep(0.2)
            assert waiter.poll() is None, 'wait returned while the upload was still active'
            # Expired caches cannot be removed while even an upload owns a lease.
            guard = "from pathlib import Path; import dependency_cache as c, resources; p=c.scope(Path('/workspace'), {'project':'expired-fixture'}); resources.write_json(p/'.retention.json', {'version':1,'last_used':0}); c.maintain(Path('/workspace')); assert p.exists()"
            command(['docker', 'exec', '--user', '10001:10001', name, 'python3', '-c', guard])
            slow.stdin.write(body[10240:]); slow.stdin.close(); slow.stdin = None
            out, errors = slow.communicate(timeout=60)
            assert slow.returncode == 0, errors[-1000:]
            completed = json.loads(out)
            waiting, errors = waiter.communicate(timeout=30)
            assert waiter.returncode == 0, errors[-1000:]
            assert json.loads(waiting) == {'kind': 'job_wait', 'id': completed['id'], 'status': 'passed', 'timed_out': False}
            execute(name, 'release', json.dumps({'id': completed['id']}).encode() + b'\n')
            clean = "from pathlib import Path; import dependency_cache as c; root=Path('/workspace'); before=list((root/'cache/managed').iterdir()); old=[p for p in before if c.last_used(p)==0]; assert len(old)==1; c.maintain(root); assert not old[0].exists(); assert all(p.exists() for p in before if p not in old); assert c.status(root)['removed_scopes']==1"
            command(['docker', 'exec', '--user', '10001:10001', name, 'python3', '-c', clean])
        finally:
            if slow.poll() is None:
                slow.terminate(); slow.wait(timeout=10)
            if waiter and waiter.poll() is None:
                waiter.terminate(); waiter.wait(timeout=10)
        print('Server-side completion wait + active transfer and safe cache retention: passed', flush=True)
        peak_command = ['python3', '-c', 'data=bytearray(128*1024**2); print(len(data))']
        first_peak = json.loads(execute(name, 'run', payload(peak_command, ram_bytes=None)))
        assert first_peak['status'] == 'passed', first_peak
        measured = first_peak['resources']['ram_peak_bytes']
        assert measured >= 128 * 1024**2, first_peak
        assert measured > first_peak['resources']['ram_bytes'], first_peak
        second_peak = json.loads(execute(name, 'run', payload(peak_command, ram_bytes=None)))
        assert second_peak['status'] == 'passed', second_peak
        assert measured < second_peak['reservation']['ram_bytes'] < first_peak['reservation']['ram_bytes'], second_peak
        assert second_peak['reservation']['ram_source'] == 'measured', second_peak
        (evidence / 'ram-profile-proof.json').write_text(json.dumps({'first': first_peak, 'second': second_peak}))
        for job in [first_peak, second_peak]:
            execute(name, 'release', json.dumps({'id': job['id']}).encode() + b'\n')
        print('Short-lived child RAM peak and measured auto reservation: passed', flush=True)
        orphan = json.loads(execute(name, "run", payload(["python3", "-c", "import subprocess; from pathlib import Path; p=subprocess.Popen(['python3','-c','import time; time.sleep(60)'], start_new_session=True); Path('orphan.pid').write_text(str(p.pid))"])))
        assert orphan["status"] == "passed", orphan
        command(["docker", "exec", name, "python3", "-c", "from pathlib import Path; p=int(Path('/workspace/jobs/" + orphan["id"] + "/work/orphan.pid').read_text()); s=Path('/proc')/str(p)/'stat'; assert not s.exists() or s.read_text().rsplit(') ',1)[1].startswith('Z ')"])
        execute(name, "release", json.dumps({"id": orphan["id"]}).encode() + b"\n")
        failed = json.loads(execute(name, "run", payload(["python3", "-c", "import time; time.sleep(5)"], timeout=1), success=False))
        assert failed["status"] == "failed" and failed["reason"] == "timeout", failed
        failed_wait = json.loads(execute(name, 'fetch', json.dumps({'id': failed['id'], 'wait_timeout': 2}).encode() + b'\n'))
        assert failed_wait['status'] == 'failed' and not failed_wait['timed_out'], failed_wait
        detail_tar = execute(name, 'fetch', json.dumps({'id': failed['id'], 'paths': ['diagnostic.json']}).encode() + b'\n')
        with tarfile.open(fileobj=io.BytesIO(detail_tar)) as details:
            timeout_card = json.load(details.extractfile('diagnostic.json'))
        assert timeout_card['kind'] == 'timeout' and timeout_card['resources']['timeout_seconds'] == 1, timeout_card
        execute(name, "release", json.dumps({"id": failed["id"]}).encode() + b"\n")
        failed = json.loads(execute(name, 'run', payload(['python3', '-c',
            "print('AssertionError: expected 42'); print('TOKEN=private-fixture-value'); raise SystemExit(7)"],
            agent='CI · détails privés', project='usevps', ram_bytes=128 * 1024**2), success=False))
        execute(name, 'release', json.dumps({'id': failed['id']}).encode() + b'\n')
        detail_tar = execute(name, 'fetch', json.dumps({'id': failed['id'], 'paths': ['diagnostic.json']}).encode() + b'\n')
        detail_path = root / 'failure-details.tar'; detail_path.write_bytes(detail_tar)
        detail_dir = root / 'failure-details'
        assert vps.unpack_result(detail_path, detail_dir) == 1
        card = json.loads((detail_dir / 'diagnostic.json').read_text())
        assert card['kind'] == 'command' and card['returncode'] == 7 and 'expected 42' in card['log_tail'], card
        assert 'private-fixture-value' not in card['log_tail'], card
        public = execute(name, 'status')
        assert b'AssertionError' not in public and b'private-fixture-value' not in public
        (evidence / 'failure-details-proof.json').write_text(json.dumps(card))
        detail_path.unlink(); shutil.rmtree(detail_dir)
        print('Private failure details: classified, bounded, redacted, SHA256 verified and retained after release', flush=True)
        failed = json.loads(execute(name, "run", payload(["python3", "-c", "import sys; sys.stdout.write('x'*22*1024*1024)"], disk_bytes=64 * 1024 ** 2), success=False))
        assert failed["status"] == "failed" and failed["reason"] == "log limit", failed
        command(["docker", "exec", name, "python3", "-c", "from pathlib import Path; assert Path('/workspace/jobs/" + failed["id"] + "/stdout.log').stat().st_size <= 20*1024**2"])
        execute(name, "release", json.dumps({"id": failed["id"]}).encode() + b"\n")
        fast = json.loads(execute(name, "run", payload(["python3", "-c", "from pathlib import Path; Path('too-big.bin').write_bytes(b'x'*1024**2)"], disk_bytes=256 * 1024), success=False))
        assert fast["status"] == "failed" and fast["reason"] == "disk limit", fast
        assert fast["resources"]["growth_bytes"] > fast["resources"]["declared_disk_bytes"], fast
        execute(name, "release", json.dumps({"id": fast["id"]}).encode() + b"\n")
        print("Timeout, task cleanup and strict log cap: passed", flush=True)
        print("Startup and fast-completion resource hooks: passed", flush=True)
        seed = json.loads(execute(name, 'run', payload(['true'])))
        parallel = []
        try:
            for label in ['CI · calcul A', 'CI · calcul B']:
                child = subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', name,
                                          *WORKER_EXEC, 'run'], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                program = "import time; from pathlib import Path\nwhile not Path('continue').exists(): time.sleep(0.2)\nprint(42)"
                child.stdin.write(payload(['python3', '-c', program], timeout=120,
                                          agent=label, ram_bytes=256 * 1024 ** 2))
                child.stdin.close()
                child.stdin = None
                parallel.append(child)
            for _ in range(30):
                sharing = json.loads(execute(name, 'status'))
                if sharing['pool']['active_count'] == 2:
                    break
                time.sleep(0.2)
            assert sharing['pool']['active_count'] == 2, sharing
            assert sharing['pool']['cpu_reserved'] == 2, sharing
            assert sharing['pool']['ram_reserved'] == 512 * 1024 ** 2, sharing
            execute(name, 'release', json.dumps({'id': seed['id']}).encode() + b'\n')
            second_id = next(x['id'] for x in sharing['pool']['active'] if x['agent'] == 'CI · calcul B')
            command(['docker', 'exec', name, 'python3', '-c',
                     "from pathlib import Path; Path('/workspace/jobs/" + second_id + "/work/continue').touch()"])
            second_out, second_err = parallel[1].communicate(timeout=30)
            second = json.loads(second_out)
            assert parallel[1].returncode == 0 and second['status'] == 'passed', second_err
            execute(name, 'release', json.dumps({'id': second['id']}).encode() + b'\n')
            memory = json.loads(execute(name, 'run', payload(['python3', '-c',
                'import time; data=bytearray(256*1024**2); time.sleep(6)'], ram_bytes=64 * 1024 ** 2), success=False))
            assert memory['reason'] == 'task memory limit', memory
            execute(name, 'release', json.dumps({'id': memory['id']}).encode() + b'\n')
            assert json.loads(execute(name, 'status'))['pool']['active_count'] == 1
            first_id = next(x['id'] for x in sharing['pool']['active'] if x['agent'] == 'CI · calcul A')
            command(['docker', 'exec', name, 'python3', '-c',
                     "from pathlib import Path; Path('/workspace/jobs/" + first_id + "/work/continue').touch()"])
            first_out, first_err = parallel[0].communicate(timeout=30)
            first = json.loads(first_out)
            assert parallel[0].returncode == 0 and first['status'] == 'passed', first_err
            execute(name, 'release', json.dumps({'id': first['id']}).encode() + b'\n')
            (evidence / 'sharing-proof.json').write_text(json.dumps({'two_jobs': True, 'isolated_memory_stop': True,
                'release_while_running': True, 'reserved_cpu': sharing['pool']['cpu_reserved'],
                'reserved_ram': sharing['pool']['ram_reserved']}))
            print('Concurrent reservations, independent cleanup and per-task memory guard: passed', flush=True)
        finally:
            for child in parallel:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=10)
        for file, argv, peak in [("browser-check.js", ["node", "browser-check.js"], 128),
                                 ("python-check.py", ["python3", "python-check.py"], 128),
                                 ("java-check.py", ["python3", "java-check.py"], 512)]:
            job = json.loads(execute(name, "run", payload(argv, files={file: (REPO / "examples" / file).read_text()},
                                                         timeout=150, disk_bytes=peak * 1024 ** 2)))
            assert job["status"] == "passed", job
            proof_name = file.split("-")[0] + "-proof.json"
            proof_tar = execute(name, "fetch", json.dumps({"id": job["id"], "paths": ["work/" + proof_name]}).encode() + b"\n")
            proof_archive = root / "tool-proof.tar"
            proof_archive.write_bytes(proof_tar)
            proof_dir = root / ("proof-" + job["id"])
            vps.unpack_result(proof_archive, proof_dir)
            assert json.loads((proof_dir / "work" / proof_name).read_text())["value"] == 42
            shutil.copyfile(proof_dir / "work" / proof_name, evidence / proof_name)
            shutil.rmtree(proof_dir)
            proof_archive.unlink()
            execute(name, "release", json.dumps({"id": job["id"]}).encode() + b"\n")
            print(file + ": real toolchain scenario passed", flush=True)
        checked_guard = json.loads(execute(name, "run", payload(["rtk", "proxy", "python3", "/app/storage-guard.py", "--volume", ".", "--require-gib", "0.001"])))
        assert checked_guard["status"] == "passed", checked_guard
        execute(name, "release", json.dumps({"id": checked_guard["id"]}).encode() + b"\n")
        desktop_checks.verify_desktops(root, name, workspace, deployment, remote_host if remote else None,
                                       command, execute, desktop_cache)
        from team_checks import verify_team_isolation
        verify_team_isolation(root, dev, remote_host if remote else None, volume, command, execute, payload, image)
        print('Separate developers: private files, hard quota, CPU cap and neighboring OOM survival passed', flush=True)
        from enrollment_checks import verify_enrollment
        verify_enrollment(root, command, image, remote_host if remote else None)
        print('Real SSH enrollment: one use, host pin, preserved primary access and forced commands passed', flush=True)
        shutil.rmtree(dest)
        archive.unlink()
        complete = True
    finally:
        subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True, timeout=15)
        if process:
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                state = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=15)
                if state.returncode == 0 and json.loads(state.stdout)[0]['State']['Running']:
                    raise RuntimeError('QA container is still running; preserve its workspace')
                # The fixture is stopped; close only its lingering local Docker/SSH frontend.
                for pid in ssh_frontends(process.pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                process.terminate()
                process.communicate(timeout=10)
        if remote and desktop_cache:
            desktop_checks.relocate_cache(desktop_cache, workspace, Path(deployment['workspace']),
                                           remote_host, command, restore=True)
        if not complete:
            (evidence / 'preserved-fixture.json').write_text(json.dumps({'workspace': str(workspace),
                'container': name, 'reason': 'complete suite failed; preserve owned fixture data for diagnosis'}, indent=2))
            print('Owned failed fixture preserved for diagnosis: ' + str(workspace), flush=True)
            raise RuntimeError('Complete suite failed; its owned fixture was preserved for diagnosis')
        if local:
            cleanup_work = "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); assert p.name.startswith('container-') and (p/'.volume-id').read_text().strip()=='ci-volume'; shutil.rmtree(p)"
            command([*( ["sudo", "-n"] if os.geteuid() else []), sys.executable, "-c", cleanup_work, str(workspace)])
            capacity.chmod(0o700)
            capacity.rmdir()
            cleanup_dev = "import pathlib,os,shutil,sys; p=pathlib.Path(sys.argv[1]); assert p.name=='devroot' and (p/'.usevps-owned').is_file() and not os.path.ismount(p/'proc'); shutil.rmtree(p)"
            command([*( ["sudo", "-n"] if os.geteuid() else []), sys.executable, "-c", cleanup_dev, str(dev)])
        else:
            cleanup = "from pathlib import Path; import shutil; p=Path(" + repr(str(workspace)) + "); assert p.name.startswith('.usevps-qa-'); shutil.rmtree(p)"
            command(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", remote_host, "python3", "-"], input=cleanup.encode())


if __name__ == "__main__":
    main()
