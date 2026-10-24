#!/usr/bin/python3
"""Enable a hard project quota on the newly added, identified Hetzner volume."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

MOUNT = Path(os.environ.get('USEVPS_VOLUME_MOUNT', '/mnt/atelier-vps'))
WORKSPACE = MOUNT / "usevps"
VOLUME_ID = os.environ.get('USEVPS_VOLUME_ID', '')
PROJECT_ID = int(os.environ.get('USEVPS_PROJECT_ID', '34110'))
GIB = 1024 ** 3
MIB = 1024 ** 2
RESERVE_BYTES = GIB // 2


def quota_bytes(physical_free, project_used):
    budget = ((physical_free + project_used - RESERVE_BYTES) // MIB) * MIB
    if budget <= project_used + MIB:
        raise RuntimeError("insufficient volume headroom; preserve and retrieve existing results")
    return budget


def run(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(str(args[:3]) + ": " + p.stderr[-1000:])
    return p.stdout


def fstab_with_quota(text):
    result, count = [], 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == str(MOUNT) and not line.lstrip().startswith("#"):
            opts = fields[3].split(",")
            if "prjquota" not in opts:
                opts.append("prjquota")
            fields[3] = ",".join(opts)
            line = "\t".join(fields)
            count += 1
        result.append(line)
    if count != 1:
        raise ValueError("expected exactly one existing volume fstab entry")
    return "\n".join(result) + "\n"


def main():
    if sys.argv[1:] != ["--apply"] or os.geteuid() != 0:
        raise ValueError("root and --apply are required")
    if not VOLUME_ID or MOUNT == Path('/') or not MOUNT.is_absolute():
        raise ValueError('explicit added-volume mount and UUID are required')
    mounted_id = run(["findmnt", "-n", "-o", "UUID", "--mountpoint", str(MOUNT)]).strip()
    if mounted_id != VOLUME_ID:
        raise ValueError("volume identity mismatch")
    source = run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(MOUNT)]).strip()
    fstab = Path("/etc/fstab")
    original = fstab.read_text()
    updated = fstab_with_quota(original)
    features = run(["tune2fs", "-l", source])
    quota_ready = "project" in features.split("Filesystem features:", 1)[1].splitlines()[0].split()
    if not shutil.which("setquota"):
        run(["apt-get", "install", "--no-install-recommends", "-y", "quota"])
    if not quota_ready:
        if set(os.listdir(MOUNT)) - {"lost+found"}:
            raise ValueError("feature activation is permitted only on the still-empty new volume")
        run(["umount", str(MOUNT)])
        try:
            run(["tune2fs", "-O", "project,quota", "-Q", "prjquota", source])
        finally:
            run(["mount", "-o", "prjquota", source, str(MOUNT)])
    else:
        options = run(["findmnt", "-n", "-o", "OPTIONS", "--mountpoint", str(MOUNT)])
        if "prjquota" not in options:
            run(["mount", "-o", "remount,prjquota", str(MOUNT)])
    backup = Path("/etc/fstab.usevps-before")
    if not backup.exists():
        shutil.copy2(fstab, backup)
    if updated != original:
        tmp = Path("/etc/.fstab.usevps.tmp")
        tmp.write_text(updated)
        tmp.chmod(0o644)
        tmp.replace(fstab)
    WORKSPACE.mkdir(exist_ok=True)
    run(["chattr", "-p", str(PROJECT_ID), "+P", str(WORKSPACE)])
    # Only the additional data volume is dedicated to the worker; never the system disk.
    run(["tune2fs", "-m", "0", source])
    physical = os.statvfs(MOUNT)
    project = os.statvfs(WORKSPACE)
    if project.f_blocks < physical.f_blocks:
        project_used = (project.f_blocks - project.f_bfree) * project.f_frsize
    else:
        project_used = sum((Path(base) / n).lstat().st_blocks * 512
                           for base, dirs, files in os.walk(WORKSPACE) for n in dirs + files)
    budget = quota_bytes(physical.f_bavail * physical.f_frsize, project_used)
    run(["setquota", "-P", str(PROJECT_ID), "0", str(budget // 1024), "0", "500000", str(MOUNT)])
    os.chown(WORKSPACE, 10001, 10001)
    marker = WORKSPACE / ".volume-id"
    if marker.exists() and marker.read_text().strip() != VOLUME_ID:
        raise ValueError("existing marker mismatch")
    if not marker.exists():
        marker.write_text(VOLUME_ID + "\n")
    os.chown(marker, 0, 0)
    marker.chmod(0o444)
    stats = MOUNT / ".usevps-stat"
    if stats.is_symlink():
        raise ValueError("capacity probe must be a real empty directory")
    stats.mkdir(exist_ok=True)
    if any(stats.iterdir()):
        raise ValueError("capacity probe must remain empty")
    os.chown(stats, 0, 0)
    stats.chmod(0o000)
    free = shutil.disk_usage(MOUNT).free
    if free < RESERVE_BYTES:
        raise RuntimeError("unexpected volume occupancy; inspect before admitting tasks")
    print(json.dumps({"volume": VOLUME_ID, "workspace": str(WORKSPACE), "hard_quota_gib": round(budget / GIB, 3),
                      "reserve_gib": 0.5, "free_gib": round(free / GIB, 2)}))


if __name__ == "__main__":
    main()
