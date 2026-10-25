"""Bounded Linux worker. Commands are transported over a restricted SSH key."""
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telemetry import process_token, public_label, record_activity, snapshot, Transfer
import dependency_cache
import diagnostics
import resources
from quota_protection import protect_quota

GIB = 1024 ** 3
MIB = 1024 ** 2
ROOT = Path(os.environ.get("USEVPS_ROOT", "/workspace"))
INPUT_LIMIT = 64 * MIB
FETCH_LIMIT = 256 * MIB
JOB_LIMIT = 32 * GIB
TOTAL_LIMIT = 32 * GIB
LOG_LIMIT = 20 * MIB
RECEIPT_RESERVE = b"usevps receipt reserve v1\n"
EXCLUDED = {".git", "node_modules", ".venv", "__pycache__", "CLAUDE.md"}


def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\x00" in name:
        raise ValueError("unsafe path")
    if any(x in EXCLUDED for x in p.parts):
        raise ValueError("excluded path")
    return p


def usage(path):
    total, count = 0, 0
    def missing_only(error):
        if error.errno != errno.ENOENT:
            raise error
    for base, dirs, files in os.walk(path, followlinks=False, onerror=missing_only):
        for name in dirs + files:
            try:
                s = (Path(base) / name).lstat()
            except FileNotFoundError:
                # Atomic lease/cache replacements and independent releases are normal.
                continue
            total += s.st_blocks * 512
            count += 1
            if count > 500000:
                raise ValueError("file count limit")
    return total


def volume_ok():
    expected = os.environ.get("USEVPS_VOLUME_ID", "")
    return bool(expected) and (ROOT / ".volume-id").read_text().strip() == expected


def available_ram():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("host memory unavailable")


def validate_environment():
    """Startup/health hook: validate configuration, independently of capacity."""
    if not volume_ok() or os.geteuid() != 10001 or os.getegid() != 10001:
        raise RuntimeError("worker identity or volume mismatch")
    probe = os.environ.get("USEVPS_VOLUME_STATS")
    if not probe:
        raise RuntimeError("required read-only capacity probe is missing")
    capacity = Path(probe)
    if capacity.stat().st_dev != ROOT.stat().st_dev or not os.statvfs(capacity).f_flag & os.ST_RDONLY:
        raise RuntimeError("capacity probe must be read-only on the workspace filesystem")
    if not os.access(ROOT, os.W_OK):
        raise RuntimeError("workspace is not writable")
    expected = hashlib.sha256(Path("/app/toolchain-lock.json").read_bytes()).hexdigest()
    if json.loads(Path("/opt/usevps-dev.json").read_text()).get("spec_sha256") != expected:
        raise RuntimeError("development toolchain does not match this image")


