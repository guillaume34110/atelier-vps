# Contributing

Welcome to the atelier. Keep changes small, measurable and safe for neighboring
projects. A VPS is a shared machine; refusal is preferable to silent overload.

1. Start a feature branch. Preserve the configured Git identity.
2. Read `AGENTS.md`, the architecture and deployment guides.
3. Install Python 3.12, uv and Docker, then run `uv sync --frozen`.
4. Run `TASK_WORKSPACE=/your/bounded/temporary/directory uv run --frozen python scripts/checks.py`.
   This runs all unit tests, builds the real image, starts real services and
   exercises transfers, concurrent jobs, resource stops, browser, Python and Java.
   Toolchain provisioning requires Linux and several GiB of free space. On macOS,
   use an explicitly configured existing Linux Docker host as documented.
5. On macOS also run `macos/build.py` inside a bounded `TASK_WORKSPACE`. It compiles
   the native UI, runs model checks and verifies the local signature.
6. Open a PR; both `checks` and `macos-widget` must pass on the latest revision.
   Keep the branch current. Deploy only a verified, pinned revision.

Do not commit credentials, endpoints from your private infrastructure, result
files, generated caches or environment directories. Do not weaken quotas,
host-key checks, process cleanup, deployment locks or E2E assertions to make a
test pass. Never run destructive cleanup against another project's resources.
