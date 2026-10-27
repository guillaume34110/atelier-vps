#!/usr/bin/python3
"""Install administrator settings under the deployment lock, after green CI."""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys


def install(application, admin_key):
    target = Path('/usr/local/lib/usevps')
    target.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    for name in ['team.py', 'team_settings.py', 'coolify_team.js', 'coolify_cloud.py', 'render_compose.py', 'deploy_lock.py', 'gateway.py', 'invitations.py', 'install_enrollment.py']:
        file = target / name
        temporary = target / ('.' + name + '.tmp')
        if temporary.is_symlink():
            raise ValueError('installation temporary must not be a symlink')
        shutil.copyfile(source / name, temporary); temporary.chmod(0o644); os.chown(temporary, 0, 0)
        if name == 'deploy_lock.py':
            temporary.chmod(0o755)
        temporary.replace(file)
    gateway = Path('/usr/local/libexec/usevps-gateway')
    shutil.copyfile(source / 'gateway.py', gateway); gateway.chmod(0o755); os.chown(gateway, 0, 0)
    helper = Path('/usr/local/libexec/atelier-settings')
    helper.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/usr/local/lib/usevps")\nfrom team_settings import main\n'
                      'try:\n main()\nexcept Exception as error:\n import json\n print(json.dumps({"ok":False,"error":str(error)}),file=sys.stderr)\n sys.exit(1)\n')
    helper.chmod(0o755); os.chown(helper, 0, 0)
    sys.path.insert(0, str(target))
    import team_settings
    result = team_settings.initialize(application, str(admin_key))
    import install_enrollment
    result.update(install_enrollment.install())
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True)
    parser.add_argument('--admin-key', required=True, type=Path)
    parser.add_argument('--cloud-config', type=Path)
    args = parser.parse_args()
    if (os.geteuid() != 0 or not re.fullmatch('[a-z0-9]+', args.application) or args.admin_key.suffix != '.pub'
            or args.admin_key.is_symlink() or not args.admin_key.is_file() or args.admin_key.stat().st_size > 256):
        raise ValueError('root, exact Coolify application and a regular administrator .pub file are required')
    try:
        from . import team_settings as settings
        from .gateway import WORKER_EXEC
        from .deploy_lock import deployment_lock
    except ImportError:
        import team_settings as settings
        from gateway import WORKER_EXEC
        from deploy_lock import deployment_lock
    base = settings.read(settings.BASE)
    raw = settings.command(['docker', 'exec', '-i', '--user', '10001:10001', base['container'], *WORKER_EXEC, 'status'], timeout=15)
    current = json.loads(raw)
    if current.get('maintenance') is not True or current.get('pool', {}).get('active_count') != 0:
        raise ValueError('hold the canonical deployment lock on the idle owner before installation')
    with settings.LOCK.open('a') as lock, ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = settings.read(settings.STATE) if settings.STATE.exists() else {'tenants': {}}
        # Shared helpers affect every team member, including workers unchanged by this release.
        for tenant in state['tenants'].values():
            if tenant['workspace'] != base['workspace']:
                stack.enter_context(deployment_lock(Path(tenant['workspace'])))
        if args.cloud_config:
            try:
                from .coolify_cloud import CloudTeam, configuration
            except ImportError:
                from coolify_cloud import CloudTeam, configuration
            cloud = configuration(args.cloud_config)
            CloudTeam(cloud, base, settings.command, None).owner(args.application)
            settings.save(settings.CLOUD, cloud)
        install(args.application, args.admin_key)


if __name__ == '__main__':
    main()
