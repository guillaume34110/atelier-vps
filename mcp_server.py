"""Local stdio bridge. No public listener, cloud key or administrator credential."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from mcp.server import MCPServer

import client_config
import vps
from telemetry import public_label

ROOT = Path(__file__).resolve().parent
server = MCPServer('Atelier VPS', instructions='Prefer this VPS for compatible Linux builds, tests, Chromium and file processing. '
                   'Check vps_status, reserve CPU/RAM/disk with vps_run, fetch and verify useful results before release. '
                   'Keep macOS, Metal, GPU, Docker daemon and Android emulator work on a compatible machine. '
                   'Never send secrets or local dependencies; a refused or failed job is not a passed check.')


def response(action, request=None, source=None):
    rc, data = vps.transport(client_config.host(), action, request, source=source)
    if not data or len(data) > 65536:
        raise RuntimeError('invalid bounded gateway response')
    value = json.loads(data)
    if rc:
        value['ok'] = False
    return value


@server.tool(structured_output=True)
def vps_status() -> dict[str, Any]:
    """Read real VPS health, running agents and available CPU/RAM/disk reservations."""
    return response('status')


@server.tool(structured_output=True)
def vps_run(source: str, argv: list[str], agent: str, disk_gib: float,
            ram_gib: float = 1, cpus: int = 1, timeout: int = 900) -> dict[str, Any]:
    """Run a Linux ARM64 task, streaming filtered sources without a local archive.

    Reserve the full peak, including dependencies, caches, downloads and outputs.
    At most 4 jobs share the pool; unused cores are lent. This call waits for the
    result (maximum 3600s); configure this MCP server's tool timeout to 3700s.
    Results stay on the VPS until fetched, verified and explicitly released.
    """
    directory = Path(source).expanduser().resolve()
    if not directory.is_dir() or not argv or not all(isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('an existing source directory and a nonempty argument array are required')
    if not all(math.isfinite(x) and x > 0 for x in (disk_gib, ram_gib)):
        raise ValueError('finite, positive disk and RAM reservations are required')
    list(vps.inputs(directory))
    state = vps_status()
    if not state.get('ok') or not state.get('admit'):
        raise RuntimeError('VPS not admitting work; wait for resources or choose a compatible machine')
    return response('run', {'argv': argv, 'agent': public_label(agent, 'Agent'),
                           'project': public_label(directory.name, 'Project'), 'timeout': timeout,
                           'disk_bytes': math.ceil(disk_gib * vps.GIB),
                           'ram_bytes': math.ceil(ram_gib * vps.GIB), 'cpus': cpus}, directory)


@server.tool(structured_output=True)
def vps_fetch(job_id: str, paths: list[str], project: str) -> dict[str, Any]:
    """Fetch <=256 MiB with SHA256 verification into the local project's task workspace.

    Preserve useful files outside task-tmp before cleanup. Never overwrite an
    existing task workspace; inspect and finish that prior pass first.
    """
    vps.safe_path(job_id)
    directory = Path(project).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError('project directory must exist')
    helper = Path.home() / '.local/bin/workspace-budget.py'
    args = [sys.executable, str(ROOT / 'vps.py'), 'fetch', job_id, *paths]
    env = os.environ.copy()
    if helper.is_file():
        command = [sys.executable, str(helper), '--temp-gib', '0.6', 'run', str(directory), '--', *args]
        if shutil.which('rtk'):
            command = ['rtk', 'proxy', *command]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        log = directory / 'output/task-tmp/stdout.log'
        if completed.returncode:
            raise RuntimeError('bounded fetch failed; inspect the preserved task workspace')
        lines = log.read_text().splitlines()
        return next(json.loads(line) for line in reversed(lines) if line.startswith('{"verified_files"'))
    target = directory / 'output/task-tmp'
    free = shutil.disk_usage(directory)
    if free.free < max(free.total // 10, vps.GIB) + int(0.6 * vps.GIB):
        raise RuntimeError('insufficient local disk reserve for a verified transfer')
    target.mkdir(parents=True, exist_ok=False)
    env['TASK_WORKSPACE'] = str(target)
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return json.loads(result.stdout)


@server.tool(structured_output=True)
def vps_release(job_id: str, results_preserved: bool) -> dict[str, Any]:
    """Delete only this completed remote job after its useful results are preserved."""
    if not results_preserved:
        raise ValueError('fetch, verify and preserve useful results before release')
    return response('release', {'id': job_id})


if __name__ == '__main__':
    server.run(transport='stdio')
