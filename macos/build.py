#!/usr/bin/env python3
"""Build and verify the native Mac widget using the installed Apple toolchain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='also render two real SSH samples on this Mac')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise RuntimeError('the widget requires the native macOS SDK')
    root = Path(os.environ['TASK_WORKSPACE']).resolve()
    if not root.is_dir():
        raise RuntimeError('use workspace-budget.py run before building')
    source = Path(__file__).resolve().parent
    bundle = root / 'usevps.app'
    if bundle.exists():
        raise RuntimeError('refusing to overwrite an existing build')
    binary = bundle / 'Contents/MacOS/usevps'
    binary.parent.mkdir(parents=True)
    (bundle / 'Contents/Resources').mkdir()
    sources = [source / name for name in ['WidgetModel.swift', 'AdminSettings.swift', 'AdminView.swift', 'WidgetApp.swift']]
    translations = sorted(source.glob('*.lproj/*.strings'))
    for locale in ['en', 'fr']:
        shutil.copytree(source / (locale + '.lproj'), bundle / 'Contents/Resources' / (locale + '.lproj'))
    digest = hashlib.sha256(b''.join(p.relative_to(source).as_posix().encode() + b'\0' + p.read_bytes() for p in sources + [Path(__file__).resolve()] + translations)).hexdigest()
    info = {'CFBundleIdentifier': 'eu.progsoft.usevps.widget', 'CFBundleName': 'usevps',
            'CFBundleDisplayName': 'usevps', 'CFBundleExecutable': 'usevps',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0', 'LSUIElement': True,
            'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
            'CFBundleDevelopmentRegion': 'en', 'CFBundleLocalizations': ['en', 'fr'],
            'UseVPSSourceDigest': digest}
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', '-O', '-whole-module-optimization',
                    '-parse-as-library', '-module-cache-path', str(root / 'swift-cache'),
                    '-framework', 'AppKit', '-framework', 'SwiftUI', *map(str, sources),
                    '-o', str(binary)], check=True, timeout=240)
    subprocess.run([str(binary), '--self-test'], check=True, timeout=20)
    subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(bundle)], check=True, timeout=30)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True, timeout=30)
    if args.live:
        subprocess.run([sys.executable, str(source / 'check_live.py'), '--app', str(bundle)], check=True, timeout=65)
    result = {'status': 'passed', 'app': str(bundle), 'source_digest': digest,
              'bytes': sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()),
              'model_checks': 'passed', 'signature': 'verified local signature'}
    (root / 'mac-widget-build.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
