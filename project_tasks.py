"""Route a named project recipe once, reusing an immutable source version."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import projects
import recipes
from telemetry import public_label


def identity(source, configured):
    if configured is not None:
        return projects.identifier(configured)
    key = str(source)
    try:
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', '--path-format=absolute',
                                 '--git-common-dir', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines()
        if result.returncode == 0 and len(lines) == 2:
            key = lines[0] + '/' + str(source.relative_to(Path(lines[1])))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    name = re.sub('[^a-z0-9-]', '-', source.name.lower()).strip('-')[:20] or 'project'
    if not name[0].isalpha():
        name = 'p-' + name[:18]
    return projects.identifier(name + '-' + hashlib.sha256(key.encode()).hexdigest()[:8])


def execute(source, recipe, agent, disk_gib, cpus=1, ram_gib=None, timeout=900,
            server=None, arch='any', plan_only=False):
    import vps
    source = Path(source).expanduser().resolve()
    if not source.is_dir() or not isinstance(recipe, str):
        raise ValueError('an existing source directory and a recipe name are required')
    if (not math.isfinite(disk_gib) or disk_gib <= 0
            or ram_gib is not None and (not math.isfinite(ram_gib) or ram_gib <= 0)
            or type(cpus) is not int or cpus < 1 or type(timeout) is not int or not 1 <= timeout <= 3600
            or arch not in {'any', 'aarch64', 'x86_64'}):
        raise ValueError('invalid complete task reservation')
    # Apply the same credential/worktree policy before opening project manifests.
    files = list(vps.inputs(source, math.ceil(disk_gib * vps.GIB)))
    configured, found = recipes.discover(source)
    if recipe not in found:
        return 2, {'ok': False, 'status': 'unconfigured', 'recipe': recipe,
                   'available_recipes': sorted(found), 'reason': 'configure this recipe in ' + recipes.CONFIG}
    value = found[recipe]
    if value['platform'] == 'local':
        return 2, {'ok': False, 'status': 'local_required', 'recipe': recipe,
                   'reason': 'recipe requires a local platform, device or Docker daemon', 'argv': value['argv']}
    if value['arch'] != 'any':
        if arch not in {'any', value['arch']}:
            raise ValueError('requested architecture conflicts with the project recipe')
        arch = value['arch']
    digest, entries = projects.fingerprint(files, root=source)
    name = identity(source, configured)
    size = sum(x['bytes'] for x in entries)
    raw = json.dumps(value, separators=(',', ':'))
    if len(raw.encode()) > 49152:
        raise ValueError('recipe exceeds its transport budget')
    request = {'argv': ['python3', '/app/recipes.py', raw], 'recipe_run': True,
               'project_task': {'id': name, 'revision': digest, 'upload': False},
               'project': public_label(name, 'Projet'), 'agent': public_label(agent, 'Recette'),
               'disk_bytes': math.ceil(disk_gib * vps.GIB), 'cpus': cpus,
               'ram_bytes': None if ram_gib is None else math.ceil(ram_gib * vps.GIB),
               'timeout': timeout, 'source_bytes': size, 'dependency_digest': vps.dependency_digest(source)}
    if len(json.dumps(request).encode()) > 65535:
        raise ValueError('recipe request exceeds the gateway budget')
    selected = vps.select_server(request, server, arch)
    present = selected.get('plan', {}).get('project', {}).get('present', False)
    reusable = present or 2 * size < request['disk_bytes']
    route = {'target': 'vps', 'server': selected['id'], 'recipe': recipe,
             'source_revision': digest, 'source_bytes': size,
             'mode': 'reused' if present else 'stored' if reusable else 'temporary',
             'setup': value['setup'], 'services': [x['name'] for x in value['services']],
             'suggested_paths': ['receipt.json', 'stdout.log', *['work/' + p for p in value['artifacts']]]}
    if plan_only:
        return 0, {'ok': True, 'status': 'planned', 'route': route, 'reservation': selected.get('plan', {}).get('requested')}
    chosen = dict(selected['request'])
    if reusable:
        chosen['project_task'] = {'id': name, 'revision': digest, 'upload': not present}
    else:
        chosen.pop('project_task')
    selected['request'] = chosen
    rc, result = vps.run_selected(selected, None if present else source)
    result['route'] = route
    if result.get('managed_project'):
        result['project_ref'] = selected['id'] + ':' + name + ':' + digest
    return rc, result
