# Deploy your own workbench

The reference host is Linux ARM64 with four vCPU and about 8 GiB RAM. The locked
Ubuntu 24.04 toolchain supports ARM64 and x86_64 Linux; each project still needs
compatible binaries. Docker/Compose and SSH are operator prerequisites. Do not
install a second daemon or change another project's Docker configuration.

Use an additional, dedicated ext4 volume. Never point these steps at `/`, a
system partition or a volume with unrelated projects. Attaching storage in a
provider's console does not mount it or make it visible in Coolify.

## Prepare the added volume

Mount the new volume using its provider instructions. Verify its device, UUID,
filesystem, capacity and mount point with `lsblk -f`, `findmnt` and `df -h`.
This repository does not format disks.

On the server, set your actual values and keep these variables for the setup:

```sh
export USEVPS_VOLUME_MOUNT=/mnt/atelier-vps
export USEVPS_VOLUME_ID=YOUR_REAL_EXT4_UUID
python3 ops/prepare_volume.py --apply
python3 ops/provision_env.py --root "$USEVPS_VOLUME_MOUNT/.usevps-dev" --reserve-gib 0.5
python3 ops/prepare_volume.py --apply
```

These operator commands require root. Inspect them before use. Enabling project
quota filesystem features is accepted only on an empty volume; an already
configured project-quota volume can retain its existing data. The scripts verify
the UUID, preserve the previous fstab and refuse unexpected state. Provisioning
verifies download checksums and installs one shared toolchain on the added volume.
Allow several GiB for download/extraction peaks before starting. The final quota
uses remaining capacity after tools, with a 512 MiB physical margin.

Copy `ops/deployment.example.json` to `ops/deployment.local.json` and fill in your
values. This local file is ignored by Git. Generate only the personal mount
override:

```sh
python3 ops/render_compose.py --config ops/deployment.local.json > docker-compose.local.yaml
docker compose -f docker-compose.yaml -f docker-compose.local.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.local.yaml up -d --build
```

The base Compose file deliberately has no personal host mounts. Without the
nine required binds and correct UUID, health refuses the configuration. Defaults
keep UID/GID 10001, read-only root, no capabilities/new privileges, 5 GiB RAM,
no extra swap, low CPU shares, bounded PIDs and rotated Docker logs.

## Coolify

Create a Docker Compose application from your repository and use
`docker-compose.yaml`. Disable automatic deployment and pin a revision whose CI
is green. In that application's **persistent storage**, configure the nine binds
produced by `ops/render_compose.py`: workspace read/write, capacity probe and
seven toolchain paths read-only. This makes the added volume visible in Coolify
without relocating global Docker storage. Ensure the generated Compose retains
all `:ro` flags; verify actual container mounts before admitting work.

Set application runtime values: `USEVPS_VOLUME_ID` to the real UUID,
`USEVPS_RESERVE_GIB=0.5`, `USEVPS_HOST_RAM_RESERVE_GIB=1` and
`USEVPS_VOLUME_STATS=/volume-stat`. The capacity probe is empty and inaccessible;
it must never store data. Older Coolify versions may mishandle variable-based
mount sources; explicit application binds avoid that substitution issue.

### Coolify 4 / Coolify Cloud

For a Git application whose Compose editor is read-only, choose
`/docker-compose.coolify.yaml` as the Compose file. In Advanced, select **Raw
(deploy file as-is)** and **Manual deployments only**. Set
`USEVPS_VOLUME_MOUNT` to the prepared dedicated volume mount and
`USEVPS_VOLUME_ID` to its verified UUID. Leave public domains and published ports
empty. Pin a revision with both required CI checks green before deploying.

This portable file includes the nine binds and the same CPU, RAM, process,
read-only-root and temporary-filesystem protections as `docker-compose.yaml`.
CI compares the rendered worker settings and rejects a missing volume mount.
Changing its host-specific values does not require putting personal addresses
or paths into Git. The original Compose file remains available for existing
installations with explicit Coolify storage entries or a local override.

## Restricted SSH access

Create a dedicated Ed25519 key on the client, mode 0600, outside the checkout.
Transfer only its **public** half to the operator. Install the forced gateway:

```sh
python3 ops/install_gateway.py /path/to/public-key.pub ops/gateway.py EXACT_CONTAINER_NAME
```

This also requires root and the volume variables above. With plain Compose the
default container is `ateliervps-worker`; Coolify assigns its own application
container name. The installer refuses conflicting users or authorized keys.
It grants only the fixed gateway command, not arbitrary sudo, an interactive
host shell or Docker access.

On the client configure an SSH alias, keeping your own host and key path:

```sshconfig
Host dev-vps
    HostName vps.example.com
    User usevps
    IdentityFile ~/.ssh/id_ed25519_atelier_vps
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    ControlMaster auto
    ControlPersist 60
    ControlPath ~/.ssh/cm-%C
```

Verify the host fingerprint independently and add the verified host to
known_hosts. Install the client with `scripts/install.py`, then query status.
Do not publish your private key, environment or personal deployment file.

## Update safely

Require a PR, a current branch and green `checks`/`macos-widget` checks on the
latest revision. Keep administrators subject to protections. Pin the verified
commit in Coolify. On the server, hold `ops/deploy_lock.py --hold` in the
foreground through deployment and health validation, then send `release`.
Install that script with its gateway metadata path for operator use. A refused
lock means active work must finish first; never kill it to force an update.

Verify UID, mounts, quota, container limits and live status after deployment.
Run the examples and a pair of concurrent named jobs, fetch their verified
outputs and preserve receipts before release. Check neighboring containers have
not changed. Roll back to a verified revision under the same exclusive lock if
health or required real scenarios fail.
