"""Install a verified private Mac worker and start it for the current user."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys

import bridge
import prepare
import task

LABEL = 'eu.progsoft.atelier-vps.mac-worker'


def verify(bundle):
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError('prepared bundle must be a directory')
    manifest_path = bundle / 'manifest.json'
    info = manifest_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
        raise ValueError('invalid prepared bundle manifest')
    manifest = json.loads(manifest_path.read_text())
    expected = set(prepare.APP_FILES + ['macos/compute/VM.swift',
                                       'macos/compute/virtualization.entitlements'])
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or not isinstance(manifest.get('sources'), dict) or set(manifest['sources']) != expected
            or manifest.get('sha256') != prepare.tree_digest(bundle)):
        raise ValueError('prepared bundle verification failed')
    for name, digest in manifest['sources'].items():
        if task.digest(prepare.ROOT / name) != digest:
            raise ValueError('prepared bundle does not match the current sources')
        if name in prepare.APP_FILES:
            source = prepare.plain_file(bundle, 'app/' + name)
            if task.digest(source) != digest:
                raise ValueError('prepared worker code differs from the current sources')
    for name in ('assets/linux-image', 'assets/initramfs-virt', 'assets/modules/modloop-virt', 'assets/vm'):
        prepare.plain_file(bundle, name)
    for name in ('assets/modules', 'assets/tools'):
        path = bundle / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError('prepared worker is incomplete')
    prepare.check_toolchain(bundle / 'assets/tools')
    return manifest


def bundle_size(bundle):
    total = 0
    for base, dirs, files in os.walk(bundle, followlinks=False):
        for name in dirs + files:
            info = (Path(base) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += max(info.st_size, info.st_blocks * 512)
    return total


def install_bundle(bundle, project, budget):
    manifest = verify(bundle)
    bridge.private_directory(project)
    versions = project / 'versions'
    bridge.private_directory(versions)
    destination = versions / manifest['sha256'][:24]
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or verify(destination)['sha256'] != manifest['sha256']:
            raise ValueError('existing worker version differs; it was preserved')
        return destination
    required_mib = (bundle_size(bundle) + 1024 ** 2 - 1) // (1024 ** 2) + 32
    if not bridge.storage_available(project, budget, required_mib):
        raise RuntimeError('not enough disk for the verified worker bundle')
    destination.mkdir(mode=0o700)
    shutil.copytree(bundle, destination, symlinks=True, dirs_exist_ok=True)
    if verify(destination)['sha256'] != manifest['sha256']:
        raise ValueError('installed worker bundle verification failed; it was preserved')
    return destination


def agent_plist(project, version, mke2fs, scope, budget):
    if not re.fullmatch('[a-z][a-z0-9-]{0,31}', scope):
        raise ValueError('invalid developer scope')
    rtk = shutil.which('rtk')
    if not rtk or not budget.is_file() or not mke2fs.is_file():
        raise RuntimeError('rtk, workspace budget and mke2fs are required')
    assets = version / 'assets'
    command = ['/usr/bin/python3', str(version / 'app/macos/compute/bridge.py'),
               '--project', str(project), '--budget', str(budget), '--scope', scope,
               '--kernel', str(assets / 'linux-image'), '--initramfs', str(assets / 'initramfs-virt'),
               '--modules', str(assets / 'modules'), '--tools', str(assets / 'tools'),
               '--vm', str(assets / 'vm'), '--mke2fs', str(mke2fs)]
    path = ':'.join(dict.fromkeys([str(Path(rtk).resolve().parent), '/opt/homebrew/bin', '/usr/local/bin',
                                   '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
    return plistlib.dumps({'Label': LABEL, 'ProgramArguments': command, 'RunAtLoad': True,
                           'KeepAlive': True, 'ProcessType': 'Background', 'LowPriorityIO': True,
                           'Nice': 10, 'ThrottleInterval': 30,
                           'EnvironmentVariables': {'PATH': path, 'PYTHONDONTWRITEBYTECODE': '1'},
                           'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'})


def install_agent(plist, launch_dir):
    launch_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if launch_dir.is_symlink() or not launch_dir.is_dir():
        raise PermissionError('invalid LaunchAgents directory')
    path = launch_dir / (LABEL + '.plist')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(plist)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 16384 or path.read_bytes() != plist:
            raise ValueError('an existing Mac worker launch configuration was preserved')
    domain = 'gui/' + str(os.getuid())
    current = subprocess.run(['/bin/launchctl', 'print', domain + '/' + LABEL],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if current.returncode:
        subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(path)], check=True, timeout=20)
    subprocess.run(['/bin/launchctl', 'print', domain + '/' + LABEL], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=10)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--mke2fs', required=True, type=Path)
    parser.add_argument('--project', type=Path, default=Path.home() / '.atelier-vps/mac-worker')
    parser.add_argument('--budget', type=Path, default=Path.home() / '.local/bin/workspace-budget.py')
    parser.add_argument('--scope', default='owner')
    args = parser.parse_args()
    version = install_bundle(args.bundle, args.project, args.budget)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(version / 'assets/vm')],
                   check=True, timeout=30)
    plist = agent_plist(args.project, version, args.mke2fs.resolve(strict=True), args.scope, args.budget)
    path = install_agent(plist, Path.home() / 'Library/LaunchAgents')
    print(json.dumps({'installed': str(version), 'launch_agent': str(path), 'scope': args.scope}))


if __name__ == '__main__':
    main()
