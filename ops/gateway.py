#!/usr/bin/python3
"""Root-owned, sudo-authorized gateway for the pinned Coolify worker only."""
import json
import os
from pathlib import Path
import subprocess
import sys

# Compose 2.6 ignores oom_score_adj; enforce it in each unprivileged process.
WORKER_EXEC = ["sh", "-c", 'echo 500 > /proc/self/oom_score_adj && exec python3 /app/worker.py "$@"', "usevps"]


def main():
    operation = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if operation not in {"status", "run", "fetch", "release"}:
        raise ValueError("only status, run, fetch and release are allowed")
    config = json.loads(Path("/etc/usevps-gateway.json").read_text())
    mount = subprocess.check_output(["findmnt", "-n", "-o", "UUID,OPTIONS", "--mountpoint", config["mount"]], text=True).strip().split()
    if len(mount) != 2 or mount[0] != config["volume_id"] or "prjquota" not in mount[1].split(","):
        raise ValueError("expected volume or project quota is not mounted")
    names = subprocess.check_output(["docker", "ps", "-q", "--filter", "name=^/" + config["container"] + "$"], text=True).split()
    if len(names) != 1:
        raise ValueError("expected exactly one worker")
    info = json.loads(subprocess.check_output(["docker", "inspect", names[0]], text=True))[0]
    if info.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise ValueError("worker is not healthy yet")
    host = info["HostConfig"]
    mounts = info["Mounts"]
    if host["Privileged"] or not host["ReadonlyRootfs"] or not 0 < host["Memory"] <= 5 * 1024 ** 3:
        raise ValueError("worker isolation or memory limit missing")
    if host.get("MemorySwap") != host["Memory"] or not 0 < host.get("PidsLimit", 0) <= 256 or not 0 < host.get("CpuShares", 0) <= 128:
        raise ValueError("worker swap, process or CPU limits missing")
    if "ALL" not in (host.get("CapDrop") or []) or not any(x.startswith("no-new-privileges") for x in host.get("SecurityOpt") or []):
        raise ValueError("worker privilege restrictions missing")
    expected = {"/workspace": (config["workspace"], True),
                "/volume-stat": (str(Path(config["mount"]) / ".usevps-stat"), False)}
    if (len(mounts) != 2 or {m["Destination"] for m in mounts} != set(expected)
            or any(m.get("Type") != "bind" or (m["Source"], m["RW"]) != expected[m["Destination"]] for m in mounts)):
        raise ValueError("unexpected host mounts")
    os.execvp("docker", ["docker", "exec", "-i", "--user", "10001:10001", names[0], *WORKER_EXEC, operation])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
