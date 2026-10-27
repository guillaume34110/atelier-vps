#!/usr/bin/python3
"""Root-owned, sudo-authorized gateway for the pinned Coolify worker only."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

# Compose 2.6 ignores oom_score_adj; enforce it in each unprivileged process.
WORKER_EXEC = ["sh", "-c", 'echo 500 > /proc/self/oom_score_adj && exec python3 /app/worker.py "$@"', "usevps"]
DESKTOP_TMPFS = 'rw,noexec,nosuid,nodev,size=8388608,mode=0700,uid=10001,gid=10001'


def server_identity():
    value = Path('/etc/machine-id').read_bytes().strip()
    if len(value) != 32 or any(c not in b'0123456789abcdef' for c in value):
        raise ValueError('stable server identity unavailable')
    return hashlib.sha256(value).hexdigest()


def main():
    operation = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if operation not in {"status", "plan", "run", "fetch", "release", "desktop"}:
        raise ValueError("unsupported worker operation")
    config = json.loads(Path("/etc/usevps-gateway.json").read_text())
    process_ceiling = config.get('pids_limit', 512)
    if type(process_ceiling) is not int or process_ceiling not in (512, 768):
        raise ValueError('unsupported operator process ceiling')
    memory_ceiling = config.get('memory_limit_bytes', 5 * 1024 ** 3)
    if type(memory_ceiling) is not int or not 64 * 1024 ** 2 <= memory_ceiling <= 48 * 1024 ** 3:
        raise ValueError('unsupported operator memory ceiling')
    settings = Path('/etc/atelier-vps-team.json')
    tenant = None
    pending = False
    if settings.exists():
        state = json.loads(settings.read_text())
        principal = os.environ.get('SUDO_USER')
        matches = [(name, value) for name, value in state['tenants'].items()
                   if value['ssh_user'] == principal and value['enabled']]
        if len(matches) != 1:
            raise PermissionError('no developer workspace for this authenticated SSH user')
        name, tenant = matches[0]
        config = {**config, 'container': tenant['container'], 'workspace': tenant['workspace']}
        transaction = Path('/etc/atelier-vps-team.pending.json')
        pending = transaction.exists() and name in json.loads(transaction.read_text())['affected']
        if pending and operation == 'run':
            raise RuntimeError('developer settings update pending; existing results are preserved')
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
    if host["Privileged"] or not host["ReadonlyRootfs"] or not 0 < host["Memory"] <= memory_ceiling:
        raise ValueError("worker isolation or memory limit missing")
    if host.get("MemorySwap") != host["Memory"] or not 0 < host.get("PidsLimit", 0) <= process_ceiling or not 0 < host.get("CpuShares", 0) <= 128:
        raise ValueError("worker swap, process or CPU limits missing")
    if "ALL" not in (host.get("CapDrop") or []) or not any(x.startswith("no-new-privileges") for x in host.get("SecurityOpt") or []):
        raise ValueError("worker privilege restrictions missing")
    temporary = host.get('Tmpfs') or {}
    if (temporary not in ({}, {'/tmp': DESKTOP_TMPFS})
            or host['PidsLimit'] > 256 and not temporary
            or operation == 'desktop' and (not temporary or host['PidsLimit'] != process_ceiling)):
        raise ValueError('unexpected temporary filesystem or missing bounded GUI process capacity')
    if tenant:
        if host['Memory'] != tenant['ram_bytes']:
            raise ValueError('worker does not match the developer RAM allocation')
        if tenant.get('network') and set(info.get('NetworkSettings', {}).get('Networks', {})) != {tenant['network']}:
            raise ValueError('worker does not match the private developer network')
        if 'cpu_set' in tenant and host.get('CpusetCpus', '') != tenant['cpu_set']:
            raise ValueError('worker does not match the developer CPU core set')
        if state['policy']['mode'] == 'allocated':
            if host['CpuShares'] != tenant['cpu_shares']:
                raise ValueError('worker does not match the developer CPU priority')
            quota = host.get('NanoCpus', 0) / 1e9
            if not quota and host.get('CpuPeriod', 0) > 0:
                quota = host.get('CpuQuota', 0) / host['CpuPeriod']
            if abs(quota - tenant['cpus']) > 0.000001:
                raise ValueError('worker does not match the developer CPU allocation')
    expected = {"/workspace": (config["workspace"], True),
                "/volume-stat": (str(Path(config["mount"]) / ".usevps-stat"), False)}
    dev = Path(config["mount"]) / ".usevps-dev"
    for destination, source in {"/usr": "usr", "/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib",
                                "/lib64": "usr/lib64", "/etc": "etc", "/opt": "opt"}.items():
        expected[destination] = (str(dev / source), False)
    if (len(mounts) != len(expected) or {m["Destination"] for m in mounts} != set(expected)
            or any(m.get("Type") != "bind" or (m["Source"], m["RW"]) != expected[m["Destination"]] for m in mounts)):
        raise ValueError("unexpected host mounts")
    server_id = server_identity()
    flags = ['--env', 'USEVPS_SERVER_ID=' + server_id]
    if pending:
        flags.extend(['--env', 'USEVPS_MAINTENANCE=1'])
    os.execvp("docker", ["docker", "exec", "-i", *flags, "--user", "10001:10001", names[0], *WORKER_EXEC, operation])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
