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


def response(action, request=None, source=None, host=None):
    return vps.json_request(host or client_config.host(), action, request, source=source,
                            timeout=3700 if action=='run' else 30)[1]


@server.tool(structured_output=True)
def vps_status(server: str | None = None) -> dict[str, Any]:
    """Read fresh capacity on connected servers and deduplicated Global totals.

    Offline/stale/unidentified servers do not contribute to a multi-server sum.
    A job must fit one worker; combined RAM is never one shared address space.
    """
    return vps.fleet_status(server)


@server.tool(structured_output=True)
def vps_server_add(identifier: str, name: str, team: str, ssh_alias: str,
                   admin_ssh_alias: str | None = None) -> dict[str, Any]:
    """Register an already configured restricted SSH connection after authentication.

    One team belongs to each server. Aliases keep their local credentials; no
    private key is read or transferred. Optional administrator access must
    authenticate separately and match the same physical server.
    """
    import onboarding
    return onboarding.add_alias(identifier,name,team,ssh_alias,admin_ssh_alias)


@server.tool(structured_output=True)
def vps_agent_desktop(action: str = 'status', server: str | None = None,
                      session_id: str | None = None, confirmed_stop: bool = False,
                      provider: str = 'chatgpt', ram_gib: float | None = None) -> dict[str, Any]:
    """Control the reserved VPS desktop containing official Codex and Claude programs.

    Actions: status, start, stop. Start reserves a one-hour session in the normal
    pool. Open it from the widget's Agents tab and sign in there with the user's
    provider account; no inference API key is needed. Never return a viewer
    password or authentication code to the conversation. Stop requires explicit
    confirmation that its tasks are finished; profiles and files are preserved.
    Omit RAM to reuse measured peaks. An explicit known workload peak is allowed;
    never shrink a reservation merely to fit a refusal or add a 2x margin.
    """
    if action not in {'status', 'start', 'stop'} or action == 'stop' and not confirmed_stop:
        raise ValueError('use status/start, or confirm the finished desktop before stopping it')
    import desktop_client
    profile, result = desktop_client.request(server, action, session_id, provider, ram_gib)
    allowed = {'available', 'active', 'id', 'provider', 'phase', 'started', 'expires', 'ram_bytes', 'disk_bytes', 'stopping'}
    value = {'server': profile['id'], **{key: result[key] for key in allowed if key in result}}
    if result.get('id'):
        value['job_ref'] = profile['id'] + ':' + result['id']
    return value


@server.tool(structured_output=True)
def vps_connect_from_clipboard() -> dict[str, Any]:
    """Accept the invitation explicitly copied by the user, without echoing it.

    Use only when asked to connect that copied invitation. The Mac widget or
    CLI standard input handles invitations on machines without a clipboard.
    """
    if sys.platform!='darwin':
        raise RuntimeError('paste the invitation into the widget, or use atelier-vps connect --stdin')
    import onboarding
    raw=onboarding.bounded_output(['/usr/bin/pbpaste'],limit=8193,timeout=5)
    return onboarding.accept(raw.decode().strip())


