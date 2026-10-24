"""Render only machine-specific bind mounts; no credentials are required."""
import argparse
import json
from pathlib import Path


def bindings(config):
    root = Path(config['volume_mount'])
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('an explicit added-volume mount is required')
    workspace = Path(config.get('workspace', root / 'usevps'))
    dev = Path(config.get('toolchain_root', root / '.usevps-dev'))
    if (not workspace.is_relative_to(root) or not dev.is_relative_to(root)
            or any('..' in p.parts for p in (root, workspace, dev))):
        raise ValueError('all data must stay on the dedicated added volume')
    return [str(workspace) + ':/workspace', str(root / '.usevps-stat') + ':/volume-stat:ro',
            *[str(dev / src) + ':' + target + ':ro' for src, target in
              [('usr', '/usr'), ('usr/bin', '/bin'), ('usr/sbin', '/sbin'), ('usr/lib', '/lib'),
               ('usr/lib64', '/lib64'), ('etc', '/etc'), ('opt', '/opt')]]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    print('services:\n  worker:\n    environment:\n      USEVPS_VOLUME_ID: ' + json.dumps(config['volume_id']))
    print('    volumes:')
    for value in bindings(config):
        print('      - ' + json.dumps(value))


if __name__ == '__main__':
    main()
