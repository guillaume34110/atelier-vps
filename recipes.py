"""Bounded project recipes and their Linux service/test lifecycle."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None
import urllib.parse
import urllib.request
import urllib.error

CONFIG = '.atelier-vps.json'
STANDARD = re.compile(r'(?:test|build|lint|typecheck|type-check|check|e2e)(?::[a-z0-9][a-z0-9:-]{0,31})?')
LOCAL = re.compile(r'\b(?:xcodebuild|xcrun|osascript|simctl|mlx|metal|mps|docker|adb|fastboot)\b|--mac\b', re.IGNORECASE)
PORT = re.compile(r'\{port:([a-z][a-z0-9-]{0,31})\}')


def read(path, loader=json.loads):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise ValueError('project manifests must be regular, unlinked files of at most 64 KiB')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe project manifest')
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('project manifest exceeds 64 KiB')
    value = loader(raw.decode())
    if not isinstance(value, dict):
        raise ValueError('project manifest must be an object')
    return value


def command(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 128
            or any(not isinstance(x, str) or not x or len(x) > 4096 or '\0' in x for x in value)):
        raise ValueError('recipe commands must be bounded, nonempty argument arrays')
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) - {'argv', 'setup', 'services', 'artifacts', 'platform', 'arch'}:
        raise ValueError('unknown recipe fields')
    argv = command(value.get('argv'))
    setup = value.get('setup', [])
    if not isinstance(setup, list) or len(setup) > 8:
        raise ValueError('a recipe supports at most eight setup commands')
    setup = [command(x) for x in setup]
    platform, arch = value.get('platform', 'linux'), value.get('arch', 'any')
    if platform not in {'linux', 'local'} or arch not in {'any', 'aarch64', 'x86_64'}:
        raise ValueError('invalid recipe platform or architecture')
    services = value.get('services', [])
    if not isinstance(services, list) or len(services) > 4:
        raise ValueError('a recipe supports at most four services')
    if any(not isinstance(x, dict) for x in services):
        raise ValueError('invalid service')
    known_names = {x.get('name') for x in services if isinstance(x.get('name'), str)}
    names = set()
    for service in services:
        if not isinstance(service, dict) or set(service) - {'name', 'argv', 'ready_url', 'timeout'}:
            raise ValueError('unknown service fields')
        name = service.get('name', '')
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', name) or name in names:
            raise ValueError('services require distinct, short names')
        names.add(name)
        command(service.get('argv'))
        url = service.get('ready_url')
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('a service requires a loopback health URL')
        if any(name not in known_names for name in PORT.findall(url)):
            raise ValueError('unknown service port placeholder')
        if PORT.findall(url) and set(PORT.findall(url)) != {name}:
            raise ValueError('a health URL must reference its own service port')
        parsed = urllib.parse.urlsplit(PORT.sub('12345', url))
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('service health URLs must use HTTP on a literal loopback address without credentials')
        timeout = service.get('timeout', 30)
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('service readiness timeout must be 1..120 seconds')
    for arg in argv + [arg for s in services for arg in s['argv']]:
        if any(name not in known_names for name in PORT.findall(arg)):
            raise ValueError('unknown service port placeholder')
    artifacts = value.get('artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) > 16:
        raise ValueError('a recipe supports at most sixteen result paths')
    from worker import safe_path
    private = {'.home', '.tmp', '.ssh', '.config', '.codex', '.claude', '.agents', '.desktops', '.agent-auth'}
    for name in artifacts:
        if not isinstance(name, str) or len(name) > 1024:
            raise ValueError('invalid recipe result path')
        path = safe_path(name)
        if (any(p in private or p.casefold() == 'claude.md' for p in path.parts)
                or path.name.startswith(('.env', 'id_rsa', 'id_ed25519'))
                or path.name.endswith(('.key', '.pem'))):
            raise ValueError('recipe results cannot include private paths')
    return {'argv': argv, 'setup': setup, 'services': services, 'artifacts': artifacts,
            'platform': platform, 'arch': arch}


def discover(source):
    config = read(source / CONFIG) or {'version': 1, 'recipes': {}}
    if (set(config) - {'version', 'id', 'recipes'} or type(config.get('version')) is not int
            or config['version'] != 1):
        raise ValueError('unsupported project recipe configuration')
    configured = config.get('recipes', {})
    if not isinstance(configured, dict) or len(configured) > 32:
        raise ValueError('configure at most thirty-two named recipes')
    found = {}
    package = read(source / 'package.json')
    if package:
        scripts = package.get('scripts', {})
        if not isinstance(scripts, dict):
            raise ValueError('package scripts must be an object')
        locks = [name for name in ['pnpm-lock.yaml', 'package-lock.json'] if (source / name).is_file()]
        if len(locks) > 1 and not configured:
            raise ValueError('choose the package manager explicitly in ' + CONFIG)
        manager = 'pnpm' if locks == ['pnpm-lock.yaml'] else 'npm'
        setup = ([['pnpm', 'install', '--frozen-lockfile', '--prefer-offline']] if manager == 'pnpm' else
                 [['npm', 'ci', '--prefer-offline', '--no-audit', '--no-fund']] if locks else [])
        if len(locks) <= 1 and (locks or not (package.get('dependencies') or package.get('devDependencies'))):
            for name in sorted(scripts):
                if not STANDARD.fullmatch(name):
                    continue
                if not isinstance(scripts[name], str):
                    raise ValueError('package scripts must be strings')
                found[name] = {'argv': [manager, 'run', name], 'setup': setup,
                               'platform': 'local' if LOCAL.search(scripts[name]) else 'linux'}
    python = read(source / 'pyproject.toml', tomllib.loads) if tomllib else None
    python_available = tomllib is not None or not (source / 'pyproject.toml').exists()
    if not package and python_available and ((source / 'tests').is_dir() and not (source / 'tests').is_symlink()):
        uv = (source / 'uv.lock').is_file()
        tests = []
        for path in sorted((source / 'tests').glob('test_*.py'))[:32]:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 65536:
                tests.append(path.read_text(errors='replace'))
        pytest = bool(python and 'pytest' in python.get('tool', {})) or any(re.search(r'\b(?:import pytest|from pytest\b)', s) for s in tests)
        unittest = any(re.search(r'\b(?:import unittest|from unittest\b)', s) for s in tests)
        argv = ['pytest'] if pytest else ['python3', '-m', 'unittest', 'discover', '-s', 'tests']
        deps = (python or {}).get('project', {}).get('dependencies', [])
        local = any(isinstance(x, str) and re.match(r'mlx(?:\b|[-_])', x) for x in deps)
        other_deps = any((source / name).is_file() for name in
                         ['requirements.txt', 'requirements-dev.txt', 'Pipfile', 'poetry.lock'])
        if (pytest or unittest) and (uv or (unittest and not pytest and not deps and not other_deps)):
            found['test'] = {'argv': ['uv', 'run', '--no-sync', *argv] if uv else argv,
                             'setup': [['uv', 'sync', '--frozen']] if uv else [],
                             'platform': 'local' if local else 'linux'}
    for name, value in configured.items():
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9:-]{0,47}', name):
            raise ValueError('invalid recipe name')
        found[name] = value
    return config.get('id'), {name: validate(value) for name, value in found.items()}


def stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def service_ports(services):
    if not services:
        return None, {}
    root = Path(os.environ['USEVPS_ROOT'])
    fd = os.open(root / '.recipe-ports.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise ValueError('unsafe recipe port lock')
        ports = {}
        for service in services:
            name = service['name']
            dynamic = bool(PORT.findall(service['ready_url']))
            parsed = urllib.parse.urlsplit(PORT.sub('12345', service['ready_url']))
            host = parsed.hostname
            for _ in range(64):
                with socket.socket(socket.AF_INET6 if host == '::1' else socket.AF_INET) as sock:
                    sock.bind((host, 0 if dynamic else parsed.port))
                    port = sock.getsockname()[1]
                    if port in ports.values():
                        if dynamic:
                            continue
                        raise ValueError('recipe services cannot share a fixed port')
                    try:
                        # One byte-range lock per port, with no growing lease files.
                        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, port)
                    except BlockingIOError:
                        if dynamic:
                            continue
                        raise RuntimeError('recipe service port is already reserved') from None
                    ports[name] = port
                    break
            else:
                raise RuntimeError('no private recipe service port is available')
        return fd, ports
    except BaseException:
        os.close(fd)
        raise


def execute(value):
    value = validate(value)
    if value['platform'] != 'linux':
        raise ValueError('this recipe requires the local machine')
    processes, task, port_lock = [], None, None
    try:
        for argv in value['setup']:
            print('Recipe dependency setup', flush=True)
            subprocess.run(argv, check=True)
        port_lock, ports = service_ports(value['services'])
        def expand(text):
            return PORT.sub(lambda m: str(ports[m[1]]) if m[1] in ports else m[0], text)
        environment = os.environ.copy()
        for service in value['services']:
            url = expand(service['ready_url'])
            parsed = urllib.parse.urlsplit(url)
            key = 'ATELIER_SERVICE_' + service['name'].upper().replace('-', '_')
            environment[key + '_URL'] = url
            environment[key + '_ORIGIN'] = parsed.scheme + '://' + parsed.netloc
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for service in value['services']:
            process = subprocess.Popen([expand(x) for x in service['argv']], env=environment, start_new_session=True)
            processes.append((service['name'], process))
            deadline = time.monotonic() + service.get('timeout', 30)
            while True:
                if any(p.poll() is not None for _, p in processes):
                    raise RuntimeError('recipe service exited before readiness')
                try:
                    with opener.open(expand(service['ready_url']), timeout=1) as response:
                        if 200 <= response.status < 300:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('recipe service readiness timeout: ' + service['name'])
                time.sleep(0.1)
            print('Recipe service ready: ' + service['name'], flush=True)
        task = subprocess.Popen([expand(x) for x in value['argv']], env=environment, start_new_session=True)
        while task.poll() is None:
            if any(p.poll() is not None for _, p in processes):
                raise RuntimeError('recipe service exited while tests were running')
            time.sleep(0.1)
        if any(p.poll() is not None for _, p in processes):
            raise RuntimeError('recipe service exited before recipe completion')
        return task.returncode
    finally:
        stop(task)
        for _, process in reversed(processes):
            stop(process)
        if port_lock is not None:
            os.close(port_lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('recipe')
    args = parser.parse_args()
    try:
        if len(args.recipe) > 49152:
            raise ValueError('recipe is too large')
        sys.exit(execute(json.loads(args.recipe)))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
