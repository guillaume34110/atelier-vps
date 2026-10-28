# Local agents and ChatGPT

Run `python3 scripts/install.py --host YOUR_RESTRICTED_SSH_ALIAS` from the clone.
It writes endpoints to `~/.config/atelier-vps/config.json`, creates
`~/.local/bin/atelier-vps` and installs the routing skill in
`~/.agents/skills/atelier-vps`. Keep `~/.local/bin` on PATH.

With uv and a working Codex CLI, it registers the local stdio MCP server and
sets its tool timeout to 3700 seconds, preserving other settings. Restart the
agent host to discover the tools. See the
[official MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

For another compatible host, configure this command with absolute paths:

```json
{"mcpServers":{"atelier-vps":{
  "command":"/absolute/path/to/uv",
  "args":["run","--frozen","--project","/path/to/atelier-vps","python","/path/to/atelier-vps/mcp_server.py"]
}}}
```

Tools: `vps_status`, `vps_run`, `vps_project_prepare`, `vps_project_run`, `vps_wait`,
`vps_fetch`, `vps_release`. Set the host's timeout
above the longest remote job. The routing skill prefers compatible VPS work;
it cannot override permissions, unavailable resources or incompatible runtimes.
These tools share the widget's private registry and choose a compatible server
with the complete requested resources. Keep the returned `connection:JOB_ID`
reference when fetching or releasing. `vps_server_add` and
`vps_connect_from_clipboard` register connections; administrator settings and
invitation tools require the separate administrator identity.
See [multiple servers and invitations](fleet.md).

`vps_run` holds one request open until the worker has completed the job and
returns its receipt. Avoid repeatedly asking the model to poll for it. To wait
for a job that is already running, obtain its ID from `vps_status` and call
`vps_wait` with `job_id="connection:JOB_ID"`, or use:

```sh
atelier-vps wait connection:JOB_ID --timeout 3600
```

The worker waits over the existing authenticated connection and returns
`passed`, `failed`, `running` (the wait expired) or `unknown` (the worker cannot
confirm the result). A timeout or unknown state never means success: preserve
the job and investigate instead of automatically starting it again. Fetch and
verify the original outputs before releasing the job. The MCP host must keep
the call alive; the server cannot wake a closed client or monitor a generation
on another service without an integration for that service. No log, command or
secret is included in the wait response.

For one-off work or frequently changing sources, use `vps_run` to stream the
filtered sources for the job, then fetch and preserve the needed results and
release the job promptly. This avoids retaining a project version after each edit.
For a stable project used repeatedly, `vps_project_prepare` transfers its filtered
sources into a versioned, developer-private location on one updated worker.
Record the returned `connection:project:revision` reference; preserve and release
the separate preparation job as usual. `vps_project_run` materializes a verified
copy inside each new reserved job, without transferring the project again. Pass
the local `source` path when available: changed files reject the old revision,
and preparing again creates a new version without overwriting the old one.
Concurrent tasks operate on separate copies; retrieve their results with `vps_fetch`.
Sources are limited to the normal 64 MiB filter, count against the worker quota,
and are never automatically removed. A project reference belongs to one
developer and one server; another team member prepares their own copy.

Run compatible CI and browser scenarios on the VPS when they do not depend on a
personal browser account. Start the actual application and API, reuse one
Chromium session for related checks, and return concise assertions and only the
screenshots needed to verify the result. Existing examples use `CHROMIUM_PATH`.
For personal mail or other account-specific sessions, keep the existing browser
and its profile on the original machine. Save credentials only through the
host's password manager or a secure CI secret store; do not copy profiles,
cookies or credentials to the worker or put them in job arguments or logs.

Fetch creates `output/task-tmp` in the supplied project. With the canonical
`~/.local/bin/workspace-budget.py` helper, MCP uses that guard with a 0.6 GiB
transfer allowance. Otherwise it requires a new task directory and a 10% disk
reserve, at least 1 GiB, plus transfer headroom. Stricter local policy applies.
Existing workspaces are never overwritten.

```sh
python3 ~/.local/bin/workspace-budget.py --temp-gib 0.6 run /path/to/project -- \
  atelier-vps fetch JOB_ID stdout.log receipt.json work/result.json
```

Preserve verified outputs outside temporary storage before finishing your own
transfer pass and releasing its remote job. Keep other agents' files intact.

ChatGPT in a browser does not acquire SSH access by cloning a repository. It
requires an explicitly configured supported connection, authentication and
network reachability. OpenAI's [MCP documentation](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
describes remote servers and Secure MCP Tunnel. This project ships local stdio;
it does not open an unauthenticated public HTTP bridge.
