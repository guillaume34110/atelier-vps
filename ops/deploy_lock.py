#!/usr/bin/python3
"""Hold the worker's shared queue lock throughout an administrator deployment."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


@contextmanager
def deployment_lock(workspace):
    with (workspace / ".queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    if sys.argv[1:] != ["--hold"] or os.geteuid() != 0:
        raise ValueError("administrator and --hold are required")
    config = json.loads(Path("/etc/usevps-gateway.json").read_text())
    actual = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--mountpoint", config["mount"]], text=True).strip()
    if actual != config["volume_id"]:
        raise ValueError("volume identity mismatch")
    with deployment_lock(Path(config["workspace"])):
        print(json.dumps({"state": "deployment_locked", "container": config["container"]}), flush=True)
        if sys.stdin.readline().strip() != "release":
            raise RuntimeError("deployment lock closed; verify deployment state")
    print(json.dumps({"state": "released"}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
