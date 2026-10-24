"""Personal endpoints live outside the public checkout; credentials stay in SSH."""
import json
import os
from pathlib import Path
import re


def load():
    path = Path(os.environ.get('ATELIER_VPS_CONFIG', Path.home() / '.config/atelier-vps/config.json'))
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ValueError('client configuration exceeds 16 KiB')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError('client configuration must contain text values')
    return value


def valid_alias(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('configure a valid SSH host alias')
    return value


def host():
    return valid_alias(os.environ.get('ATELIER_VPS_HOST', load().get('ssh_host', 'dev-vps')))
