"""Coolify Cloud team workers: the pinned Git owner and private image services."""
import base64
import ipaddress
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request

try:
    from .gateway import DESKTOP_TMPFS
    from .render_compose import bindings
except ImportError:
    from gateway import DESKTOP_TMPFS
    from render_compose import bindings

CONFIG = Path('/etc/atelier-vps-cloud.json')


def configuration(path=CONFIG):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > 8192:
        raise ValueError('Cloud credentials must be a private root-owned file')
    value = json.loads(path.read_text())
    if set(value) != {'token', 'base_application', 'approved_revision', 'server_uuid', 'project_uuid', 'environment_uuid', 'network_pool'}:
        raise ValueError('invalid Cloud configuration')
    if not isinstance(value['token'], str) or not 10 <= len(value['token']) <= 4096 or any(c.isspace() for c in value['token']):
        raise ValueError('invalid Cloud credential')
    if not re.fullmatch('[a-f0-9]{40}', value['approved_revision']):
        raise ValueError('an approved immutable revision is required')
    if any(not isinstance(value[k], str) or not re.fullmatch('[a-z0-9]{8,64}', value[k])
           for k in ('base_application', 'server_uuid', 'project_uuid', 'environment_uuid')):
        raise ValueError('invalid Cloud resource identity')
    pool = ipaddress.ip_network(value['network_pool'])
    ranges = [ipaddress.ip_network(v) for v in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if pool.version != 4 or pool.prefixlen != 16 or not any(pool.subnet_of(network) for network in ranges):
        raise ValueError('an unused private IPv4 /16 network pool is required')
    return value


def private_subnet(config, command):
    ids = command(['docker', 'network', 'ls', '-q']).split()
    networks = json.loads(command(['docker', 'network', 'inspect', *ids])) if ids else []
    used = [ipaddress.ip_network(item['Subnet']) for row in networks
            for item in (row.get('IPAM', {}).get('Config') or []) if item.get('Subnet')]
    routes = json.loads(command(['ip', '-j', 'route', 'show']))
    used += [ipaddress.ip_network(row['dst'], strict=False) for row in routes if row.get('dst') not in (None, 'default')]
    for candidate in ipaddress.ip_network(config['network_pool']).subnets(new_prefix=28):
        if not any(candidate.overlaps(other) for other in used if other.version == 4):
            return str(candidate)
    raise ValueError('no unused private worker subnet remains')


def environment(request):
    limits = request['limits']
    return {'USEVPS_VOLUME_ID': request['volume_id'], 'USEVPS_VOLUME_STATS': '/volume-stat',
            'USEVPS_RESERVE_GIB': '0.5', 'USEVPS_HOST_RAM_RESERVE_GIB': str(request['host_ram_reserve_bytes'] / 1024**3),
            'USEVPS_TENANT': request['tenant'], 'USEVPS_MODE': request['mode'],
            'USEVPS_MEMORY_LIMIT': str(limits['ram_bytes']), 'USEVPS_POOL_RAM_GIB': str(limits['ram_bytes'] / 1024**3),
            'USEVPS_CPU_LIMIT': str(limits['cpus']), 'USEVPS_CPU_QUOTA': str(round(limits['cpus'] * 100000)),
            'USEVPS_CPUSET': request['cpu_set'],
            'USEVPS_MAX_CPUS': str(max(1, math.ceil(limits['cpus']))),
            'USEVPS_CPU_SHARES': str(limits['cpu_shares']), 'USEVPS_MAX_JOBS': str(limits['slots'])}


def compose(request, image, network, pids_limit=512):
    if not re.fullmatch('sha256:[a-f0-9]{64}', image) or not re.fullmatch('[a-z0-9]{8,64}', network):
        raise ValueError('an immutable local image and a private worker network are required')
    if pids_limit not in (512, 768):
        raise ValueError('unsupported worker process ceiling')
    limits = request['limits']
    worker = {'image': image, 'pull_policy': 'never', 'restart': 'unless-stopped',
              'user': '10001:10001', 'read_only': True, 'cap_drop': ['ALL'],
              'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:' + DESKTOP_TMPFS],
              'mem_limit': limits['ram_bytes'], 'memswap_limit': limits['ram_bytes'],
              'cpu_period': 100000, 'cpu_quota': round(limits['cpus'] * 100000), 'cpu_shares': limits['cpu_shares'],
              'cpuset': request['cpu_set'],
              'pids_limit': pids_limit, 'oom_score_adj': 500, 'network_mode': network,
              'environment': environment(request), 'volumes': bindings({'volume_mount': request['mount'], 'workspace': request['workspace']}),
              'logging': {'driver': 'json-file', 'options': {'max-size': '2m', 'max-file': '2'}}}
    return {'services': {'worker': worker}}


