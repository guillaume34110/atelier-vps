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
    args = parser.parse_args()
    root = Path(os.environ['TASK_WORKSPACE'])
    image = root / 'widget.png'
    binary = args.app / 'Contents/MacOS/usevps'
    subprocess.run([str(binary), '--capture', str(image), '--exercise-controls', '--exit-after-capture'], check=True, timeout=60)
    result = json.loads(image.with_suffix('.json').read_text())
    assert image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert result['source'] == 'real restricted SSH' and result['connected'] and result['samples'] >= 2
    assert result['controls_exercised'] is True
    assert result['status']['ok'] and result['window_width'] == 480 and result['window_height'] > 400
    if args.require_telemetry:
        assert result['cpu_fraction'] is not None and 0 <= result['cpu_fraction'] <= 1
        assert result['status']['telemetry']['worker_ram_limit'] == 5 * 1024**3
    print(json.dumps({'native_live_render': 'passed', 'samples': result['samples'],
                      'telemetry_verified': args.require_telemetry, 'image': str(image),
                      'size': [result['window_width'], result['window_height']]}), flush=True)


if __name__ == '__main__':
    main()
