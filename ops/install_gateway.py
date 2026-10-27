#!/usr/bin/python3
"""Install the dedicated SSH account and its narrowly scoped sudo gateway."""
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

from prepare_volume import MOUNT, VOLUME_ID, WORKSPACE


def main():
    if os.geteuid() != 0 or len(sys.argv) != 4 or not re.fullmatch(r"[a-z0-9]+-worker", sys.argv[3]):
        raise ValueError("root, public-key file, gateway source and exact container name are required")
    process_ceiling = os.environ.get('USEVPS_PIDS_LIMIT', '512')
    if process_ceiling not in {'512', '768'}:
        raise ValueError('USEVPS_PIDS_LIMIT must be 512 or 768')
    memory_ceiling = int(float(os.environ.get('USEVPS_POOL_RAM_GIB', '5')) * 1024 ** 3)
    if not 64 * 1024 ** 2 <= memory_ceiling <= 48 * 1024 ** 3:
        raise ValueError('USEVPS_POOL_RAM_GIB must be between 0.0625 and 48')
    public = Path(sys.argv[1]).read_text().strip()
    if not public.startswith("ssh-ed25519 ") or len(public.split()) < 2:
        raise ValueError("expected an ed25519 public key")
    public = " ".join(public.split()[:2]) + " atelier-vps"
    if not VOLUME_ID:
        raise ValueError('USEVPS_VOLUME_ID must identify the prepared added volume')
    config = Path("/etc/usevps-gateway.json")
    try:
        user = pwd.getpwnam("usevps")
        if user.pw_uid != 10001 or not config.exists():
            raise ValueError("existing account is not this installation")
    except KeyError:
        try:
            pwd.getpwuid(10001)
        except KeyError:
            pass
        else:
            raise ValueError("uid 10001 is already allocated")
        subprocess.run(["useradd", "--uid", "10001", "--create-home", "--shell", "/bin/sh", "usevps"], check=True)
    home = Path("/home/usevps")
    home.chmod(0o755)
    os.chown(home, 0, 0)
    ssh = home / ".ssh"
    ssh.mkdir(exist_ok=True)
    ssh.chmod(0o755)
    os.chown(ssh, 0, 0)
    authorized = ssh / "authorized_keys"
    line = 'restrict,command="sudo -n /usr/local/libexec/usevps-gateway" ' + public + "\n"
    if authorized.exists() and authorized.read_text() != line:
        raise ValueError("unexpected existing authorized keys; manual inspection required")
    authorized.write_text(line)
    authorized.chmod(0o444)
    os.chown(authorized, 0, 0)
    target = Path("/usr/local/libexec/usevps-gateway")
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(sys.argv[2], target)
    target.chmod(0o755)
    os.chown(target, 0, 0)
    config.write_text(json.dumps({"mount": str(MOUNT), "workspace": str(WORKSPACE), "volume_id": VOLUME_ID,
                                  "container": sys.argv[3], "pids_limit": int(process_ceiling),
                                  "memory_limit_bytes": memory_ceiling}))
    config.chmod(0o644)
    sudo = Path("/etc/sudoers.d/usevps-gateway")
    content = 'Defaults:usevps env_keep += "SSH_ORIGINAL_COMMAND"\nusevps ALL=(root) NOPASSWD: /usr/local/libexec/usevps-gateway ""\n'
    temporary = Path("/etc/sudoers.d/.usevps-gateway.tmp")
    temporary.write_text(content)
    temporary.chmod(0o440)
    subprocess.run(["visudo", "-cf", str(temporary)], check=True, capture_output=True)
    temporary.replace(sudo)
    print(json.dumps({"user": "usevps", "gateway": str(target), "interactive_shell": False}))


if __name__ == "__main__":
    main()