class CloudTeam:
    def __init__(self, config, base, command, prepare_network):
        self.config, self.base = config, base
        self.command, self.prepare_network = command, prepare_network

    def api(self, path, body=None, method=None):
        request = urllib.request.Request('https://app.coolify.io/api/v1/' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json', 'Accept': 'application/json'},
            method=method or ('POST' if body is not None else 'GET'))
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read(2 * 1024**2 + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError('Coolify Cloud HTTP ' + str(error.code)) from None
        if len(data) > 2 * 1024**2:
            raise ValueError('Cloud response exceeds the bounded control protocol')
        return json.loads(data) if data else {}

    def owner(self, application):
        if application != self.config['base_application']:
            raise ValueError('Cloud settings belong to another application')
        owner = self.api('applications/' + application)
        if (owner.get('uuid') != application or owner.get('git_repository') != 'guillaume34110/atelier-vps'
                or owner.get('git_branch') != 'main' or owner.get('git_commit_sha') != self.config['approved_revision']
                or owner.get('settings', {}).get('is_auto_deploy_enabled') is not False
                or owner.get('settings', {}).get('is_preview_deployments_enabled') is not False
                or owner.get('settings', {}).get('is_raw_compose_deployment_enabled') is not True):
            raise ValueError('Cloud owner must retain its approved pinned source and manual deployment')
        destination = owner.get('destination') or {}
        if destination and destination.get('server', {}).get('uuid') != self.config['server_uuid']:
            raise ValueError('Cloud owner belongs to another server')
        if owner.get('server_id') is None:
            owner['server_id'] = destination.get('server_id')
        if type(owner.get('server_id')) is not int:
            raise ValueError('Cloud owner server identity unavailable')
        return owner

    def image(self):
        row = json.loads(self.command(['docker', 'inspect', self.base['container']]))[0]
        image = row['Image']
        if not re.fullmatch('sha256:[a-f0-9]{64}', image):
            raise ValueError('approved owner image unavailable')
        return image

    def marker(self, tenant):
        return 'Atelier VPS ' + self.config['base_application'] + ' / ' + tenant

    def member(self, row, tenant, owner):
        if (row.get('description') != self.marker(tenant)
                or row.get('server_id') != owner['server_id'] or row.get('environment_id') != owner['environment_id']):
            raise ValueError('refusing an unrelated Cloud service')
        uuid = row.get('uuid', '')
        if not re.fullmatch('[a-z0-9]{8,64}', uuid):
            raise ValueError('invalid Cloud service identity')
        return {'id': uuid, 'container': 'worker-' + uuid, 'network': uuid}

    def run(self, request):
        action = request['action']; tenant = request.get('tenant', 'owner')
        if not re.fullmatch('[a-z][a-z0-9-]{0,15}', tenant):
            raise ValueError('invalid developer identity')
        owner = self.owner(request['base_application'])
        if action == 'create':
            existing = [row for row in self.api('services') if row.get('description') == self.marker(tenant)]
            if len(existing) > 1:
                raise ValueError('duplicate managed Cloud services require inspection')
            if existing:
                return self.member(existing[0], tenant, owner)
            # The service is never started until its real limits, mounts and private network are configured.
            raw = {'services': {'worker': {'image': self.image(), 'pull_policy': 'never', 'command': ['false'], 'network_mode': 'none'}}}
            result = self.api('services', {'name': 'atelier-vps-' + tenant, 'description': self.marker(tenant),
                'server_uuid': self.config['server_uuid'], 'project_uuid': self.config['project_uuid'],
                'environment_uuid': self.config['environment_uuid'], 'instant_deploy': False, 'connect_to_docker_network': False,
                'docker_compose_raw': base64.b64encode(json.dumps(raw).encode()).decode()})
            if not re.fullmatch('[a-z0-9]{8,64}', result.get('uuid', '')):
                raise ValueError('invalid created Cloud service identity')
            return self.member(self.api('services/' + result['uuid']), tenant, owner)
        application = request['application']
        is_owner = application == self.config['base_application']
        if is_owner != (tenant == 'owner'):
            raise ValueError('developer and Cloud resource identity differ')
        if not is_owner:
            if not re.fullmatch('[a-z0-9]{8,64}', application):
                raise ValueError('invalid member service identity')
            self.member(self.api('services/' + application), tenant, owner)
        if action == 'configure':
            if is_owner:
                self.prepare_network('owner', 'atelier-vps-owner')
                self.api('applications/' + application, {'docker_compose_location': '/docker-compose.coolify-team.yaml'}, 'PATCH')
                values = environment(request)
                self.api('applications/' + application + '/envs/bulk', {'data': [
                    {'key': key, 'value': value, 'is_preview': preview, 'is_buildtime': True, 'is_runtime': True}
                    for preview in (False, True) for key, value in values.items()]}, 'PATCH')
            else:
                self.prepare_network(tenant, application)
                raw = compose(request, self.image(), application, self.base.get('pids_limit', 512))
                self.api('services/' + application, {'docker_compose_raw': base64.b64encode(json.dumps(raw).encode()).decode(),
                    'connect_to_docker_network': False, 'instant_deploy': False}, 'PATCH')
            return {'id': application, 'commit': self.config['approved_revision'], 'configured': True}
        if action == 'deploy':
            return self.api('deploy', {'uuid': application, 'force': False}) if is_owner else self.api('services/' + application + '/start', {})
        if action == 'stop':
            return self.api('applications/' + application + '/stop', {}) if is_owner else self.api('services/' + application + '/stop', {})
        raise ValueError('unsupported Cloud team action')
