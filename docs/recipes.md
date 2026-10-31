# Project routing and reusable VPS recipes

Use one command for a recurring project check:

```sh
atelier-vps task test --source ./my-project --disk-gib 2 --agent "Tests · API"
atelier-vps task test --source ./my-project --disk-gib 2 --plan
```

The equivalent MCP tool is `vps_task`. It discovers standard Node scripts
(`test`, `test:unit`, `build`, `lint`, `typecheck`, `check`, `e2e`) and Python
unittest/pytest projects, or reads `.atelier-vps.json`. Python TOML discovery
requires Python 3.11+; explicit JSON recipes and the existing client remain
available on Python 3.10. Unknown recipes return `unconfigured`; they do not
silently execute on the Mac. Use the existing `vps_run` for an identified one-off
Linux command, or add an explicit recipe.

Recipes select an authenticated, compatible VPS that can admit the **complete**
CPU/RAM/disk reservation. Sources already present on an admissible worker are
preferred. No executed command is retried elsewhere. An explicit `local` recipe
returns `local_required` with its command; the agent runs it on the required
machine under its normal local guards. This keeps macOS, Metal, devices, Docker
daemons and personal browser profiles outside the shared Linux worker.

## Reusable sources and environments

The first call fingerprints filtered sources, stores an immutable version and
runs the recipe in its own verified working copy, within one reserved job.
The next call probes that version and sends only the request and empty archive
framing, not the project again. Local edits produce a new version. Parallel
tasks get independent working copies, including writable dependencies and
generated outputs; they never modify the stored version.

The project ID is stable across Git worktrees of the same checkout. Set `id` in
the JSON configuration to share the same logical project ID across clones or
machines of the same developer. Each authenticated developer retains a separate
private namespace.

SDKs remain shared and read-only. Recipe setup reconstructs each job's own
dependencies using private package caches keyed by project and dependency locks.
For example, npm uses `npm ci`, pnpm uses its frozen lockfile, and uv uses
`uv sync --frozen`. A mutable virtualenv or `node_modules` directory is never
shared between concurrent jobs. Managed caches retain their existing 30 idle
day policy; installed base tools, project versions and useful results are not
automatically deleted.

Reserve the full peak: the initial stored source plus its working copy, setup,
caches, downloads, temporary files, logs and outputs. If two source copies alone
exceed the declared peak, routing keeps the recipe remote but uses a temporary
source transfer instead of storing another version. Quotas and physical margins
remain enforced throughout copying, setup and execution. `--plan` reports
`stored`, `reused` or `temporary` without creating a job or writing a version.

## Explicit recipes and complete E2E

```json
{
  "version": 1,
  "id": "my-api",
  "recipes": {
    "e2e": {
      "setup": [["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]],
      "services": [{
        "name": "api",
        "argv": ["node", "api.js", "{port:api}"],
        "ready_url": "http://127.0.0.1:{port:api}/health",
        "timeout": 30
      }],
      "argv": ["npm", "run", "e2e"],
      "artifacts": ["test-results"]
    },
    "android-device": {
      "platform": "local",
      "argv": ["adb", "devices"]
    }
  }
}
```

Setup succeeds before services start. Every configured service must return an
HTTP 2xx response before tests run, and must remain alive throughout the recipe.
Readiness URLs use literal loopback addresses, with no credentials or query.
`{port:api}` reserves a distinct private port for each parallel execution without
creating a network listener or monitoring service. The test receives
`ATELIER_SERVICE_API_URL` and `ATELIER_SERVICE_API_ORIGIN`. Use `CHROMIUM_PATH`
and the documented headless browser flags; do not install another Chromium.

Failed setup, missing services, an early service exit or test failure makes the
job fail. Only this recipe's service process groups are stopped. The overall
job timeout also covers source preparation; CPU, RAM, disk, logs and process
guards apply to its descendants. Configuring a service does not publish a host
port or enable SSH port forwarding.

Commands are argument arrays. Keep secrets out of configuration, commands,
sources, caches and result paths. Personal browser sessions stay on their
original machine. The result includes a `route`, the source `project_ref` when
stored, and `suggested_paths`. Fetch useful outputs and failure evidence with
SHA256 verification, preserve them, then explicitly release that `job_ref`.
