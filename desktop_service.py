"""One private graphical session per worker, using the normal resource pool."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import select
import secrets
import signal
import socket
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import accounts
import desktop_runtime
import resources
import worker

ROOT = worker.ROOT
DIRECTORY = ROOT / '.desktops'
STATE = DIRECTORY / 'session.json'
PROVIDERS = {'chatgpt', 'claude'}


class DesktopClosed(Exception):
    pass


def private_directory():
    if DIRECTORY.is_symlink():
        raise ValueError('unsafe desktop directory')
    DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    DIRECTORY.chmod(0o700)
    return DIRECTORY


def record(path=None):
    path = path or STATE
    if path.is_symlink():
        raise ValueError('invalid private desktop state')
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 4096:
        raise ValueError('invalid private desktop state')
    value = json.loads(path.read_text())
    if not re.fullmatch('[a-f0-9]{32}', value.get('id', '')):
        raise ValueError('invalid desktop identity')
    return value


def active(value, root=None):
    if not value:
        return False
    root = root or ROOT
    with resources.resource_lock(root):
        return any(r['id'] == value['id'] and not r.get('orphaned') for r in resources.leases(root))


def status(root=None):
    value = record(root / '.desktops/session.json' if root else None)
    if not value:
        return {'available': True, 'active': False}
    live = active(value, root)
    return {'available': True, 'active': live, 'id': value['id'],
            'provider': value.get('provider'),
            'phase': value.get('phase', 'preparing') if live else 'closed',
            'started': value.get('started'), 'expires': value.get('expires'),
            'ram_bytes': value.get('ram_bytes'), 'disk_bytes': value.get('disk_bytes'),
            'error': value.get('error')}


def start(provider='chatgpt', ram_bytes=None):
    if provider not in PROVIDERS:
        raise ValueError('select ChatGPT/Codex or Claude Desktop')
    if ram_bytes is not None and (type(ram_bytes) is not int or not 0 < ram_bytes <= 5 * worker.GIB):
        raise ValueError('declare a positive known RAM peak within the worker limit')
    private_directory()
    with (DIRECTORY / '.control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = record()
        if active(previous):
            if previous.get('provider') != provider:
                raise RuntimeError('finish and close the current program before switching providers')
            if ram_bytes is not None and previous.get('ram_bytes') != ram_bytes:
                raise RuntimeError('finish the current session before changing its RAM reservation')
            return status()
        state = worker.status()
        if state['maintenance'] or not state['admit']:
            raise RuntimeError('the shared worker is not admitting a new desktop')
        ident = uuid.uuid4().hex
        # A new graphical workload needs one measured first pass, then the usual margin.
        request = {'argv': ['python3', '/app/desktop_service.py', 'run', provider],
                   'agent': 'Atelier · ' + ('ChatGPT' if provider == 'chatgpt' else 'Claude'), 'project': 'atelier-desktop',
                   'timeout': 3600, 'cpus': 1, 'disk_bytes': 6 * worker.GIB, 'ram_bytes': None,
                   'dependency_digest': hashlib.sha256(desktop_runtime.MANIFEST.read_bytes()).hexdigest()}
        runtime, _ = desktop_runtime.location(ROOT)
        if (runtime / 'installed.json').is_file():
            request['disk_bytes'] = 2 * worker.GIB
        ram, source = resources.ram_request(ROOT, request)
        if source == 'unprofiled':
            ram = int(2.5 * worker.GIB)
            request['ram_bytes'] = ram
        if ram_bytes is not None:
            ram = ram_bytes; request['ram_bytes'] = ram_bytes
        admission = resources.admission(state['pool'], request['cpus'], ram,
                                        request['disk_bytes'], state['maintenance'])
        if not admission['allowed']:
            raise resources.AdmissionError(admission)
        value = {'id': ident, 'provider': provider, 'phase': 'preparing', 'started': time.time(),
                 'expires': time.time() + 3600, 'ram_bytes': ram,
                 'disk_bytes': request['disk_bytes'], 'request': request}
        resources.write_json(STATE, value)
        proc = subprocess.Popen(['python3', '/app/desktop_service.py', 'supervise', ident],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if active(value):
                return status()
            if proc.poll() is not None:
                raise RuntimeError('desktop reservation failed; inspect its private receipt')
            time.sleep(.1)
        proc.terminate(); proc.wait(timeout=5)
        raise RuntimeError('desktop reservation did not complete within its admission window')


def supervise(ident):
    from quota_protection import protect_quota
    protect_quota(); worker.validate_environment()
    value = record()
    if not value or value['id'] != ident:
        raise ValueError('desktop launch identity changed')
    empty = io.BytesIO()
    with tarfile.open(fileobj=empty, mode='w'):
        pass
    empty.seek(0)
    home = private_directory() / 'home'
    if home.is_symlink():
        raise ValueError('desktop profile must not be a symbolic link')
    home.mkdir(mode=0o700, exist_ok=True)
    try:
        result = worker.run_job(value['request'], empty, ident=ident, persistent=home,
                                profile_ready=lambda: (record() or {}).get('phase') == 'ready')
        current = record()
        if current and current['id'] == ident:
            for key in ['password', 'port']:
                current.pop(key, None)
            current.update(phase='closed' if result['status'] == 'passed' else 'failed', finished=time.time(),
                           error=None if result['status'] == 'passed' else 'Open this session in failure history for its private explanation.')
            resources.write_json(STATE, current)
    except Exception:
        value.update(phase='failed', error='Desktop admission failed; existing tasks were preserved.')
        resources.write_json(STATE, value)
        raise


def write_script(path, text):
    if path.is_symlink():
        raise ValueError('unsafe desktop launcher')
    fd, name = tempfile.mkstemp(prefix='.launcher-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
        os.chmod(name, 0o700); os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def check_programs(programs):
    failed = [name + ' (exit ' + str(process.returncode) + ')'
              for name, process in programs.items() if process.poll() is not None]
    if failed:
        raise RuntimeError('remote program exited: ' + ', '.join(failed))


def install_global_instructions(home):
    rules = home / 'AGENTS.md'; config = home / '.codex'
    if rules.is_symlink() or config.is_symlink():
        raise ValueError('unsafe desktop instructions directory')
    config.mkdir(mode=0o700, exist_ok=True)
    global_rules = config / 'AGENTS.md'
    if global_rules.is_symlink():
        raise ValueError('unsafe global instructions file')
    if not global_rules.exists():
        write_script(global_rules, 'Read ' + str(rules) + ' before working on any project. '
                     'This program already runs inside its reserved Atelier VPS session.\n')


def run(ident):
    value = record()
    if not value or value['id'] != ident or os.environ.get('USEVPS_JOB_ID') != ident:
        raise PermissionError('the desktop must run inside its own reservation')
    def closed(*_):
        raise DesktopClosed()
    signal.signal(signal.SIGTERM, closed)
    try:
        value['process'] = worker.process_token(os.getpid())
        resources.write_json(STATE, value)
        if not (worker.job_path(ident) / '.desktop-stop').exists():
            _run(ident)
    except DesktopClosed:
        return


def _run(ident):
    value = record()
    if not value or value['id'] != ident or os.environ.get('USEVPS_JOB_ID') != ident:
        raise PermissionError('the desktop must run inside its own reservation')
    provider = value.get('provider')
    if provider not in PROVIDERS:
        raise ValueError('unknown desktop program')
    runtime = desktop_runtime.prepare(ROOT)
    tools = desktop_runtime.commands(runtime)
    home = DIRECTORY / 'home'
    launchers = home / '.atelier'
    if home.is_symlink() or launchers.is_symlink():
        raise ValueError('unsafe private desktop profile')
    launchers.mkdir(mode=0o700, exist_ok=True)
    run = Path(os.environ['TMPDIR']); run.mkdir(parents=True, exist_ok=True)
    private_run = run / 'desktop'; private_run.mkdir(mode=0o700)
    env = accounts.account_environment()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_DATA_HOME=str(home / '.local/share'), XDG_CACHE_HOME=str(home / '.cache'),
               XDG_RUNTIME_DIR=str(private_run), TMPDIR=str(run),
               CODEX_HOME=str(home / '.codex'), BROWSER=str(launchers / 'browser'))
    env = desktop_runtime.environment(runtime, env)
    browser = desktop_runtime.browser(runtime)
    import shlex
    write_script(launchers / 'browser', '#!/bin/sh\nexec ' + shlex.quote(str(browser))
        + ' --no-sandbox --disable-dev-shm-usage --user-data-dir=' + shlex.quote(str(home / '.browser')) + ' "$@"\n')
    for name, schemes in [('claude', ['claude', 'claude-desktop']), ('chatgpt', ['chatgpt', 'codex'])]:
        launcher = launchers / name
        write_script(launcher, '#!/bin/sh\nexec ' + shlex.quote(str(tools[name]))
                     + ' --no-sandbox --disable-dev-shm-usage "$@"\n')
        applications_dir = home / '.local/share/applications'
        applications_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = applications_dir / ('atelier-' + name + '.desktop')
        write_script(entry, '[Desktop Entry]\nType=Application\nName=' + name + '\nExec="'
                     + str(launcher).replace('"', '\\"') + '" %u\nMimeType='
                     + ';'.join('x-scheme-handler/' + value for value in schemes) + ';\n')
        for scheme in schemes:
            subprocess.run(['xdg-mime', 'default', entry.name, 'x-scheme-handler/' + scheme],
                           env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    projects = home / 'projects'; projects.mkdir(mode=0o700, exist_ok=True)
    rules = home / 'AGENTS.md'
    if not rules.exists():
        write_script(rules, '# Atelier VPS\n\n@/app/AGENTS.md\n\n'
            'This program already runs in a reserved Linux VPS session. Work sequentially; delegate only '
            'when the human explicitly asks. Keep projects and useful results '
            'in ~/projects. Use the existing toolchain; never start another worker, container or VM '
            'to bypass admission. Preserve other projects, jobs and reservations.\n\n'
            'Read real capacity with `rtk proxy python3 /app/worker.py status`. Respect the session '
            'deadline. Size work from measured peaks; apply 25% plus 64 MiB once, never a second '
            'margin or a 2x multiplier. A heavier task needs its full admitted reservation before starting.\n\n'
            'Never open, read or parse any CLAUDE.md file. Do not read, export or version provider '
            'credentials, browser profiles or SSH private keys. Account sign-in belongs to the user '
            'inside the official program. Preserve profiles and files outside managed dependency caches.\n')
    install_global_instructions(home)
    write_script(launchers / 'terminal', '#!/bin/sh\ncd ' + shlex.quote(str(projects))
                 + '\nexec ' + shlex.quote(str(tools['terminal'])) + ' -title "Atelier VPS" -e /bin/bash\n')
    menu = home / '.config/openbox'; menu.mkdir(parents=True, exist_ok=True)
    from xml.sax.saxutils import escape
    write_script(menu / 'menu.xml', '<openbox_menu xmlns="http://openbox.org/3.4/menu"><menu id="root-menu" label="Atelier VPS">'
        + ''.join('<item label="' + title + '"><action name="Execute"><command>'
                  + escape(str(launchers / command)) + '</command></action></item>'
                  for title, command in [(('ChatGPT / Codex' if provider == 'chatgpt' else 'Claude / Cowork'), provider), ('Terminal', 'terminal')])
        + '</menu></openbox_menu>')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    # X11 uses a cookie on the private container network; the read-only root stays intact.
    display = 100 + int(ident[:4], 16) % 500
    env.update(DISPLAY='127.0.0.1:' + str(display), XAUTHORITY=str(private_run / 'Xauthority'))
    subprocess.run([str(tools['xauth']), '-f', env['XAUTHORITY'], 'source', '-'],
                   input=('add ' + env['DISPLAY'] + ' . ' + os.urandom(16).hex() + '\n').encode(),
                   env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    encoded = subprocess.run([str(tools['vncpasswd']), '-f'], input=password.encode(),
                             env=env, check=True, capture_output=True, timeout=5).stdout
    if len(encoded) != 8:
        raise ValueError('invalid private VNC password file')
    password_file = private_run / 'vnc-password'
    with password_file.open('xb') as out:
        out.write(encoded)
    password_file.chmod(0o600)
    server = subprocess.Popen([str(tools['vnc']), ':' + str(display), '-geometry', '1280x800', '-depth', '24',
        '-SecurityTypes', 'VncAuth', '-PasswordFile', str(password_file), '-localhost', '-AlwaysShared', '-rfbport', str(port),
        '-nolisten', 'unix', '-listen', 'tcp', '-auth', env['XAUTHORITY'],
        '-fp', str(runtime / 'root/usr/share/fonts/X11/misc')],
        env=env, stdout=subprocess.DEVNULL, stderr=sys.stderr)
    deadline = time.monotonic() + 15
    while True:
        if server.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('the private graphical server failed to start')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.5) as probe:
                if probe.recv(12).startswith(b'RFB '):
                    break
        except OSError:
            time.sleep(.1)
    bus = private_run / 'bus'
    config = private_run / 'dbus.conf'
    config.write_text('<busconfig><type>session</type><listen>unix:path=' + str(bus)
        + '</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/>'
        + '<allow send_destination="*"/><allow eavesdrop="true"/></policy></busconfig>')
    applications = []
    for argv in [[str(tools['dbus']), '--nofork', '--nopidfile', '--config-file=' + str(config)],
                 [str(tools['wm'])], [str(launchers / provider)]]:
        if argv[0] != str(tools['dbus']):
            env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + str(private_run / 'bus')
        applications.append(subprocess.Popen(argv, env=env, cwd=home, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(1)
    programs = dict(zip(['session bus', 'window manager', provider], applications))
    check_programs(programs)
    value.update(phase='ready', port=port, password=password)
    resources.write_json(STATE, value)
    while server.poll() is None:
        check_programs(programs)
        if (worker.job_path(ident) / '.desktop-stop').exists():
            return
        time.sleep(.2)
    raise RuntimeError('the private graphical server exited')


def bridge(ident):
    value = record()
    if not value or value['id'] != ident or value.get('phase') != 'ready' or not active(value):
        raise PermissionError('that desktop session is not active in this private worker')
    port = value.get('port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid private desktop endpoint')
    with (DIRECTORY / '.viewer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.create_connection(('127.0.0.1', port), timeout=10) as remote:
            remote.settimeout(None)
            while active(value):
                readable, _, _ = select.select([sys.stdin.buffer, remote], [], [], 5)
                for source in readable:
                    part = remote.recv(65536) if source is remote else sys.stdin.buffer.read1(65536)
                    if not part:
                        return
                    if source is remote:
                        sys.stdout.buffer.write(part); sys.stdout.buffer.flush()
                    else:
                        remote.sendall(part)


def stop_program(ident, current):
    (worker.job_path(ident) / '.desktop-stop').touch(exist_ok=True)
    token = current.get('process')
    if token is None:
        return  # A just-starting program checks the marker before installation.
    if not isinstance(token, str) or not re.fullmatch('[0-9]+:[0-9]+', token):
        raise ValueError('invalid desktop process identity')
    pid = int(token.split(':')[0])
    with resources.resource_lock(ROOT):
        lease = next((r for r in resources.leases(ROOT) if r['id'] == ident), None)
        if not lease or worker.process_token(pid) != token:
            return
        try:
            parent = int((Path('/proc') / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()[1])
        except FileNotFoundError:
            return
        if parent != int(lease['runner'].split(':')[0]):
            raise PermissionError('desktop process ownership changed; no process was signalled')
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def dispatch(request):
    action = request.get('action')
    if action == 'status':
        value = status()
    elif action == 'start':
        value = start(request.get('provider', 'chatgpt'), request.get('ram_bytes'))
    elif action in {'stream', 'stop', 'ticket'}:
        ident = request.get('id')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            raise ValueError('invalid desktop identity')
        current = record()
        if not current or current['id'] != ident or not active(current):
            raise PermissionError('desktop session is not active')
        if action == 'stream':
            return bridge(ident)
        if action == 'ticket':
            if current.get('phase') != 'ready':
                raise RuntimeError('the remote desktop is still preparing')
            value = {'id': ident, 'password': current['password']}
        else:
            stop_program(ident, current)
            value = {'stopping': ident}
    else:
        raise ValueError('unsupported desktop action')
    print(json.dumps(value))


if __name__ == '__main__':
    running = len(sys.argv) == 3 and sys.argv[1] == 'run' and sys.argv[2] in PROVIDERS
    ident = os.environ.get('USEVPS_JOB_ID', '') if running else sys.argv[2] if len(sys.argv) == 3 else ''
    supervising = len(sys.argv) == 3 and sys.argv[1] == 'supervise'
    if not (running or supervising) or not re.fullmatch('[a-f0-9]{32}', ident):
        raise SystemExit('use the restricted desktop controls')
    (run if sys.argv[1] == 'run' else supervise)(ident)
