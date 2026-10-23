"""The complete CI suite, including a real running container and file round trip."""
import io
import json
import os
from pathlib import Path
import shutil
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


def command(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(str(argv[:4]) + ": " + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout


def execute(container, action, body=b"", success=True):
    r = subprocess.run(["docker", "exec", "-i", "--user", "10001:10001", container,
                        "python3", "/app/worker.py", action], input=body, capture_output=True, timeout=60)
    if success and r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace")[-2000:] + r.stdout.decode(errors="replace")[-1000:])
    if not success and r.returncode == 0:
        raise AssertionError("expected operation to fail")
    return r.stdout


def payload(argv, **options):
    request = {"argv": argv, "disk_bytes": 16 * 1024 ** 2, "timeout": 20, **options}
    out = io.BytesIO()
    out.write(json.dumps(request).encode() + b"\n")
    with tarfile.open(fileobj=out, mode="w") as tar:
        data = b"21\n"
        info = tarfile.TarInfo("input.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def main():
    root = Path(os.environ["TASK_WORKSPACE"])
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(root)
    command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=REPO, env=env)
    print("Complete unit suite: passed", flush=True)
    command(["docker", "compose", "-f", "docker-compose.yaml", "config", "--quiet"], cwd=REPO)
    command(["docker", "build", "-t", "usevps:checks", "."], cwd=REPO)
    print("Container build: passed", flush=True)
    remote = os.environ.get("DOCKER_HOST") == "ssh://root@coolifyx.progsoft.eu"
    local = not remote
    workspace = Path(tempfile.mkdtemp(prefix="container-", dir=root)) if local else Path("/mnt/HC_Volume_107251752/.usevps-qa-" + uuid.uuid4().hex)
    if local:
        workspace.chmod(0o777)
        (workspace / ".volume-id").write_text("ci-volume\n")
    else:
        setup = ("from pathlib import Path; import os; p=Path(" + repr(str(workspace)) + "); "
                 "assert os.statvfs(p.parent).f_bavail*os.statvfs(p.parent).f_frsize > 21*1024**3; "
                 "p.mkdir(mode=0o777); p.chmod(0o777); (p/'.volume-id').write_text('ci-volume')")
        command(["ssh", "-o", "BatchMode=yes", "root@coolifyx.progsoft.eu", "python3", "-"], input=setup.encode())
    name = "usevps-checks-" + uuid.uuid4().hex[:10]
    process = None
    try:
        process = subprocess.Popen(["docker", "run", "--rm", "--name", name, "--read-only",
                                    "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=256m",
                                    "--memory-swap=256m", "--pids-limit=128", "--cpus=1",
                                    "--publish", "127.0.0.1::8080", "--volume", str(workspace) + ":/workspace",
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
        task = json.loads(execute(name, "run", payload(["python3", "-c",
                           "from pathlib import Path; Path('result.txt').write_text(str(int(Path('input.txt').read_text())*2))"])))
        assert task["status"] == "passed", task
        data = execute(name, "fetch", json.dumps({"id": task["id"], "paths": ["work/result.txt", "receipt.json"]}).encode() + b"\n")
        archive = root / "roundtrip.tar"
        archive.write_bytes(data)
        dest = root / ("result-" + name)
        assert vps.unpack_result(archive, dest) == 2
        assert (dest / "work/result.txt").read_text() == "42"
        print("Running service + input/process/result SHA256 round trip: passed", flush=True)
        execute(name, "release", json.dumps({"id": task["id"]}).encode() + b"\n")
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
        print("Timeout, task cleanup and strict log cap: passed", flush=True)
        shutil.rmtree(dest)
        archive.unlink()
    finally:
        subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True, timeout=15)
        if process:
            process.wait(timeout=15)
        if local:
            shutil.rmtree(workspace)
        else:
            cleanup = "from pathlib import Path; import shutil; p=Path(" + repr(str(workspace)) + "); assert p.name.startswith('.usevps-qa-'); shutil.rmtree(p)"
            command(["ssh", "-o", "BatchMode=yes", "root@coolifyx.progsoft.eu", "python3", "-"], input=cleanup.encode())


if __name__ == "__main__":
    main()
