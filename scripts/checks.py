"""The complete CI suite, including a real running container and file round trip."""
import io
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
from ops.gateway import WORKER_EXEC


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


def payload(argv, files=None, **options):
    request = {"argv": argv, "disk_bytes": 16 * 1024 ** 2, "timeout": 20, **options}
    out = io.BytesIO()
    out.write(json.dumps(request).encode() + b"\n")
    with tarfile.open(fileobj=out, mode="w") as tar:
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
    command(["docker", "build", "-t", "usevps:checks", "."], cwd=REPO)
    print("Container build: passed", flush=True)
    workspace = Path(tempfile.mkdtemp(prefix="container-", dir=root)) if local else volume / ('.usevps-qa-' + uuid.uuid4().hex)
    capacity = Path(tempfile.mkdtemp(prefix="capacity-", dir=root)) if local else volume / '.usevps-stat'
    if local:
        capacity.chmod(0o000)
        workspace.chmod(0o777)
        (workspace / ".volume-id").write_text("ci-volume\n")
    else:
        setup = ("from pathlib import Path; import os; p=Path(" + repr(str(workspace)) + "); "
                 "assert os.statvfs(p.parent).f_bavail*os.statvfs(p.parent).f_frsize > 1024**3; "
                 "p.mkdir(mode=0o777); p.chmod(0o777); (p/'.volume-id').write_text('ci-volume')")
        command(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", remote_host, "python3", "-"], input=setup.encode())
    name = "usevps-checks-" + uuid.uuid4().hex[:10]
    process = None
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    try:
        process = subprocess.Popen(["docker", "run", "--rm", "--name", name, "--read-only",
                                    "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=2g",
                                    "--memory-swap=2g", "--pids-limit=256", "--cpus=2", *mounts,
                                    "--publish", "127.0.0.1::8080", "--volume", str(workspace) + ":/workspace",
                                    "--volume", str(capacity) + ":/volume-stat:ro", "--env", "USEVPS_VOLUME_STATS=/volume-stat",
                                    "--env", "USEVPS_VOLUME_ID=ci-volume", "usevps:checks"],
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
        command(["docker", "exec", name, "python3", "-c", "import pip, venv; import socket; assert socket.getaddrinfo('registry.npmjs.org',443)"])
        command(["docker", "exec", name, "node", "--version"])
        (evidence / "toolchain.json").write_bytes(command(["docker", "exec", name, "cat", "/opt/usevps-dev.json"]))
        bad = subprocess.run(["docker", "run", "--rm", "--network=none", "--read-only",
                              "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=64m",
                              "--memory-swap=64m", "--pids-limit=32", "--volume", str(workspace) + ":/workspace",
                              "--env", "USEVPS_VOLUME_ID=ci-volume", *mounts, "usevps:checks"], capture_output=True, timeout=60)
        assert bad.returncode != 0 and b"capacity probe is missing" in bad.stdout + bad.stderr, bad.stderr[-1000:]
        capacity_status = json.loads(execute(name, "status"))
        assert capacity_status["admit"] and capacity_status["workspace_free_bytes"] > 0, capacity_status
        metrics = capacity_status['telemetry']
        assert metrics['cpu_total'] > 0 and metrics['host_total_ram'] > 0, metrics
        assert metrics['worker_ram_limit'] == 2 * 1024 ** 3 and metrics['worker_ram'] > 0, metrics
        task = json.loads(execute(name, "run", payload(["python3", "-c",
                           "from pathlib import Path; assert Path('/proc/self/oom_score_adj').read_text().strip()=='500'; Path('result.txt').write_text(str(int(Path('input.txt').read_text())*2))"],
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
        print("Running service + input/process/result SHA256 round trip: passed", flush=True)
        execute(name, "release", json.dumps({"id": task["id"]}).encode() + b"\n")
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
        execute(name, "release", json.dumps({"id": failed["id"]}).encode() + b"\n")
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
        from team_checks import verify_team_isolation
        verify_team_isolation(root, dev, remote_host if remote else None, volume, command, execute, payload)
        print('Separate developers: private files, hard quota, CPU cap and neighboring OOM survival passed', flush=True)
        shutil.rmtree(dest)
        archive.unlink()
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
