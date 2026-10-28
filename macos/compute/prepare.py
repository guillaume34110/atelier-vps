"""Prepare a small, isolated Mac worker inside the existing workspace budget."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
import zlib

import task

ROOT = Path(__file__).resolve().parents[2]
BASE = 'https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/aarch64/netboot-3.24.3/'
HASHES = {'vmlinuz-virt': '849394914e6a93cc9bb17e901f445ce21fcd7e4528541d9496a74d6a6ccee319',
          'initramfs-virt': '7b75e0475ce73eca283ab60eac28f2fe1a8b62e8ffd870c056baaafc9d6e9b1b',
          'modloop-virt': '5252eb841fdf753d75562c4993ef55859aef828922159a64a299f6ee73f53403',
          'linux-image': '256d5fff4bb892edcb9218c1e6353e388bc3646e65f76c2bc9d958b635d1b00e'}
APP_FILES = ['client_config.py', 'mac_protocol.py', 'macos/compute/admission.py',
             'macos/compute/bridge.py', 'macos/compute/task.py',
             'macos/compute/guest-exec.py', 'macos/compute/guest-init.sh']


def download(name, destination):
    with urllib.request.urlopen(BASE + name, timeout=30) as source, destination.open('xb') as output:
        total = 0
        while chunk := source.read(65536):
            total += len(chunk)
            if total > 128 * 1024 ** 2:
                raise ValueError('download exceeds limit')
            output.write(chunk)
    if task.digest(destination) != HASHES[name]:
        raise ValueError('Alpine download digest mismatch')


def kernel_image(compressed, target):
    data = compressed.read_bytes()
    offset = data.find(b'\x1f\x8b\x08')
    if offset < 0:
        raise ValueError('Linux kernel image is not compressed as expected')
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    image = decoder.decompress(data[offset:], 128 * 1024 ** 2 + 1)
    if len(image) > 128 * 1024 ** 2 or not decoder.eof:
        raise ValueError('Linux kernel exceeds limit or is incomplete')
    with target.open('xb') as output:
        output.write(image)
    with target.open('rb') as image:
        image.seek(0x38)
        if image.read(4) != b'ARM\x64':
            raise ValueError('Linux kernel is not ARM64')
    if task.digest(target) != HASHES['linux-image']:
        raise ValueError('Linux kernel digest mismatch')


def tree_digest(root):
    digest = hashlib.sha256()
    for base, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        for name in sorted(directories + files):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if relative == 'manifest.json':
                continue
            if (name.casefold() == 'claude.md' or name in {'.ssh', '.aws', '.kube', '.env'}
                    or name.startswith(('id_rsa', 'id_ed25519')) or name.endswith(('.key', '.pem'))):
                raise ValueError('private-looking data is not allowed in the worker bundle')
            info = path.lstat()
            digest.update(relative.encode() + b'\0' + oct(stat.S_IMODE(info.st_mode)).encode() + b'\0')
            if stat.S_ISLNK(info.st_mode):
                digest.update(b'L' + os.readlink(path).encode() + b'\0')
            elif stat.S_ISDIR(info.st_mode):
                digest.update(b'D\0')
            elif stat.S_ISREG(info.st_mode):
                digest.update(b'F' + bytes.fromhex(task.digest(path)))
            else:
                raise ValueError('special files are not allowed in the worker bundle')
    return digest.hexdigest()


def plain_file(root, name):
    path = root
    if path.is_symlink():
        raise ValueError('worker path must not be a link')
    for part in Path(name).parts:
        path /= part
        if path.is_symlink():
            raise ValueError('worker path must not contain links')
    if not path.is_file():
        raise ValueError('worker file is missing')
    return path


def check_toolchain(root):
    database = plain_file(root, 'lib/apk/db/installed')
    if database.stat().st_size > 2 * 1024 ** 2:
        raise ValueError('invalid Alpine package metadata')
    entries = [dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
               for block in database.read_text().split('\n\n') if block]
    versions = {row.get('P'): row.get('V') for row in entries}
    if versions.get('python3') != '3.14.10-r1' or versions.get('nodejs') != '24.22.1-r0':
        raise ValueError('unexpected Mac worker toolchain versions')
    for name in ('usr/bin/python3.14', 'usr/bin/node'):
        plain_file(root, name)


def bootstrap(work, assets, runner, mke2fs):
    share = work / 'bootstrap-share'
    share.mkdir(mode=0o700)
    empty_tools = work / 'bootstrap-tools'
    empty_tools.mkdir()
    disk = work / 'bootstrap.img'
    with disk.open('xb') as output:
        output.truncate(64 * 1024 ** 2)
    subprocess.run([mke2fs, '-q', '-t', 'ext4', '-F', str(disk)], check=True, timeout=30)
    boot = task.initramfs(work, assets / 'initramfs-virt', Path(__file__).parent / 'bootstrap-init.sh')
    command = ['/usr/bin/nice', '-n', '10', str(runner), str(assets / 'linux-image'), str(boot),
               str(share), str(empty_tools), str(assets / 'modules'), str(disk), '1',
               str(512 * 1024 ** 2), '600', 'bootstrap']
    subprocess.run(command, stdin=subprocess.DEVNULL, check=True, timeout=615)
    if (share / 'status').read_text().strip() != '0':
        raise RuntimeError('signed Alpine toolchain installation failed; see bootstrap.log in task workspace')
    check_toolchain(share / 'toolchain')
    return share / 'toolchain'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mke2fs', required=True)
    cached = parser.add_mutually_exclusive_group()
    cached.add_argument('--seed', nargs=4, metavar=('KERNEL', 'INITRAMFS', 'MODLOOP', 'TOOLS'))
    cached.add_argument('--boot-cache', nargs=3, metavar=('KERNEL', 'INITRAMFS', 'MODLOOP'))
    args = parser.parse_args()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Mac worker preparation requires Apple Silicon macOS')
    workspace = Path(os.environ['TASK_WORKSPACE'])
    bundle = workspace / 'mac-worker-bundle'
    assets, app = bundle / 'assets', bundle / 'app'
    assets.mkdir(parents=True)
    app.mkdir()
    (assets / 'modules').mkdir()
    for name in APP_FILES:
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    runner = assets / 'vm'
    subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(workspace / 'swift-cache'),
                    str(ROOT / 'macos/compute/VM.swift'), '-o', str(runner)], check=True, timeout=180)
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements',
                    str(ROOT / 'macos/compute/virtualization.entitlements'), str(runner)], check=True, timeout=30)
    if args.seed:
        for name, source, destination in [('linux-image', args.seed[0], assets / 'linux-image'),
                                          ('initramfs-virt', args.seed[1], assets / 'initramfs-virt'),
                                          ('modloop-virt', args.seed[2], assets / 'modules/modloop-virt')]:
            candidate = Path(source)
            if candidate.is_symlink() or not candidate.is_file() or task.digest(candidate) != HASHES[name]:
                raise ValueError('seed digest mismatch')
            shutil.copy2(source, destination)
        tools = Path(args.seed[3])
        check_toolchain(tools)
    else:
        boot_files = [('vmlinuz-virt', workspace / 'vmlinuz-virt'),
                      ('initramfs-virt', assets / 'initramfs-virt'),
                      ('modloop-virt', assets / 'modules/modloop-virt')]
        for index, (name, destination) in enumerate(boot_files):
            if args.boot_cache:
                candidate = Path(args.boot_cache[index])
                if candidate.is_symlink() or not candidate.is_file() or task.digest(candidate) != HASHES[name]:
                    raise ValueError('cached boot file digest mismatch')
                shutil.copy2(candidate, destination)
            else:
                download(name, destination)
        kernel_image(workspace / 'vmlinuz-virt', assets / 'linux-image')
        tools = bootstrap(workspace, assets, runner, args.mke2fs)
    before = tree_digest(tools)
    shutil.copytree(tools, assets / 'tools', symlinks=True)
    if tree_digest(assets / 'tools') != before:
        raise ValueError('toolchain copy verification failed')
    check_toolchain(assets / 'tools')
    sources = APP_FILES + ['macos/compute/VM.swift', 'macos/compute/virtualization.entitlements']
    manifest = {'version': 1, 'sha256': tree_digest(bundle),
                'sources': {name: task.digest(ROOT / name) for name in sources},
                'toolchain': {'python': '3.14', 'node': '24', 'os': 'alpine-musl',
                              'arch': 'aarch64', 'network': False}}
    (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'prepared': str(bundle), **manifest}))


if __name__ == '__main__':
    main()
