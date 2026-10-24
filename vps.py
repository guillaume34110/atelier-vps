#!/usr/bin/env python3
"""Client for the restricted usevps SSH gateway; no third-party dependencies."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from worker import FETCH_LIMIT, GIB, INPUT_LIMIT, safe_path
from telemetry import public_label
import client_config

SKIP = {".git", "node_modules", ".venv", "venv", ".cache", "__pycache__",
        "output", "dist", "build", ".next", "coverage", "CLAUDE.md", ".ssh",
        ".npmrc", ".pypirc", ".netrc", ".aws", ".kube", ".DS_Store"}
SKIP.update({'deployment.local.json', 'docker-compose.local.yaml', 'auth.json', 'credentials.json'})


def inputs(project):
    total = 0
    for base, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            file = Path(base) / name
            if (name in SKIP or name == ".env" or name.startswith((".env.", 'id_ed25519', 'id_rsa'))
                    or name.endswith((".pem", ".key")) or file.is_symlink()):
                continue
            total += file.stat().st_size
            if total > INPUT_LIMIT:
                raise ValueError("input exceeds 64 MiB; select a smaller source directory")
            yield file, str(file.relative_to(project))


def temp_root():
    value = os.environ.get("TASK_WORKSPACE")
    if not value or not Path(value).is_dir():
        raise ValueError("run through workspace-budget.py run PROJECT -- rtk proxy python3 vps.py ...")
    return Path(value)


def transport(host, operation, request=None, payload=None, output=None, source=None):
    client_config.valid_alias(host)
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", host, operation]
    if shutil.which('rtk'):
        command = ['rtk', 'proxy', *command]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output or subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        if request is not None:
            proc.stdin.write(json.dumps(request).encode() + b"\n")
        if payload:
            with payload.open("rb") as src:
                shutil.copyfileobj(src, proc.stdin, 65536)
        if source is not None:
            with tarfile.open(fileobj=proc.stdin, mode='w|') as archive:
                for file, name in inputs(source):
                    archive.add(file, arcname=name, recursive=False)
        proc.stdin.close()
        proc.stdin = None
        result, errors = proc.communicate(timeout=3700)
        if proc.returncode and not result:
            raise RuntimeError(errors.decode(errors="replace")[-2000:])
        return proc.returncode, result


def unpack_result(archive, destination):
    if destination.exists():
        raise ValueError("result destination already exists")
    destination.mkdir()
    manifest = None
    extracted = {}
    total = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                rel = safe_path(member.name)
                if not member.isfile() or member.size < 0:
                    raise ValueError("invalid result member")
                total += member.size
                if total > FETCH_LIMIT + 4 * 1024 ** 2:
                    raise ValueError("result exceeds transfer limit")
                source = tar.extractfile(member)
                if member.name == "manifest.json":
                    if manifest is not None or member.size > 4 * 1024 ** 2:
                        raise ValueError("invalid manifest")
                    manifest = json.load(source)
                else:
                    target = destination.joinpath(*rel.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with target.open("xb") as out:
                        while chunk := source.read(65536):
                            out.write(chunk)
                            digest.update(chunk)
                    extracted[member.name] = {"bytes": member.size, "sha256": digest.hexdigest()}
        if not manifest or manifest != extracted:
            raise ValueError("result checksum mismatch")
    except Exception:
        # This directory was created for this transfer and has no prior contents.
        shutil.rmtree(destination)
        raise
    return len(extracted)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=client_config.host())
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--disk-gib", type=float, default=1)
    run.add_argument("--agent", default=os.environ.get("USEVPS_AGENT"), help="Public agent/task label shown in the Mac widget")
    run.add_argument("--cpus", type=int, default=1, help="reserved cores; unused cores are lent to active jobs")
    run.add_argument("--ram-gib", type=float, default=1, help="reserved RAM, monitored per task")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("id")
    fetch.add_argument("paths", nargs="+", help="e.g. work/result.txt stdout.log receipt.json")
    release = commands.add_parser("release")
    release.add_argument("id")
    release.add_argument("--confirmed", action="store_true", required=True)
    args = parser.parse_args()
    if args.action == "status":
        rc, data = transport(args.host, "status")
        print(data.decode().strip())
        return rc
    if args.action == "release":
        rc, data = transport(args.host, "release", {"id": args.id})
        print(data.decode().strip())
        return rc
    if args.action == 'run':
        argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
        if not argv or not args.source.is_dir():
            raise ValueError('source directory and command are required')
        # Validate the complete manifest before SSH; stream it without a local copy.
        list(inputs(args.source.resolve()))
        rc, data = transport(args.host, 'run', {'argv': argv, 'timeout': args.timeout,
                             'agent': public_label(args.agent, 'Agent non identifié'),
                             'project': public_label(args.source.resolve().name, 'Projet non identifié'),
                             'cpus': args.cpus, 'ram_bytes': int(args.ram_gib * GIB),
                             'disk_bytes': int(args.disk_gib * GIB)}, source=args.source.resolve())
        print(data.decode().strip())
        return rc
    root = temp_root()
    with tempfile.TemporaryDirectory(prefix="vps-transfer-", dir=root) as staging:
        staging = Path(staging)
        archive = staging / "result.tar"
        with archive.open("wb") as out:
            rc, _ = transport(args.host, "fetch", {"id": args.id, "paths": args.paths}, output=out)
        if rc:
            raise RuntimeError("fetch failed; task remains on server")
        destination = root / ("vps-result-" + args.id)
        count = unpack_result(archive, destination)
        print(json.dumps({"verified_files": count, "directory": str(destination), "released": False}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