@server.tool(structured_output=True)
def vps_run(source: str, argv: list[str], agent: str, disk_gib: float,
            ram_gib: float | None = None, cpus: int = 1, timeout: int = 900,
            server: str | None = None, arch: str = 'any') -> dict[str, Any]:
    """Run a Linux task, streaming filtered sources without a local archive.

    Reserve the full peak, including dependencies, caches, downloads and outputs.
    At most 4 jobs share the pool; unused cores are lent. This call waits for the
    result (maximum 3600s); configure this MCP server's tool timeout to 3700s.
    Results stay on the VPS until fetched, verified and explicitly released.
    Omit ram_gib to learn this workload's measured peaks with 25% + 64 MiB
    headroom (first run: up to 1 GiB). Explicit RAM overrides are respected.
    RAM is never silently reduced to fit a busy pool. New/heavier work should
    reserve its known full peak; dependency-lock changes start a new profile.
    Pin arch to aarch64 or x86_64 for architecture-specific work. Without
    server, select an authenticated compatible worker that can admit
    this complete claim. Use the returned job_ref for fetch/release. A failed
    executed command is never automatically retried on another server.
    """
    directory = Path(source).expanduser().resolve()
    if not directory.is_dir() or not argv or not all(isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('an existing source directory and a nonempty argument array are required')
    if not all(math.isfinite(x) and x > 0 for x in (disk_gib, ram_gib) if x is not None):
        raise ValueError('finite, positive disk and RAM reservations are required')
    list(vps.inputs(directory, math.ceil(disk_gib * vps.GIB)))
    return vps.run_on_server({'argv': argv, 'agent': public_label(agent, 'Agent'),
                           'project': public_label(directory.name, 'Project'), 'timeout': timeout,
                           'disk_bytes': math.ceil(disk_gib * vps.GIB),
                           'ram_bytes': None if ram_gib is None else math.ceil(ram_gib*vps.GIB), 'cpus': cpus,
                           'dependency_digest': vps.dependency_digest(directory)}, directory, server, arch)[1]


@server.tool(structured_output=True)
def vps_project_prepare(source: str, project_id: str, disk_gib: float, cpus: int = 1,
                        ram_gib: float | None = None, timeout: int = 1800,
                        server: str | None = None, arch: str = 'any') -> dict[str, Any]:
    """Store one immutable, versioned project source on a developer's VPS.

    Sources use the normal secret exclusions and full resource reservation.
    Existing versions and results are never overwritten or removed. A changed
    source during transfer fails without accepting a partial version. Preserve
    the preparation receipt before releasing its ordinary job.
    """
    return vps.prepare_project(source, project_id, disk_gib, cpus, ram_gib, timeout, server, arch)[1]


@server.tool(structured_output=True)
def vps_project_run(project_ref: str, argv: list[str], agent: str, disk_gib: float,
                    cpus: int = 1, ram_gib: float | None = None, timeout: int = 1800,
                    source: str | None = None, server: str | None = None) -> dict[str, Any]:
    """Run on the VPS from a stored source revision without uploading it again.

    A fresh, verified copy gives each task isolation. Pass the local source when
    available to reject a stale revision; prepare a new version after edits.
    Results stay in this task until fetched, verified and explicitly released.
    """
    return vps.run_project(project_ref, argv, agent, disk_gib, cpus, ram_gib, timeout, source, server)[1]


@server.tool(structured_output=True)
def vps_task(source: str, recipe: str, agent: str, disk_gib: float,
             cpus: int = 1, ram_gib: float | None = None, timeout: int = 900,
             server: str | None = None, arch: str = 'any', plan_only: bool = False) -> dict[str, Any]:
    """Run a project recipe on an admissible VPS, automatically reusing versioned sources.

    Prefer this for recurring tests/builds/lint/E2E. Discover standard locked
    Node/Python recipes or read .atelier-vps.json; plan_only inspects without
    running a task. Explicit local requirements return local_required without
    executing on the Mac. There is no silent local fallback or executed retry.
    Reserve the full disk/RAM peak, including a first snapshot and its task copy.
    Setup uses private dependency caches; recipe services must become ready and
    are stopped with this recipe only. Fetch and preserve suggested result paths
    before releasing the returned job_ref, including a failed task's evidence.
    """
    import project_tasks
    return project_tasks.execute(source, recipe, agent, disk_gib, cpus, ram_gib,
                                 timeout, server, arch, plan_only)[1]


@server.tool(structured_output=True)
def vps_wait(job_id: str, timeout: int = 3600, server: str | None = None) -> dict[str, Any]:
    """Wait on the VPS for an existing job's final state, without repeated model polling.

    Use the connection:job reference returned by vps_run or vps_status. This
    call returns when the job ends or the bounded timeout expires; a timeout or
    unknown state is not a success. It does not fetch or release results. The
    MCP host must keep the request alive for up to timeout + 30 seconds.
    """
    return vps.wait_for_job(job_id, timeout, server)


@server.tool(structured_output=True)
def vps_fetch(job_id: str, paths: list[str], project: str, server: str | None = None) -> dict[str, Any]:
    """Fetch selected results with SHA256 verification and an automatic local disk budget.

    Preserve useful files outside task-tmp before cleanup. Never overwrite an
    existing task workspace; inspect and finish that prior pass first.
    """
    profile, ident = vps.job_target(job_id, server)
    directory = Path(project).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError('project directory must exist')
    budget = vps.fetch_plan(profile['ssh_host'], ident, paths)
    temp_gib = max(0.025, math.ceil(budget['disk_bytes'] / 1024 ** 2) / 1024)
    wait = min(3700, max(120, math.ceil(budget['archive_bytes'] / 1024 ** 2) + 120))
    helper = Path.home() / '.local/bin/workspace-budget.py'
    args = [sys.executable, str(ROOT / 'vps.py'), '--server', profile['id'], 'fetch', profile['id']+':'+ident, *paths]
    env = os.environ.copy()
    if helper.is_file():
        command = [sys.executable, str(helper), '--temp-gib', str(temp_gib), 'run', str(directory), '--', *args]
        if shutil.which('rtk'):
            command = ['rtk', 'proxy', *command]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=wait)
        log = directory / 'output/task-tmp/stdout.log'
        if completed.returncode:
            raise RuntimeError('bounded fetch failed; inspect the preserved task workspace')
        lines = log.read_text().splitlines()
        return next(json.loads(line) for line in reversed(lines) if line.startswith('{"verified_files"'))
    target = directory / 'output/task-tmp'
    free = shutil.disk_usage(directory)
    if free.free < max(free.total // 10, vps.GIB) + math.ceil(temp_gib * vps.GIB):
        raise RuntimeError('insufficient local disk reserve for a verified transfer')
    target.mkdir(parents=True, exist_ok=False)
    env['TASK_WORKSPACE'] = str(target)
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=wait)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return json.loads(result.stdout)


