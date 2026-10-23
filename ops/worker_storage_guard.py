#!/usr/bin/env python3
"""Check a planned write against this task, its quota and the dedicated volume."""
import argparse
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, "/app")
import worker


def remaining_budget(state, declared, initial_used):
    return min(state["workspace_free_bytes"] - worker.MIB,
               state["free_bytes"] - state["reserve_bytes"],
               declared - max(0, state["used_bytes"] - initial_used))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", type=Path, default=Path.cwd())
    parser.add_argument("--require-gib", type=float, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.require_gib) or args.require_gib < 0:
        parser.error("a finite, nonnegative peak is required")
    if not args.volume.is_dir() or not args.volume.resolve().is_relative_to(worker.ROOT.resolve()):
        parser.error("destination must be in the worker workspace")
    if "USEVPS_JOB_DISK_BYTES" not in os.environ:
        parser.error("run this check inside a task with a declared disk peak")
    state = worker.status()
    remaining = remaining_budget(state, int(os.environ["USEVPS_JOB_DISK_BYTES"]),
                                 int(os.environ["USEVPS_JOB_INITIAL_USED"]))
    needed = math.ceil(args.require_gib * worker.GIB)
    ok = remaining >= needed
    print(json.dumps({"ok": ok, "remaining_bytes": remaining, "planned_bytes": needed,
                      "reserve_bytes": state["reserve_bytes"]}))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
