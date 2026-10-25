"""Optional, signed Ubuntu packages and official applications in a managed cache."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from urllib.request import urlopen
from urllib.request import Request
from urllib.parse import urlsplit
import uuid

import dependency_cache
import resources
from telemetry import process_token

GIB = 1024 ** 3
MANIFEST = Path(__file__).with_name('ops') / 'agent-toolchain.json'
if not MANIFEST.is_file():
    MANIFEST = Path(__file__).with_name('agent-toolchain.json')


def specification():
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if platform.system() != 'Linux' or arch is None:
        raise ValueError('the remote desktop requires Linux ARM64 or x64')
    value = json.loads(MANIFEST.read_text())
    return {'arch': arch, **value['desktop'], 'apps': {
        name: value[name + '_desktop']['platforms'][arch] for name in ['claude', 'chatgpt']}}


def request(spec):
    return {'project': 'Atelier remote desktop',
            'dependency_digest': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()}


def location(root, touch=False):
    spec = specification()
    if touch:
        with resources.resource_lock(root):
            scope = dependency_cache.scope(root, request(spec))
    else:
        scope = dependency_cache.scope_path(root, request(spec))
    return scope / 'desktop', spec


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(path, spec):
    prefixes = ['https://downloads.claude.ai/claude-desktop/apt/stable/pool/',
                'https://persistent.oaistatic.com/codex-app-prod/linux/deb/pool/']
    if not any(spec['url'].startswith(prefix) for prefix in prefixes):
        raise ValueError('untrusted desktop download')
    if path.exists():
        if path.is_symlink() or path.stat().st_size != spec['bytes'] or digest_file(path) != spec['sha256']:
            raise ValueError('unrecognized download preserved; inspect before retrying')
        return
    partial = path.with_name('.download-' + uuid.uuid4().hex)
    digest = hashlib.sha256(); count = 0
    try:
        with urlopen(Request(spec['url'], headers={'User-Agent': 'Mozilla/5.0'}), timeout=60) as response, partial.open('xb') as out:
            if (urlsplit(response.geturl()).scheme != 'https'
                    or urlsplit(response.geturl()).hostname != urlsplit(spec['url']).hostname):
                raise ValueError('untrusted desktop redirect')
            while part := response.read(65536):
                count += len(part)
                if count > spec['bytes']:
                    raise ValueError('desktop download exceeds its pinned size')
                digest.update(part); out.write(part)
        if count != spec['bytes'] or digest.hexdigest() != spec['sha256']:
            raise ValueError('desktop download does not match its pinned checksum')
        partial.rename(path)
    finally:
        partial.unlink(missing_ok=True)


def commands(path):
    root = path / 'root'
    return {'vnc': root / 'usr/bin/Xtigervnc', 'terminal': root / 'usr/bin/xterm',
            'wm': root / 'usr/bin/openbox', 'xauth': root / 'usr/bin/xauth',
            'vncpasswd': root / 'usr/bin/tigervncpasswd',
            'dbus': root / 'usr/bin/dbus-daemon',
            'chatgpt': root / 'usr/lib/chatgpt/ChatGPT',
            'claude': root / 'usr/lib/claude-desktop/claude-desktop'}


def package_archives(apt, applications):
    files = sorted((apt / 'archives').glob('*.deb')) + applications
    if len(files) > 512 or sum(p.stat().st_size for p in files) > GIB:
        raise ValueError('desktop dependency download limit')
    return files


def environment(path, base):
    env = dict(base); root = path / 'root'
    arch = 'aarch64-linux-gnu' if specification()['arch'] == 'arm64' else 'x86_64-linux-gnu'
    libs = [root / 'usr/lib' / arch, root / 'lib' / arch, root / 'usr/lib']
    env.update(LD_LIBRARY_PATH=':'.join(map(str, libs)),
               PATH=str(root / 'usr/bin') + ':' + env.get('PATH', '/usr/bin:/bin'),
               FONTCONFIG_PATH=str(root / 'etc/fonts'),
               XDG_CONFIG_DIRS=str(root / 'etc/xdg') + ':/etc/xdg',
               XDG_DATA_DIRS=str(root / 'usr/share') + ':/usr/share')
    return env


def browser(path):
    marker = path / 'installed.json'
    if path.is_symlink() or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 262144:
        raise ValueError('invalid managed desktop manifest')
    value = json.loads(marker.read_text())['browser']
    if not isinstance(value, str) or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('invalid managed browser path')
    result = path / value
    if result.is_symlink() or not result.is_file() or not result.resolve().is_relative_to(path.resolve()):
        raise ValueError('managed graphical browser is missing')
    return result


def cleanup_stages(root, scope, spec):
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    with resources.resource_lock(root):
        active = {value['id'] for value in resources.leases(root)}
    for stage in scope.glob('.desktop-incoming-*'):
        if (stage.is_symlink() or not stage.is_dir() or stage.stat().st_uid != os.getuid()
                or not re.fullmatch(r'\.desktop-incoming-[a-f0-9]{32}', stage.name)):
            continue
        marker = stage / '.atelier-stage.json'
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 4096:
            continue
        try:
            value = json.loads(marker.read_text())
            if (value.get('spec') != fingerprint or not re.fullmatch('[a-f0-9]{32}', value.get('job', ''))
                    or not re.fullmatch('[0-9]+:[0-9]+', value.get('runner', ''))):
                continue
            if value['job'] in active or process_token(int(value['runner'].split(':')[0])) == value['runner']:
                continue
        except (ValueError, AttributeError, TypeError):
            continue
        # This marker is written before extracting software, never around user profiles.
        shutil.rmtree(stage)


def prepare(root):
    """Called only inside a guarded, reserved worker calculation."""
    if os.getuid() != 10001 or not os.environ.get('USEVPS_JOB_ID'):
        raise PermissionError('desktop installation requires a reserved worker job')
    target, spec = location(root, touch=True)
    with (target.parent / '.desktop-install.lock').open('a') as lock:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('another desktop installation is in progress')
                time.sleep(.2)
        marker = target / 'installed.json'
        if target.is_symlink() or marker.is_symlink():
            raise ValueError('unsafe desktop cache')
        if marker.is_file():
            if marker.stat().st_size > 262144 or json.loads(marker.read_text())['spec'] != spec:
                raise ValueError('desktop cache identity mismatch')
            return target
        cleanup_stages(root, target.parent, spec)
        subprocess.run(['rtk', 'proxy', 'python3', '/app/storage-guard.py', '--volume', '/workspace', '--require-gib', '5'], check=True)
        stage = target.parent / ('.desktop-incoming-' + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        resources.write_json(stage / '.atelier-stage.json', {
            'spec': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            'job': os.environ['USEVPS_JOB_ID'], 'runner': process_token(os.getpid())})
        try:
            apt = target.parent / 'desktop-downloads'
            if apt.exists():
                if apt.is_symlink() or json.loads((apt / 'spec.json').read_text()) != spec:
                    raise ValueError('unrecognized download cache preserved')
            else:
                apt.mkdir(mode=0o700); (apt / 'spec.json').write_text(json.dumps(spec))
            for name in ['lists/partial', 'archives/partial', 'log']:
                (apt / name).mkdir(parents=True, exist_ok=True)
            (apt / 'status').touch()
            mirror = 'https://ports.ubuntu.com/ubuntu-ports' if spec['arch'] == 'arm64' else 'https://archive.ubuntu.com/ubuntu'
            suites = 'noble noble-updates noble-security'
            (apt / 'sources.sources').write_text('Types: deb\nURIs: ' + mirror + '\nSuites: ' + suites
                + '\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
            # No host apt state, package installation, service scripts or root access.
            options = ['-o', 'Dir::Etc::sourcelist=' + str(apt / 'sources.sources'),
                       '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::Etc::main=-', '-o', 'Dir::Etc::parts=-',
                       '-o', 'Dir::State::status=' + str(apt / 'status'),
                       '-o', 'Dir::State::lists=' + str(apt / 'lists'),
                       '-o', 'Dir::Cache::archives=' + str(apt / 'archives'),
                       '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache=',
                       '-o', 'Dir::Log=' + str(apt / 'log'), '-o', 'Debug::NoLocking=1',
                       '-o', 'Acquire::Languages=none', '-o', 'Acquire::Retries=1',
                       '-o', 'APT::Get::AllowUnauthenticated=false']
            env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
            env.pop('APT_CONFIG', None)
            subprocess.run(['apt-get', *options, 'update'], env=env, check=True, timeout=180)
            applications = []
            for name, package_spec in spec['apps'].items():
                package = apt / (name + '-desktop.deb'); download(package, package_spec)
                applications.append(package)
            subprocess.run(['apt-get', *options, '--download-only', '--no-install-recommends', '-y',
                            'install', *spec['packages'], *map(str, applications)], env=env, check=True, timeout=360)
            dest = stage / 'root'; dest.mkdir()
            packages = package_archives(apt, applications)
            inventory = []
            for deb in packages:
                subprocess.run(['dpkg-deb', '--extract', str(deb), str(dest)], check=True, timeout=60)
                inventory.append({'file': deb.name, 'sha256': digest_file(deb)})
            for base, _, files in os.walk(dest):
                for name in files:
                    file = Path(base) / name
                    if not file.is_symlink():
                        file.chmod(file.stat().st_mode & 0o777)
            for binary in commands(stage).values():
                if not binary.is_file() or not os.access(binary, os.X_OK):
                    raise ValueError('required remote desktop executable is missing: ' + binary.name)
            runtime_env = environment(stage, env)
            for tool, directory in [('glib-compile-schemas', dest / 'usr/share/glib-2.0/schemas'),
                                    ('mkfontdir', dest / 'usr/share/fonts/X11/misc')]:
                if directory.is_dir():
                    subprocess.run([str(dest / 'usr/bin' / tool), str(directory)],
                                   env=runtime_env, check=True, timeout=30)
            bundled = []
            for file in (dest / 'usr/lib/chatgpt/resources').rglob('chrome'):
                if file.is_file() and not file.is_symlink() and os.access(file, os.X_OK):
                    with file.open('rb') as source:
                        header = source.read(20)
                        machine = b'\xb7\0' if spec['arch'] == 'arm64' else b'>\0'
                        if header[:6] == b'\x7fELF\x02\x01' and header[18:20] == machine:
                            bundled.append(file)
            browsers = stage / 'browsers'
            if len(bundled) == 1:
                graphical = bundled
            else:
                env.update(PLAYWRIGHT_BROWSERS_PATH=str(browsers), PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='0')
                subprocess.run(['node', '/usr/local/lib/node_modules/playwright-core/cli.js', 'install',
                                'chromium', '--no-shell'], env=env, check=True, timeout=360)
                graphical = list(browsers.glob('chromium-*/chrome-linux*/chrome'))
            if len(graphical) != 1:
                raise ValueError('one graphical Chromium installation was expected')
            shutil.rmtree(apt)
            (stage / 'installed.json').write_text(json.dumps({'spec': spec, 'packages': inventory,
                'browser': graphical[0].relative_to(stage).as_posix()}))
            if target.exists():
                raise ValueError('an existing desktop cache must not be overwritten')
            stage.rename(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return target