def is_busy():
    with (ROOT / '.queue.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def capacity():
    if not volume_ok():
        raise RuntimeError("volume identity mismatch")
    # ext4 project quotas clamp statvfs inside ROOT to the project allowance.
    # An empty, inaccessible, read-only bind outside that project reports the disk.
    capacity = Path(os.environ.get("USEVPS_VOLUME_STATS", str(ROOT)))
    if capacity.stat().st_dev != ROOT.stat().st_dev:
        raise RuntimeError("capacity probe is on another filesystem")
    s = os.statvfs(capacity)
    free = s.f_bavail * s.f_frsize
    q = os.statvfs(ROOT)
    workspace_free = q.f_bavail * q.f_frsize
    reserve = int(float(os.environ.get("USEVPS_RESERVE_GIB", "0.5")) * GIB)
    budget = min(TOTAL_LIMIT, q.f_blocks * q.f_frsize)
    # With an ext4 project quota these counters include caches and file metadata.
    used = ((q.f_blocks - q.f_bfree) * q.f_frsize
            if q.f_blocks < s.f_blocks else usage(ROOT))
    ram = available_ram()
    return {"ok": True, "volume_id": os.environ["USEVPS_VOLUME_ID"],
            "free_bytes": free, "workspace_free_bytes": workspace_free,
            "used_bytes": used, "reserve_bytes": reserve,
            "budget_bytes": budget, "host_available_ram": ram}


def status():
    state = capacity()
    with resources.resource_lock(ROOT):
        pool = resources.view(ROOT, state)
    with (ROOT / '.queue.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            maintenance = False
        except BlockingIOError:
            maintenance = True
    maintenance = maintenance or os.environ.get('USEVPS_MAINTENANCE') == '1'
    state.update(pool=pool, busy=bool(pool['active_count']) or maintenance, maintenance=maintenance,
                 admit=not maintenance and resources.fits(pool, 1, 64 * MIB, MIB),
                 capabilities={'ram_profiles': True, 'quota_attributes_protected': True, 'failure_details': True},
                 scope={'id': os.environ.get('USEVPS_TENANT', 'owner'),
                        'mode': os.environ.get('USEVPS_MODE', 'opportunistic')})
    return state


def public_status():
    state = status()
    state['telemetry'] = snapshot(ROOT, state['busy'])
    telemetry = state['telemetry']
    telemetry['cache_retention'] = dependency_cache.status(ROOT)
    telemetry['worker_cpu_limit'] = float(os.environ.get('USEVPS_CPU_LIMIT', resources.cpu_capacity()))
    if state['scope']['mode'] == 'allocated':
        for key in ('cpu_total', 'cpu_idle', 'host_total_ram', 'system_disk_total', 'system_disk_free'):
            telemetry.pop(key, None)
        telemetry['cpu_count'] = max(1, int(telemetry['worker_cpu_limit']))
        state['host_available_ram'] = state['pool']['ram_available']
        state['free_bytes'] = state['workspace_free_bytes']
        state['reserve_bytes'] = 0
        state.pop('volume_id', None)
        for key in ('host_available_bytes', 'host_floor_bytes'):
            state['pool']['ram_accounting'].pop(key, None)
    return state


def extract(stream, target):
    size, count = 0, 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            rel = safe_path(member.name)
            dest = target.joinpath(*rel.parts)
            count += 1
            if count > 10000 or member.size < 0:
                raise ValueError("archive file count limit")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                size += member.size
                if size > INPUT_LIMIT:
                    raise ValueError("input exceeds 64 MiB")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, dest.open("xb") as out:
                    shutil.copyfileobj(src, out, 64 * 1024)
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("links and special files are forbidden")


def read_request():
    line = sys.stdin.buffer.readline(65537)
    if len(line) > 65536 or not line.endswith(b"\n"):
        raise ValueError("invalid request")
    return json.loads(line)


def job_path(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("invalid job id")
    return ROOT / "jobs" / value


def ensure_receipt_reserve():
    reserve = ROOT / ".receipt-reserve"
    if reserve.exists():
        if reserve.is_symlink() or reserve.stat().st_size != MIB:
            raise RuntimeError("unexpected receipt reserve file")
        return True
    created = False
    try:
        with reserve.open("xb") as out:
            created = True
            out.write(RECEIPT_RESERVE + b"\0" * (MIB - len(RECEIPT_RESERVE)))
        return True
    except OSError as exc:
        if created:
            reserve.unlink(missing_ok=True)
        if exc.errno not in {errno.ENOSPC, errno.EDQUOT}:
            raise
        return False


def receipt(path, data):
    tmp = path / "receipt.tmp"
    try:
        tmp.write_text(json.dumps(data))
    except OSError as exc:
        reserve = ROOT / ".receipt-reserve"
        if exc.errno not in {errno.ENOSPC, errno.EDQUOT} or reserve.is_symlink() or not reserve.is_file():
            raise
        with reserve.open("rb") as source:
            if reserve.stat().st_size != MIB or source.read(len(RECEIPT_RESERVE)) != RECEIPT_RESERVE:
                raise RuntimeError("receipt reserve is not an owned technical buffer") from exc
        reserve.unlink()
        tmp.write_text(json.dumps(data))
    tmp.replace(path / "receipt.json")
    record_activity(ROOT, data)
    diagnostics.remember(ROOT, path, data)


def descendants():
    parents = {}
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            try:
                fields = (item / "stat").read_text().rsplit(") ", 1)[1].split()
                parents[int(item.name)] = int(fields[1])
            except (OSError, IndexError):
                pass
    found = {os.getpid()}
    while True:
        new = {pid for pid, ppid in parents.items() if ppid in found} - found
        if not new:
            return found - {os.getpid()}
        found.update(new)


def stop_children():
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in descendants():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(0.5)


def process_memory(pids):
    total = 0
    for pid in pids:
        try:
            # PSS counts shared libraries once across cooperating worker processes.
            lines = (Path('/proc') / str(pid) / 'smaps_rollup').read_text().splitlines()
            total += next(int(line.split()[1]) * 1024 for line in lines if line.startswith('Pss:'))
        except FileNotFoundError:
            continue
        except (OSError, StopIteration):
            try:
                lines = (Path('/proc') / str(pid) / 'status').read_text().splitlines()
                total += next(int(line.split()[1]) * 1024 for line in lines if line.startswith('VmRSS:'))
            except (OSError, StopIteration):
                continue
    return total


def run_job(request, stream):
    argv = request.get("argv")
    timeout = request.get("timeout", 1800)
    peak = request.get("disk_bytes", GIB)
    agent = public_label(request.get("agent"), "Agent non identifié")
    project = public_label(request.get("project"), "Projet non identifié")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and "\x00" not in x for x in argv):
        raise ValueError("argv must be a nonempty string array")
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ValueError("timeout must be 1..3600 seconds")
    if not isinstance(peak, int) or not 0 < peak <= JOB_LIMIT:
        raise ValueError("declared disk peak must fit the dedicated volume (maximum 32 GiB)")
    ident = uuid.uuid4().hex
    with resources.reserve(ROOT, ident, request, capacity) as lease:
        with resources.resource_lock(ROOT):
            if not ensure_receipt_reserve():
                raise RuntimeError("receipt headroom unavailable; retrieve and release completed jobs")
            cache = ROOT / 'cache'
            cache.mkdir(exist_ok=True)
            task_cache = dependency_cache.scope(ROOT, request)
            if not (ROOT / '.cache-accounting.json').exists():
                resources.write_json(ROOT / '.cache-accounting.json', usage(cache))
        reserve_ram = int(float(os.environ.get("USEVPS_HOST_RAM_RESERVE_GIB", "1")) * GIB)
        path = ROOT / "jobs" / ident
        work = path / "work"
        work.mkdir(parents=True)
        result = {"id": path.name, "status": "running", "argv": argv, "timeout": timeout, "started": time.time(),
                  "agent": agent, "project": project, "runner": process_token(os.getpid()),
                  "reservation": {k: lease.value[k] for k in ('cpus', 'ram_bytes', 'disk_bytes', 'ram_source')}}
        receipt(path, result)
        try:
            with Transfer(ROOT, stream, 'upload', ident, agent, project) as transfer:
                extract(transfer, work)
            for directory in (work / ".tmp", work / ".home", ROOT / "cache"):
                directory.mkdir(exist_ok=True)
            env = os.environ.copy()
            env.update(HOME=str(work / ".home"), TMPDIR=str(work / ".tmp"),
                       XDG_CACHE_HOME=str(task_cache), PIP_CACHE_DIR=str(task_cache / "pip"),
                       npm_config_cache=str(task_cache / "npm"),
                       npm_config_store_dir=str(task_cache / "pnpm"),
                       UV_CACHE_DIR=str(task_cache / "uv"), GRADLE_USER_HOME=str(task_cache / "gradle"),
                       USEVPS_JOB_DISK_BYTES=str(peak), USEVPS_JOB_ID=ident,
                       USEVPS_CPU_CAPACITY=str(resources.cpu_capacity()))
            env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "")
                                         + " -Djava.io.tmpdir=" + str(work / ".tmp")
                                         + " -Duser.home=" + str(work / ".home")).strip()
            # Reparent detached descendants here so only this task is cleaned up.
            if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
                raise RuntimeError("cannot enable child subreaper")
            os.nice(15)
            proc = subprocess.Popen(argv, cwd=work, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            overflow = threading.Event()
            capture_error = []

            def capture():
                written = 0
                try:
                    with (path / "stdout.log").open("wb") as log:
                        while True:
                            chunk = proc.stdout.read(65536)
                            if not chunk:
                                break
                            allowed = max(0, LOG_LIMIT - written)
                            log.write(chunk[:allowed])
                            written += min(allowed, len(chunk))
                            if len(chunk) > allowed:
                                overflow.set()
                except OSError as exc:
                    capture_error.append(str(exc))
                    overflow.set()

            reader = threading.Thread(target=capture, daemon=True)
            reader.start()
            start = time.monotonic()
            reason = None
            def measure():
                state = capacity()
                pids = descendants()
                # rusage retains very short, exited children's peaks between samples.
                child_peak = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * (1 if sys.platform == 'darwin' else 1024)
                lease.update(process_memory(pids | {os.getpid()}), usage(path), pids, usage(cache),
                             child_peak + process_memory({os.getpid()}))
                if state['free_bytes'] < state['reserve_bytes'] or state['used_bytes'] > state['budget_bytes'] or lease.value['disk_used'] > peak:
                    return state, 'disk limit'
                if lease.value['ram_peak'] > lease.value['ram_bytes']:
                    return state, 'task memory limit'
                if state['host_available_ram'] < reserve_ram:
                    return state, 'host memory pressure'
                return state, None
            while proc.poll() is None:
                _, resource_reason = measure()
                if overflow.is_set():
                    reason = "log limit"
                elif time.monotonic() - start > timeout:
                    reason = "timeout"
                elif resource_reason:
                    reason = resource_reason
                if reason:
                    break
                time.sleep(0.25)
            # Measure before cleanup too: a very short task must not evade its guard.
            _, resource_reason = measure()
            reason = reason or resource_reason
            stop_children()
            rc = proc.wait(timeout=5)
            reader.join(timeout=5)
            if overflow.is_set():
                reason = "log capture failed" if capture_error else "log limit"
            final, resource_reason = measure()
            reason = reason or resource_reason
            result.update(status="passed" if rc == 0 and not reason else "failed",
                          returncode=rc, reason=reason, ended=time.time(),
                          resources={"declared_disk_bytes": peak, "growth_bytes": lease.value['disk_used'],
                                     "ram_bytes": lease.value['ram_used'], "cpus": lease.value['cpus'],
                                     "ram_peak_bytes": lease.value['ram_peak'], "ram_sample_seconds": 0.25,
                                     "total_used_bytes": final["used_bytes"], "physical_free_bytes": final["free_bytes"]})
        except Exception as exc:
            stop_children()
            result.update(status="failed", reason=str(exc), returncode=1, ended=time.time())
        if result.get('resources') and (result['status'] == 'passed' or result.get('reason') == 'task memory limit'):
            try:
                resources.remember_ram(ROOT, request, max(result['resources']['ram_peak_bytes'],
                                       lease.value['ram_bytes'] if result.get('reason') == 'task memory limit' else 0),
                                       result['status'] == 'passed')
                result['ram_profile_saved'] = True
            except (OSError, ValueError, RuntimeError):
                result['ram_profile_saved'] = False
        try:
            receipt(path, result)
        except OSError:
            result.update(status="failed", reason="quota prevents receipt", returncode=1)
        return result


def fetch(request, stream):
    path = job_path(request["id"])
    if 'diagnostic.json' in request.get('paths', []) and request['paths'] != ['diagnostic.json']:
        raise ValueError('fetch failure details separately from job files')
    if request.get('paths') == ['diagnostic.json']:
        report = diagnostics.get(ROOT, path)
        with Transfer(ROOT, stream, 'download', path.name, report['agent'], report['project']) as transfer:
            transfer.write(diagnostics.bundle(report))
        return
    state = json.loads((path / "receipt.json").read_text())
    if state["status"] == "running":
        raise ValueError("job still running")
    paths = request.get("paths", ["stdout.log", "receipt.json"])
    files = {}
    for name in paths:
        rel = safe_path(name)
        source = path.joinpath(*rel.parts)
        if source.is_symlink() or not source.resolve().is_relative_to(path.resolve()):
            raise ValueError("unsafe result path")
        candidates = [source] if source.is_file() else source.rglob("*")
        for file in candidates:
            if file.is_symlink():
                raise ValueError("result symlinks are forbidden")
            if file.is_file():
                name = str(file.relative_to(path))
                safe_path(name)
                files[name] = file
    if not files or len(files) > 10000 or sum(p.stat().st_size for p in files.values()) > FETCH_LIMIT:
        raise ValueError("result must contain 1..10000 files and <= 256 MiB")
    manifest = {}
    for name, file in files.items():
        with file.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        manifest[name] = {"bytes": file.stat().st_size, "sha256": digest}
    with Transfer(ROOT, stream, 'download', path.name, state.get('agent'), state.get('project')) as transfer, \
            tarfile.open(fileobj=transfer, mode="w|") as archive:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for name, file in files.items():
            archive.add(file, arcname=name, recursive=False)


def release_job(request):
    # Shared deployment lock permits independent completed-task cleanup during work.
    with (ROOT / ".queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with resources.resource_lock(ROOT):
            path = job_path(request["id"])
            if (any(r['id'] == path.name for r in resources.leases(ROOT))
                    or json.loads((path / "receipt.json").read_text())["status"] == "running"):
                raise ValueError("cannot release active task")
            original = path.name
            trash = ROOT / ('.released-' + original)
            path.rename(trash)
            path = trash
        shutil.rmtree(path)
        with resources.resource_lock(ROOT):
            ensure_receipt_reserve()
        return {"released": original}


def main():
    action = sys.argv[1]
    if not volume_ok():
        raise RuntimeError("required volume is not mounted")
    protect_quota()
    if action == "serve":
        validate_environment()
        ensure_receipt_reserve()
        for state in (ROOT / "jobs").glob("*/receipt.json"):
            data = json.loads(state.read_text())
            if data.get("status") == "running":
                data.update(status="failed", reason="worker restarted", returncode=1, ended=time.time())
                receipt(state.parent, data)
        for expired in (ROOT / '.leases').glob('*.json'):
            expired.unlink()
        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path != "/health":
                        raise ValueError("unknown health path")
                    validate_environment()
                    result = {"ok": True}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                self.send_response(200 if result["ok"] else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, *_):
                pass
        class Server(ThreadingHTTPServer):
            last_cleanup = 0
            cleanup = None

            def service_actions(self):
                if (time.monotonic() - self.last_cleanup >= 3600
                        and (self.cleanup is None or not self.cleanup.is_alive())):
                    # Deleting a large cold cache must not stall HTTP health checks.
                    self.cleanup = threading.Thread(target=dependency_cache.maintain, args=(ROOT,), daemon=True)
                    self.cleanup.start()
                    self.last_cleanup = time.monotonic()

        Server(("0.0.0.0", 8080), Health).serve_forever()
    elif action == "status":
        print(json.dumps(public_status()))
    elif action == "run":
        result = run_job(read_request(), sys.stdin.buffer)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1
    elif action == "fetch":
        fetch(read_request(), sys.stdout.buffer)
    elif action == "release":
        print(json.dumps(release_job(read_request())))
    else:
        raise ValueError("unknown operation")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          **({'admission': exc.admission} if isinstance(exc, resources.AdmissionError) else {})}), file=sys.stderr)
        sys.exit(1)
