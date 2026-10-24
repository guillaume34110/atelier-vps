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

Tools: `vps_status`, `vps_run`, `vps_fetch`, `vps_release`. Set the host's timeout
above the longest remote job. The routing skill prefers compatible VPS work;
it cannot override permissions, unavailable resources or incompatible runtimes.

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
