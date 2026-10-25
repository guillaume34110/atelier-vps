#!/usr/bin/env python3
"""Render the real native app after two restricted SSH samples, then exit cleanly."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--require-telemetry', action='store_true')
    parser.add_argument('--layouts', action='store_true', help='capture narrow, standard, wide, short, mini and administrator layouts')
    parser.add_argument('--admin', action='store_true', help='verify real administrator get/preview/apply with an unchanged policy')
    args = parser.parse_args()
    root = Path(os.environ['TASK_WORKSPACE'])
    image = root / 'widget.png'
    binary = args.app / 'Contents/MacOS/usevps'
    subprocess.run([str(binary), '--capture', str(image), '--exercise-controls', '--exit-after-capture',
                    *(['--capture-layouts'] if args.layouts else []), *(['--exercise-admin'] if args.admin else [])], check=True, timeout=75)
    result = json.loads(image.with_suffix('.json').read_text())
    assert image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert result['source'] == 'real restricted SSH' and result['connected'] and result['samples'] >= 2
    assert result['controls_exercised'] is True
    assert result['automatic_breakpoints_exercised'] is True
    assert result['connected_agents'] >= len(result['status']['pool']['active'])
    assert all(':' in clock for clock in result['history_times'])
    assert result['status']['ok'] and 360 <= result['window_width'] <= 900 and result['window_height'] >= 180
    pool = result['status']['pool']
    used = pool['ram_accounting']['used_bytes']
    budget = pool['ram_reserved'] + pool['ram_available']
    expected = min(1, max(0, used / budget)) if budget else 0
    assert result['ram_fraction'] is not None and abs(result['ram_fraction'] - expected) < 1e-9
    if args.require_telemetry:
        assert result['cpu_fraction'] is not None and 0 <= result['cpu_fraction'] <= 1
        assert result['status']['telemetry']['worker_ram_limit'] == 5 * 1024**3
    if args.layouts:
        layouts = json.loads((root / 'widget-layouts.json').read_text())
        names = {item['layout'] for item in layouts}
        assert {'narrow', 'standard', 'wide', 'short', 'mini-small', 'mini', 'mini-wide'} <= names
        assert {'compact-edge', 'standard-edge', 'column-before', 'column-after', 'auto-mini', 'auto-full'} <= names
        if result['administrator_authenticated']:
            assert {'admin-narrow', 'admin', 'admin-wide'} <= names
        for item in layouts:
            assert (root / item['image']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
            assert item['settings_applied'] is False
            assert item['mini'] == (item['height'] < 220)
            expected = 'mini' if item['height'] < 220 else 'compact' if item['width'] < 440 else 'standard' if item['width'] < 700 else 'wide'
            assert item['resolved_layout'] == expected
    if args.admin:
        assert result['administrator_roundtrip_verified'] is True
    print(json.dumps({'native_live_render': 'passed', 'samples': result['samples'],
                      'telemetry_verified': args.require_telemetry, 'ram_fraction': result['ram_fraction'], 'image': str(image),
                      'size': [result['window_width'], result['window_height']]}), flush=True)


if __name__ == '__main__':
    main()
