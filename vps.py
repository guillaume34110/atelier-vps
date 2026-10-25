#!/usr/bin/env python3
"""Client for the restricted usevps SSH gateway; no third-party dependencies."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
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
        ".config", ".codex", ".claude", ".agents",
        ".npmrc", ".pypirc", ".netrc", ".aws", ".kube", ".DS_Store"}
SKIP.update({'deployment.local.json', 'docker-compose.local.yaml', 'auth.json', 'credentials.json'})


def inputs(project):
    project=project.resolve()
    private=[(Path.home()/name).resolve() for name in ['.ssh','.config','.codex','.claude','.agents']]
    private.append(client_config.config_path().parent.resolve())
    if project in {Path('/'),Path.home()} or any(project.is_relative_to(path) for path in private):
        raise ValueError('choose a project directory outside personal credentials and agent state')
    allowed=os.environ.get('ATELIER_VPS_SOURCE_ROOTS')
    if allowed:
        if len(allowed)>16384:raise ValueError('source-root policy is too large')
        roots=json.loads(allowed)
        if not isinstance(roots,list) or not 1<=len(roots)<=16 or not all(isinstance(p,str) and Path(p).is_absolute() for p in roots):
            raise ValueError('configure one to sixteen explicit source roots')
        if not any(project.is_relative_to(Path(path).resolve()) for path in roots):
            raise ValueError('this project is outside the authorized source roots')
    total = 0
    for base, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and d.casefold()!='claude.md' and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            file = Path(base) / name
            if (name in SKIP or name.casefold()=='claude.md' or name == ".env" or name.startswith((".env.", 'id_ed25519', 'id_rsa'))
                    or name.startswith('ssh_host_') and name.endswith('_key')
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


def dependency_digest(project):
    digest = hashlib.sha256()
    for name in ['package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'uv.lock', 'requirements.txt',
                 'pyproject.toml', 'build.gradle', 'build.gradle.kts', 'gradle.lockfile']:
        path = project / name
        if path.is_file() and not path.is_symlink():
            digest.update(name.encode())
            with path.open('rb') as source:
                while chunk := source.read(65536):
                    digest.update(chunk)
    return digest.hexdigest()


def ram_bytes(value, state):
    if value is None or value == 'auto':
        return None if state.get('capabilities', {}).get('ram_profiles') else GIB
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('RAM must be auto or finite positive GiB')
    return math.ceil(number * GIB)


def transport(host, operation, request=None, payload=None, output=None, source=None, timeout=3700):
    client_config.valid_alias(host)
    command = ["ssh", *client_config.ssh_options(host), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", host, operation]
    if shutil.which('rtk'):
        command = ['rtk', 'proxy', *command]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output or subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        try:
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
        except BrokenPipeError:
            pass  # Read the admission error even when the source stream was refused.
        proc.stdin = None
        try:
            result, errors = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
            raise TimeoutError('SSH operation timed out; preserve any remote job before retrying') from None
        if proc.returncode and not result:
            try:
                failure = json.loads(errors)
                if isinstance(failure, dict) and failure.get('ok') is False:
                    return proc.returncode, errors
            except (ValueError, UnicodeError):
                pass
            raise RuntimeError(errors.decode(errors="replace")[-2000:])
        return proc.returncode, result


def json_request(host, operation, request=None, source=None, timeout=30):
    rc, raw = transport(host, operation, request, source=source, timeout=timeout)
    if not raw or len(raw) > 65536:
        raise ValueError('invalid bounded gateway response')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('gateway response must be an object')
    if rc:
        value['ok'] = False
    return rc, value


def validate_status(state):
    if not isinstance(state, dict) or state.get('ok') is not True:
        raise ValueError('unhealthy worker')
    pool = state.get('pool')
    keys = ['cpu_total','cpu_reserved','cpu_available','ram_total','ram_reserved','ram_available',
            'disk_reserved','disk_available','active_count','max_jobs']
    if (not isinstance(pool, dict) or not all(type(pool.get(k)) is int and 0 <= pool[k] < 2**63 for k in keys)
            or not 1 <= pool['max_jobs'] <= 4 or pool['active_count'] > pool['max_jobs']
            or pool['cpu_reserved'] + pool['cpu_available'] > pool['cpu_total']
            or pool['ram_reserved'] + pool['ram_available'] > pool['ram_total']
            or not all(type(state.get(k)) is bool for k in ['admit','maintenance','busy'])):
        raise ValueError('invalid worker resource counters')
    active=pool.get('active',[])
    if not isinstance(active,list) or len(active)>4 or any(not isinstance(a,dict) for a in active):
        raise ValueError('invalid running agent list')
    if state.get('server_id') is not None and (not isinstance(state['server_id'],str) or not re.fullmatch('[a-f0-9]{64}',state['server_id'])):
        raise ValueError('invalid authenticated server identity')
    scope=state.get('scope',{})
    if not isinstance(scope,dict) or not isinstance(scope.get('id','owner'),str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}',scope.get('id','owner')):
        raise ValueError('invalid worker identity')
    if not isinstance(state.get('telemetry',{}),dict):
        raise ValueError('invalid telemetry')
    return state


def server_states(server=None):
    """Fresh authenticated samples; a failed server never contributes stale capacity."""
    profiles = [client_config.connection(server)] if server else client_config.active_connections()
    rows = []
    seen = {}
    for profile in profiles:
        row = {**profile, 'connected': False}
        try:
            rc, state = json_request(profile['ssh_host'], 'status', timeout=15)
            if rc:
                raise ValueError('unhealthy worker')
            validate_status(state)
            if profile.get('server_id') and state.get('server_id') != profile['server_id']:
                raise ValueError('server identity changed')
            if profile.get('scope') and state.get('scope', {}).get('id') != profile['scope']:
                raise ValueError('worker identity changed')
            row.update(connected=True, state=state)
            identity = state.get('server_id')
            if identity:
                if identity in seen:
                    row['duplicate_of'] = seen[identity]
                else:
                    seen[identity] = profile['id']
            elif len(profiles) > 1:
                row['identity_required'] = True
        except (RuntimeError, ValueError, OSError, TimeoutError):
            row['error'] = 'SSH unavailable or worker identity changed'
        rows.append(row)
    return rows


def fleet_status(server=None):
    rows = server_states(server)
    eligible = [row for row in rows if row['connected'] and not row.get('duplicate_of') and not row.get('identity_required')]
    totals = {key:sum(row['state']['pool'].get(key,0) for row in eligible) for key in
              ['cpu_total','cpu_reserved','cpu_available','ram_total','ram_reserved','ram_available',
               'disk_reserved','disk_available','active_count','max_jobs']}
    summary = {'servers_connected':len(eligible), 'servers_configured':len(rows), **totals}
    # The existing one-server response remains compatible with installed clients.
    if len(rows) == 1 and rows[0]['connected']:
        value = dict(rows[0]['state'])
        value['connection'] = rows[0]['id']
    else:
        value = {'ok':bool(eligible), 'admit':any(row['state'].get('admit') for row in eligible),
                 'busy':any(row['state'].get('busy') for row in eligible),
                 'maintenance':bool(eligible) and all(row['state'].get('maintenance') for row in eligible), 'pool':totals}
    value['global'] = summary
    compact = []
    for row in rows:
        entry = {key:row[key] for key in ['id','name','team','connected','enabled','duplicate_of','identity_required','error'] if key in row}
        if row['connected']:
            state=row['state']; pool=state['pool']
            entry.update(server_id=state.get('server_id'), scope=state.get('scope'), admit=state.get('admit'), maintenance=state.get('maintenance'),
                arch=state.get('telemetry',{}).get('arch'), pool={key:pool[key] for key in totals if key in pool},
                agents=[{key:a[key] for key in ['id','agent','project','started'] if key in a} for a in pool.get('active',[])[:4]])
        compact.append(entry)
    value['connections'] = compact
    return value


def select_server(request, server=None, arch='aarch64'):
    if arch not in {'aarch64','x86_64','any'}:
        raise ValueError('architecture must be aarch64, x86_64 or any')
    candidates = []
    diagnostics = []
    for row in server_states(server):
        if not row['enabled'] or not row['connected'] or row.get('duplicate_of') or row.get('identity_required'):
            continue
        state = row['state']
        if arch != 'any' and state.get('telemetry',{}).get('arch') != arch:
            diagnostics.append({'server':row['id'],'reason':'architecture'})
            continue
        if not state.get('admit') or state.get('maintenance'):
            diagnostics.append({'server':row['id'],'reason':'not_admitting'})
            continue
        needed = dict(request)
        if needed.get('ram_bytes') is None and not state.get('capabilities',{}).get('ram_profiles'):
            needed['ram_bytes'] = GIB
        if state.get('capabilities',{}).get('planning'):
            try:
                rc, plan = json_request(row['ssh_host'], 'plan', needed, timeout=15)
                if plan.get('scope',{}).get('id','owner') != state.get('scope',{}).get('id','owner'):
                    raise ValueError('planning identity changed')
            except (RuntimeError,ValueError,OSError,TimeoutError):
                diagnostics.append({'server':row['id'],'reason':'planning_unavailable'})
                continue
            if rc or plan.get('admission',{}).get('allowed') is not True:
                diagnostics.append({'server':row['id'],'reason':'resources','admission':plan.get('admission')})
                continue
            row['plan'] = plan
        else:
            pool = state['pool']
            # A legacy worker resolves its auto profile during atomic admission.
            wanted = needed.get('ram_bytes') or 64*1024**2
            if (needed['cpus'] > pool.get('cpu_available',0) or wanted > pool.get('ram_available',0)
                    or needed['disk_bytes'] > pool.get('disk_available',0) or pool.get('active_count',0) >= pool.get('max_jobs',0)):
                diagnostics.append({'server':row['id'],'reason':'resources'})
                continue
        row['request'] = needed
        candidates.append(row)
    if not candidates:
        raise RuntimeError('No compatible server can admit the full reservation: ' + json.dumps(diagnostics, separators=(',',':')))
    selected = min(candidates, key=lambda row:(row['state']['pool'].get('active_count',0),
                    -row['state']['pool'].get('cpu_available',0), -row['state']['pool'].get('ram_available',0)))
    return selected


def run_on_server(request, source, server=None, arch='aarch64'):
    selected = select_server(request, server, arch)
    # Persist an explicit environment override before handing out a stable job reference.
    previous=next((row for row in client_config.connections() if row['id']==selected['id']),None)
    profile=dict(previous) if previous else {k:selected[k] for k in ['id','name','team','ssh_host','enabled','managed']}
    profile['scope']=selected['state'].get('scope',{}).get('id','owner')
    if selected['state'].get('server_id'):profile['server_id']=selected['state']['server_id']
    if not client_config.config_path().with_name('connections.json').exists() or profile!=previous:
        client_config.save_connection(profile)
    rc, result = json_request(selected['ssh_host'], 'run', selected['request'], source=source, timeout=3700)
    result['connection'] = selected['id']
    if result.get('id'):
        safe_path(result['id'])
        if not re.fullmatch('[a-f0-9]{32}',result['id']):raise ValueError('invalid job receipt ID')
        result['job_ref'] = selected['id'] + ':' + result['id']
    return rc, result


def job_target(reference, server=None):
    if ':' in reference:
        identifier, ident = reference.split(':',1)
        profile = client_config.connection(identifier)
        if server and client_config.connection(server)['id'] != profile['id']:
            raise ValueError('job belongs to another server')
    else:
        ident = reference
        values = client_config.connections()
        if server:
            profile = client_config.connection(server)
        elif len(values) == 1:
            profile = values[0]
        else:
            raise ValueError('use the qualified job_ref or select its server explicitly')
    safe_path(ident)
    if not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('invalid job ID')
    return profile, ident


def admin_request(action, server=None, **request):
    profiles=client_config.active_connections()
    if not server and len(profiles)!=1:
        raise ValueError('select the server to administer')
    profile=client_config.connection(server) if server else profiles[0]
    alias=profile.get('admin_ssh_host')
    if not alias:
        raise PermissionError('administrator SSH alias is not configured for this server')
    identity=profile.get('server_id')
    if not identity:
        try:
            state=json_request(profile['ssh_host'],'status',timeout=15)[1]
            identity=state.get('server_id')
        except (RuntimeError,ValueError,OSError,TimeoutError):
            if server or len(profiles)>1:
                raise PermissionError('server identity is required before administration') from None
    if server and not identity:
        raise PermissionError('update the server gateway to verify administrator targeting')
    expected={'expected_server_id':identity} if identity else {}
    rc, current=json_request(alias,'settings',{'action':'get',**expected})
    if rc or identity and current.get('server_id')!=identity:
        raise PermissionError('administrator authentication or server identity mismatch')
    if action=='get':return current
    return json_request(alias,'settings',{'action':action,**expected,**request},timeout=120)[1]


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
    parser.add_argument("--host", help='existing restricted SSH alias; legacy explicit target')
    parser.add_argument('--server', help='registered connection ID')
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    servers = commands.add_parser('servers', help='list or register a private server connection')
    servers.add_argument('operation', nargs='?', choices=['list','add','enable','disable'], default='list')
    servers.add_argument('--id')
    servers.add_argument('--name')
    servers.add_argument('--team')
    servers.add_argument('--ssh-host')
    servers.add_argument('--admin-host')
    connect = commands.add_parser('connect', help='accept a private invitation from standard input')
    connect.add_argument('--stdin', action='store_true', required=True)
    request_access = commands.add_parser('access-request', help='prepare a public device request for an existing operator')
    request_access.add_argument('--name', required=True)
    settings = commands.add_parser('settings', help='restricted administrator settings; separate SSH identity required')
    settings.add_argument('operation', choices=['get', 'preview', 'apply'])
    settings.add_argument('--policy', type=Path)
    settings.add_argument('--expected-revision')
    run = commands.add_parser("run")
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--disk-gib", type=float, default=1)
    run.add_argument("--agent", default=os.environ.get("USEVPS_AGENT"), help="Public agent/task label shown in the Mac widget")
    run.add_argument("--cpus", type=int, default=1, help="reserved cores; unused cores are lent to active jobs")
    run.add_argument("--ram-gib", default='auto', help="auto learns measured peaks with headroom; or explicit GiB")
    run.add_argument('--arch', choices=['aarch64','x86_64','any'], default='aarch64')
    run.add_argument("argv", nargs=argparse.REMAINDER)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("id")
    fetch.add_argument("paths", nargs="+", help="e.g. work/result.txt stdout.log receipt.json")
    release = commands.add_parser("release")
    release.add_argument("id")
    release.add_argument("--confirmed", action="store_true", required=True)
    args = parser.parse_args()
    if args.host and args.server:
        parser.error('choose --host or --server')
    if args.action in {'servers','connect','access-request'}:
        import onboarding
        if args.action == 'connect':
            link = sys.stdin.buffer.readline(8194)
            if len(link)>8193:
                raise ValueError('invitation exceeds 8 KiB')
            result = onboarding.accept(link.decode().strip())
        elif args.action == 'access-request':
            result = onboarding.access_request(args.name)
        elif args.operation == 'list':
            result = {'connections':client_config.connections()}
        elif args.operation == 'add':
            if not all([args.id,args.name,args.team,args.ssh_host]):
                raise ValueError('server ID, name, team and SSH alias are required')
            result = onboarding.add_alias(args.id,args.name,args.team,args.ssh_host,args.admin_host)
        else:
            result=client_config.connection(args.id or '')
            result['enabled']=args.operation=='enable'
            client_config.save_connection(result)
        print(json.dumps(result,ensure_ascii=False));return 0
    if args.action == 'settings':
        request = {'action': args.operation}
        if args.operation != 'get':
            if not args.policy or args.policy.stat().st_size > 65536 or args.policy.name == 'CLAUDE.md':
                raise ValueError('a bounded JSON policy file is required')
            request['policy'] = json.loads(args.policy.read_text())
        if args.operation == 'apply':
            if not args.expected_revision:
                raise ValueError('preview and pass --expected-revision before applying')
            request['expected_revision'] = args.expected_revision
        # CLI and MCP share the authenticated server binding and revision checks.
        operation=request.pop('action')
        result=admin_request(operation,args.server,**request)
        print(json.dumps(result));return 1 if result.get('ok') is False else 0
    if args.action == "status":
        if args.host:
            rc, data = transport(args.host,'status',timeout=15)
            print(data.decode().strip());return rc
        result=fleet_status(args.server)
        print(json.dumps(result));return 0 if result['ok'] else 1
    if args.action == "release":
        profile, ident=job_target(args.id,args.server) if not args.host else ({'ssh_host':args.host},args.id)
        rc, data = transport(profile['ssh_host'], "release", {"id": ident},timeout=30)
        print(data.decode().strip())
        return rc
    if args.action == 'run':
        argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
        if not argv or not args.source.is_dir():
            raise ValueError('source directory and command are required')
        # Validate the complete manifest before SSH; stream it without a local copy.
        list(inputs(args.source.resolve()))
        request = {'argv': argv, 'timeout': args.timeout,
                             'agent': public_label(args.agent, 'Agent non identifié'),
                             'project': public_label(args.source.resolve().name, 'Projet non identifié'),
                             'cpus': args.cpus, 'ram_bytes': ram_bytes(args.ram_gib, {'capabilities':{'ram_profiles':True}}),
                             'dependency_digest': dependency_digest(args.source.resolve()),
                             'disk_bytes': int(args.disk_gib * GIB)}
        if args.host:
            state=json_request(args.host,'status',timeout=15)[1]
            request['ram_bytes']=ram_bytes(args.ram_gib,state)
            rc,data=transport(args.host,'run',request,source=args.source.resolve())
            print(data.decode().strip())
        else:
            rc,result=run_on_server(request,args.source.resolve(),args.server,args.arch)
            print(json.dumps(result))
        return rc
    root = temp_root()
    profile, ident=job_target(args.id,args.server) if not args.host else ({'id':'legacy','ssh_host':args.host},args.id)
    with tempfile.TemporaryDirectory(prefix="vps-transfer-", dir=root) as staging:
        staging = Path(staging)
        archive = staging / "result.tar"
        with archive.open("wb") as out:
            rc, _ = transport(profile['ssh_host'], "fetch", {"id": ident, "paths": args.paths}, output=out,timeout=120)
        if rc:
            raise RuntimeError("fetch failed; task remains on server")
        destination = root / ("vps-result-" + profile['id'] + '-' + ident)
        count = unpack_result(archive, destination)
        print(json.dumps({"verified_files": count, "directory": str(destination), "released": False}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
