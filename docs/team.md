# Private developer allocations

Atelier VPS has two modes. **Opportunistic** gives one developer access to the
available capacity of a VPS that also hosts other projects. **Allocated** gives
developers separate workers, identities and quotas. Their immutable Node,
Python, Java and browser tools are mounted once, read-only; projects, writable
caches, logs and results stay private.

This is container isolation for a trusted development team. The host kernel,
physical disk, network and CPU scheduling remain shared. A percentage is a
ceiling in an explicit pool, not a guarantee of physical CPU throughput.

## RAM that learns from completed work

```sh
atelier-vps status
atelier-vps run --source ./api --agent "API tests" \
  --ram-gib auto --cpus 1 --disk-gib 1 --timeout 900 -- npm test
```

`auto` is the CLI default; omit `ram_gib` in `vps_run`. A new workload reserves
up to 1 GiB. Successful runs retain the largest observed process-tree peak;
future reservations add 25% and 64 MiB, rounded up to 64 MiB. Profiles are
private, bounded to 128 entries, and keyed by project, argument array and root
dependency-lock digest. A changed lock starts a new profile. RAM-limited runs
establish a lower bound and cannot teach a smaller reservation.

Explicit `--ram-gib N` remains available for a new/heavier build with a known
peak. Profiles do not guarantee future demand: code, inputs and environment
can change without changing a lock. There is no automatic retry of arbitrary
commands and no silent reduction to fit a busy server.

Receipts distinguish current RAM from `resources.ram_peak_bytes`, with the
sampling interval. Linux child `ru_maxrss` also catches short exited processes;
simultaneous sub-sample process-tree peaks can still escape aggregate sampling.
Per-job checks are sampled; the container's memory and ext4 quota are hard limits.
Admission errors identify the missing resource and requested/available bytes.

The reference service retains the 1 GiB host floor, 5 GiB global worker envelope
and 512 MiB physical volume margin. The live reservable amount can be much
smaller than the worker ceiling. Do not increase reservations to force saturation.

## Install the separate administrator identity

After the complete CI suite and protected integration, hold `ops/deploy_lock.py
--hold` on the server. Keep that foreground SSH connection alive throughout
deployment and verification. Install the root helpers from the approved revision:

```sh
python3 ops/install_team.py --application YOUR_COOLIFY_APPLICATION \
  --admin-key /root/atelier-administrator.pub
```

Use a dedicated local private key, kept in SSH with mode 0600. Only its `.pub`
file is transferred. The installed `avps-admin` account has a forced SSH command
and narrowly scoped sudo helper; it has no interactive administrator shell.
Set `admin_ssh_host` in your private `~/.config/atelier-vps/config.json` to its
strictly verified SSH alias. Ordinary developer installations omit this field.

MCP tools are `admin_settings_get`, `admin_settings_preview` and
`admin_settings_apply`. They require the administrator alias even though their
schemas can be listed. The CLI fallback is:

```sh
atelier-vps settings get
atelier-vps settings preview --policy /your/private/policy.json
atelier-vps settings apply --policy /your/private/policy.json \
  --expected-revision HASH_FROM_THE_PREVIEW
```

## Allocation example

This example deliberately uses a 2 GiB **worker** RAM pool, including each
worker's supervisors. The real preview must approve it against current host
memory. Replace the example public key before applying.

```json
{
  "mode": "allocated",
  "pool": {"cpus": 4, "ram_bytes": 2147483648,
           "disk_bytes": 26843545600, "slots": 4},
  "users": [
    {"id": "owner", "cpu_percent": 50, "ram_percent": 50,
     "disk_percent": 50, "slots": 2},
    {"id": "marie", "public_key": "ssh-ed25519 REPLACE_WITH_PUBLIC_KEY",
     "cpu_percent": 50, "ram_percent": 50, "disk_percent": 50, "slots": 2}
  ]
}
```

Each developer gets a 2-vCPU ceiling, a 1-GiB worker limit, two task slots and
12.5 GiB of private disk in this example. The task budget excludes 64 MiB per
slot for supervisors. CPU reservations use integer scheduling slots within the
worker; fractional container CPU quotas still cap total CPU time.
The developers share a total CPU scheduling weight of at most 128, preserving
the service's low priority relative to existing projects.
CPU ceilings use `cpu_quota` with a 100,000-microsecond period, supported by
Coolify 3's Compose 2.6 client. Allocations round down to that precision and
require at least 0.01 vCPU. A plain service-level `cpus` field is ignored by
that older client; the administrator adapter writes both scheduling and kernel
quota settings.

