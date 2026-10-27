# Several workbenches, one place

Each physical server belongs to one team. Your private connection registry is
shared by the CLI, MCP integration and Mac widget. A server can grant you ordinary
development access, a separate administrator identity, or both.

## Join with an invitation

Install the client from your clone, then open the [Mac widget](widget.md):

```sh
python3 scripts/install.py
```

A fresh installation starts without an invented server. In the widget, choose
**+ → Paste invitation → Connect**. The administrator's private link expires
after 30 minutes and enrolls one device. Its key is created locally; only the
public half is sent. The SSH host key is pinned by the invitation. No private
key is copied from the administrator, the Mac or another server.

On a machine without the widget, enter the link through standard input:

```sh
atelier-vps connect --stdin
```

Paste the link, then press Return. Treat it as a temporary password: share it
privately, never commit it, and never paste it into an agent conversation.
The MCP `vps_connect_from_clipboard` tool can consume a link you explicitly copied
on your Mac without returning that link in its response.

Enrollment uses the existing SSH listener. A temporary identity can only run
the enrollment helper: no shell, port forwarding, interactive terminal, project
access or administrator access. The server binds the invitation to the first
public key, writes the additional developer key and locks the temporary login.
The current primary key and resource policy remain unchanged. A lost response
can be resumed by the same local device. Expired enrollment accounts have no
home or results; only those service-owned accounts are cleaned on the next
invitation request.

## Monitor Global, then each server

**Global** sums the pools you can actually use on connected, enabled servers.
It excludes offline samples and readings older than 60 seconds, and counts a physical machine once, even when two
aliases reach it. Older gateways need an update before their identity can be
included alongside other machines. A single older connection remains compatible.

Below the gauges, server rows show team, RAM reservable now, active agents and
storage. Select a row or use the server picker for its detailed activity.
CPU use is weighted by each worker's CPU limit. RAM occupation is total measured
task use divided by the sum of committed and still-reservable RAM. It is zero
with no active calculation; worker overhead and caches are not task occupation.

The combined figures are not a distributed RAM address space. A calculation must
fit entirely on one machine. Routing checks fresh authenticated status and the
worker's learned RAM plan before uploading sources; it never shrinks a claim to
fit, nor repeats an arbitrary command after an executed failure. A stopped or
unavailable machine does not contribute capacity.

```sh
atelier-vps status
atelier-vps --server paris status
atelier-vps run --source ./api --agent 'API tests' --cpus 1 \
  --ram-gib auto --disk-gib 1 --timeout 600 -- npm test
```

Use `--arch aarch64`, `x86_64` or `any` according to the workload. The default
`any` considers both architectures; pin one for architecture-specific binaries
or dependencies. Source filtering, admission and all task limits still apply.
The result includes a reference such as `paris:JOB_ID`; keep that complete
reference for `fetch` and `release`. Changing the selected server never changes
where an old result lives. Disabling a connection retains its keys and results.

## Add your own server

Deploy a server following the [server guide](deployment.md). In **+ → Use an
existing SSH alias**, enter its name, team and restricted developer alias. A
separate administrator alias is optional and is authenticated against that same
server. The CLI and MCP `vps_server_add` provide the same registration:

```sh
atelier-vps servers add --id paris --name Paris --team 'Équipe Paris' \
  --ssh-host paris-worker --admin-host paris-admin
```

Keep up to sixteen connection profiles. The private registry is
`~/.config/atelier-vps/connections.json`; managed SSH configurations and public
host pins live alongside it. The public repository contains no personal endpoint
or credential. Use a new connection ID to replace a machine or workspace, so
older result references remain addressable.

## Administer a team

Select its server and open **Management**, visible only after separate
administrator authentication. Solo lends the full CPU/RAM pool to the owner;
Team divides it between private developers. The activity card shows every
member's authenticated counters at the last explicit refresh. Settings are not
polled continuously.

To add a person, choose **Add a member**, set the ID and resource shares, then
**Preview → Apply**. New members start with an unusable public placeholder;
you never have to exchange their public keys by hand. Active calculations block
affected changes. A reduced disk quota is refused when it would endanger
preserved data. Disabling a member keeps their workspace and disk allocation.

Once applied, choose **Copy invitation** beside that member. The owner can use
this same button to connect another device without rotating an existing key.
The link never grants administration. Up to eight additional device keys per
member and eight outstanding invitations keep this small service bounded.

The equivalent agent workflow uses `admin_settings_get`,
`admin_settings_preview`, `admin_settings_apply`, then
`admin_invitation_create(member, expected_revision, server)`. Invitation creation
returns a private local file path and expiry, never the secret link itself.
Give the file to its recipient through your own private channel.

```sh
atelier-vps --server paris invitations create --member camille \
  --expected-revision REVISION_FROM_SETTINGS_GET
```

The invitation file is created exclusively with mode 0600 in the private
configuration directory. Existing primary keys and data are retained. Device
authorization follows the member's enabled state and isolated worker.

SSH restrictions use OpenSSH's [forced commands and per-group rules](https://man.openbsd.org/sshd_config).
Installation validates configuration and compares existing login policies before
reloading SSH. The new rule is included at the end of the main configuration;
unrelated logins and established sessions remain intact. The complete CI suite
exercises real SSH password/key authentication in a disposable container with no
published port, including host-pin rejection and one-use replay rejection.
