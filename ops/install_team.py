#!/usr/bin/python3
"""Install administrator settings under the deployment lock, after green CI."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True)
    parser.add_argument('--admin-key', required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[a-z0-9]+', args.application) or args.admin_key.suffix != '.pub':
        raise ValueError('root, exact Coolify application and an administrator .pub file are required')
    target = Path('/usr/local/lib/usevps')
    target.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    for name in ['team.py', 'team_settings.py', 'coolify_team.js', 'deploy_lock.py']:
        file = target / name
        shutil.copyfile(source / name, file); file.chmod(0o644); os.chown(file, 0, 0)
        if name == 'deploy_lock.py':
            file.chmod(0o755)
    gateway = Path('/usr/local/libexec/usevps-gateway')
    shutil.copyfile(source / 'gateway.py', gateway); gateway.chmod(0o755); os.chown(gateway, 0, 0)
    helper = Path('/usr/local/libexec/atelier-settings')
    helper.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/usr/local/lib/usevps")\nfrom team_settings import main\n'
                      'try:\n main()\nexcept Exception as error:\n import json\n print(json.dumps({"ok":False,"error":str(error)}),file=sys.stderr)\n sys.exit(1)\n')
    helper.chmod(0o755); os.chown(helper, 0, 0)
    sys.path.insert(0, str(target))
    import team_settings
    import json
    print(json.dumps(team_settings.initialize(args.application, str(args.admin_key))))


if __name__ == '__main__':
    main()