Workers cannot change inode flags or project IDs: an additional inherited
seccomp filter protects quota attributes while preserving Docker's existing
filter. Compatible workloads must use native 64-bit ARM or x86 instructions;
32-bit compatibility ABIs are refused. This prevents an ordinary file owner from
moving files outside its project quota.

Percentages apply to the declared pool, converted to absolute limits before
application. Active CPU/RAM allocations and slots cannot exceed the pool.
Disabled developers retain their disk allocation and data. Deleting users or
reducing quotas below existing data is refused. No automatic purge exists.

All workers use sibling ext4 project quotas on the added volume. The original
single workspace quota must be divided, not retained in addition to new quotas.
Shared tools and metadata are counted before the disk pool; physical headroom
is checked again before applying. The original system disk is never overflow
space for projects.

## Safe application and recovery

Only changed developers are drained and redeployed. Any active job refuses the
update before mutations begin. Unaffected developers continue. Each developer
has its own Coolify application, pinned to the approved commit of the main
application; automatic deployment remains disabled. The original adapter uses
the installed **Coolify 3.12** API. Coolify Cloud is supported through its v4 API,
with one Git-pinned owner application and a private service per additional
developer. Each service reuses the approved owner's local immutable image with
`pull_policy: never`; it does not install another toolchain.

For Cloud, pass `--cloud-config /root/atelier-cloud.json` to `install_team.py`.
This must be a root-owned mode-0600 JSON file with `token`, `base_application`,
`approved_revision`, `server_uuid`, `project_uuid`, `environment_uuid` and
`network_pool`. The credential needs Read, Write and Deploy, without Root or
sensitive-read access. It is copied only to `/etc/atelier-vps-cloud.json` and is
never returned by settings or stored in a developer workspace. Keep it valid for
ongoing administration; renew or revoke it through your Coolify account.
The pinned revision must have completed the required CI checks. Use an unused
private IPv4 /16 for `network_pool`: the helper chooses non-overlapping /28
bridges after checking existing Docker networks and host routes. It never
changes Docker's global address pools or another project's networks.

The helper reads CPU, RAM, slots and host memory floor from the live solo owner
at initialization. The original 4-vCPU/5-GiB/1-GiB installation remains unchanged;
a larger server keeps its explicitly configured envelope. Preview limits total
allocated RAM to actual host availability beyond that floor. For example, a
48-GiB ceiling with about 44.5 GiB currently allocatable supports two 22-GiB
workers. Whole-core shares receive distinct CPU sets so each worker does not
pin its tasks to the same first cores; CPU quotas and low priority remain active.

Allocated workers receive private bridges. Host and private-network access is
blocked by rules matching only their bridge, including IPv6. Public Internet
and localhost services inside a worker remain usable. The gateway checks the
expected private mounts, memory limit, CPU quota and network before execution.
After an external redeploy, restore that worker's private network under its
maintenance lock before reopening admission; an unexpected network fails closed.
The root repair hook is `python3 /usr/local/lib/usevps/team_settings.py
--repair-network DEVELOPER_ID`; it refuses active jobs or pending settings.

SSH identity is bound to the workspace in root-owned settings. Agent labels and
caller-supplied IDs cannot select another developer. Status and refusal payloads
in allocated mode omit host statistics; file retrieval and release can only
reach the authenticated worker's mounted workspace.

Settings use a revision and an atomic configuration commit. A failed multi-step
Coolify operation leaves `/etc/atelier-vps-team.pending.json`, preserves data and
blocks new work for affected developers. Inspect the failure and resume the exact
same policy against the revision returned by `settings get`; do not delete the
marker to bypass it.
Some affected services may require recovery before retrieval; unaffected workers
remain available. The operation does not claim to roll back external deployments.

Both modes retain the host memory-pressure stop to protect neighboring projects.
Allocated limits contain one developer's OOM; they cannot guarantee uninterrupted
work if unrelated services consume the host's safety margin. Provision enough
physical RAM for the expected simultaneous peaks.

## Native widget

The widget displays only its authenticated worker's CPU, RAM, storage and tasks.
CPU is normalized to its own quota, not all host cores. It distinguishes the
worker ceiling, committed reservations and RAM reservable now.

French and English follow the system language or the Language menu. Resources,
Tasks and History fold independently with preferences per SSH profile/user.
Resize the window; narrow or short layouts scroll. Visible active work polls at
5 seconds, idle at 30 seconds, errors back off to 60 seconds. Hidden widgets stop
polling. There are no per-second animation or elapsed-time timers, browser, web
server or duplicate monitoring service.