@server.tool(structured_output=True)
def vps_release(job_id: str, results_preserved: bool, server: str | None = None) -> dict[str, Any]:
    """Delete only this completed remote job after its useful results are preserved."""
    if not results_preserved:
        raise ValueError('fetch, verify and preserve useful results before release')
    profile, ident = vps.job_target(job_id, server)
    return response('release', {'id': ident}, host=profile['ssh_host'])


def settings_response(action, server=None, **request):
    return vps.admin_request(action,server,**request)


@server.tool(structured_output=True)
def admin_invitation_create(member: str, expected_revision: str, server: str | None = None) -> dict[str, Any]:
    """Administrator only: create a 30-minute, one-device link for an applied member.

    The secret link is saved in a private local 0600 file; only its path and
    expiration are returned. Do not read or paste that link into the chat.
    The joining device generates its key locally. Existing access and tasks remain intact.
    """
    import onboarding
    return onboarding.deliver_invitation(member,server,expected_revision)


@server.tool(structured_output=True)
def admin_settings_get(server: str | None = None) -> dict[str, Any]:
    """Administrator only: inspect modes, per-developer quotas and settings revision.

    Requires a separately authenticated, restricted administrator SSH alias.
    Ordinary development access cannot call the root settings helper.
    """
    return settings_response('get',server)


@server.tool(structured_output=True)
def admin_settings_preview(policy: dict[str, Any], server: str | None = None) -> dict[str, Any]:
    """Administrator only: preview developer percentages as CPU/RAM/disk limits.

    No mutation. Refuses overcommit, quotas below preserved data and unavailable
    host RAM. Developer IDs bind to separate SSH users and private workspaces.
    """
    return settings_response('preview',server,policy=policy)


@server.tool(structured_output=True)
def admin_settings_apply(policy: dict[str, Any], expected_revision: str, server: str | None = None) -> dict[str, Any]:
    """Administrator only: apply a previewed policy on its current revision.

    Drain affected work first; active jobs block updates. Only changed workers
    are deployed. Data is never purged. Interrupted updates remain pending and
    must be resumed with the same candidate; unaffected developers continue.
    """
    return settings_response('apply',server,policy=policy, expected_revision=expected_revision)


if __name__ == '__main__':
    server.run(transport='stdio')
