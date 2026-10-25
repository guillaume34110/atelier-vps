"""Personal endpoints live outside the public checkout; credentials stay in SSH."""
import json
import os
from pathlib import Path
import re
import fcntl
import hashlib
import tempfile

MAX_CONNECTIONS = 16


def config_path():
    return Path(os.environ.get('ATELIER_VPS_CONFIG', Path.home() / '.config/atelier-vps/config.json'))


def load():
    path = config_path()
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ValueError('client configuration exceeds 16 KiB')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError('client configuration must contain text values')
    return value


def valid_alias(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('configure a valid SSH host alias')
    return value


def host():
    return valid_alias(os.environ.get('ATELIER_VPS_HOST', load().get('ssh_host', 'dev-vps')))


def admin_host():
    alias = os.environ.get('ATELIER_VPS_ADMIN_HOST', load().get('admin_ssh_host'))
    if not alias:
        raise PermissionError('administrator SSH alias is not configured on this machine')
    return valid_alias(alias)


def validate_connection(value):
    if not isinstance(value, dict) or set(value) - {
            'id', 'name', 'team', 'ssh_host', 'admin_ssh_host', 'managed', 'enabled', 'scope', 'server_id'}:
        raise ValueError('invalid connection fields')
    if not isinstance(value.get('id'), str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', value['id']):
        raise ValueError('connection ID must be a short slug')
    for field in ['name', 'team']:
        text = value.get(field)
        if not isinstance(text, str) or not 1 <= len(text) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError('connection labels must be short single lines')
    valid_alias(value.get('ssh_host', ''))
    if value.get('admin_ssh_host') is not None:
        valid_alias(value['admin_ssh_host'])
    for field in ['enabled', 'managed']:
        if field in value and type(value[field]) is not bool:
            raise ValueError(field + ' must be boolean')
    if value.get('scope') is not None and (not isinstance(value['scope'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', value['scope'])):
        raise ValueError('invalid worker scope')
    if value.get('server_id') is not None and (not isinstance(value['server_id'], str) or not re.fullmatch('[a-f0-9]{64}', value['server_id'])):
        raise ValueError('invalid server identity')
    return dict(value, enabled=value.get('enabled', True), managed=value.get('managed', False))


def connections():
    """Registry shared by CLI, MCP and the widget; legacy SSH setup remains valid."""
    path = config_path().with_name('connections.json')
    if not path.exists():
        values = load()
        profile = {'id': 'default', 'name': values.get('server_name', 'Atelier VPS'),
                   'team': values.get('team_name', 'Personnel'), 'ssh_host': valid_alias(values.get('ssh_host', 'dev-vps'))}
        if values.get('admin_ssh_host') or os.environ.get('ATELIER_VPS_ADMIN_HOST'):
            profile['admin_ssh_host'] = admin_host()
        return [validate_connection(profile)]
    if path.is_symlink() or path.stat().st_size > 131072:
        raise ValueError('connection registry must be a bounded regular file')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {'version', 'connections'} or type(data['version']) is not int or data['version'] != 1:
        raise ValueError('invalid connection registry')
    rows = data['connections']
    if not isinstance(rows, list) or len(rows) > MAX_CONNECTIONS:
        raise ValueError('configure at most sixteen connections')
    rows = [validate_connection(row) for row in rows]
    if len({row['id'] for row in rows}) != len(rows) or len({row['ssh_host'] for row in rows}) != len(rows):
        raise ValueError('duplicate connection ID or SSH alias')
    return rows


def active_connections():
    values = [row for row in connections() if row['enabled']]
    override = os.environ.get('ATELIER_VPS_HOST')
    if override:
        matches = [row for row in connections() if override in {row['id'], row['ssh_host']}]
        if matches:
            return matches
        return [validate_connection({'id':'env-'+hashlib.sha256(override.encode()).hexdigest()[:16],
            'name':override, 'team':'Personnel', 'ssh_host':valid_alias(override)})]
    return values


def connection(identifier):
    matches = [row for row in connections() if identifier in {row['id'], row['ssh_host']}]
    if len(matches) != 1:
        raise ValueError('unknown connection; list the configured servers first')
    return matches[0]


def connection_dir(identifier):
    if not re.fullmatch('[a-z][a-z0-9-]{0,31}', identifier):
        raise ValueError('invalid connection ID')
    return config_path().parent / 'connections' / identifier


def ssh_options(alias):
    matches = [row for row in connections() if row['ssh_host'] == alias and row['managed']]
    if not matches:
        return []
    path = connection_dir(matches[0]['id']) / 'ssh.conf'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError('managed SSH configuration is missing or invalid')
    return ['-F', str(path)]


def save_connection(value):
    """Serialize concurrent registrations; disabling never removes keys or results."""
    value = validate_connection(value)
    directory = config_path().parent
    if directory.is_symlink():
        raise ValueError('connection directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / '.connections.lock'
    if lock_path.is_symlink():
        raise ValueError('connection lock must not be a symlink')
    with lock_path.open('a') as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'connections.json'
        rows = connections() if path.exists() or config_path().exists() else []
        previous = next((row for row in rows if row['id'] == value['id']), None)
        if previous and (previous['ssh_host'] != value['ssh_host']
                or any(previous.get(k) and previous[k] != value.get(k) for k in ['scope','server_id'])):
            raise ValueError('use a new connection ID when changing its server or workspace; old results must stay addressable')
        rows = [row for row in rows if row['id'] != value['id']]
        if any(row['ssh_host'] == value['ssh_host'] for row in rows) or len(rows) >= MAX_CONNECTIONS:
            raise ValueError('duplicate SSH alias or connection limit reached')
        rows.append(value)
        data = json.dumps({'version': 1, 'connections': rows}, ensure_ascii=False, indent=2) + '\n'
        fd, temp = tempfile.mkstemp(prefix='.connections-', dir=directory)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
            if path.is_symlink():
                raise ValueError('refusing to replace a symlink')
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
    return value
