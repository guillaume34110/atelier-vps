---
name: atelier-vps
description: Prefer the configured shared VPS for Linux ARM64 builds, tests, Chromium, type checks and file processing; reserve resources and preserve verified results.
---

Use the `atelier-vps` MCP tools whenever a compatible development task can run on
the configured VPS, especially when local memory is under pressure. Check
`vps_status` first. Only proceed with healthy admission and enough available CPU,
RAM and disk for the complete peak. Busy can be true while admission is true:
several agents can share the pool.

Call `vps_run` with the actual source directory, an argument array, a short public
agent label and explicit CPU, RAM, disk and timeout reservations. The service
supports at most four simultaneous jobs; reserve only what the task needs. Idle
CPU cores are lent automatically. Never bypass refused admission with another
worker, root SSH, Docker or a virtual machine.

Sources are filtered and streamed. Never transfer secrets, private SSH keys,
environments, local dependencies, Git history or `CLAUDE.md`. Recreate Linux
ARM64 dependencies from lockfiles and use the existing shared toolchain/caches.
For browser work use `CHROMIUM_PATH`, `--no-sandbox` and
`--disable-dev-shm-usage`. Run real required services before claiming E2E success.

Use `vps_fetch` for logs, receipts and useful outputs; SHA256 verification is
mandatory. Preserve useful files outside the temporary directory, then remove
only your own completed transfer temporaries. Call `vps_release` only after that
preservation. Other jobs and results must remain intact.

Keep macOS APIs, Metal/MLX, GPU work, Docker daemon tasks, Android emulators and
incompatible SDK/NDK binaries on a compatible machine. A timeout, memory stop,
quota stop or incomplete receipt is a failure. Diagnose it and rerun the full
required validation; never report it as passed. Preserve neighboring projects
and all local disk/memory guards.

The MCP connection is local stdio over an existing restricted SSH alias. A cloned
repository alone does not grant a cloud chat access to the local computer. See
the repository's integration guide for supported host setup.
