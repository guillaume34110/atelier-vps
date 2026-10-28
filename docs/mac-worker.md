# Private Mac worker

An agent already running in a developer's VPS worker may request a task on that
developer's Apple Silicon Mac. The Mac keeps a lightweight SSH listener for its
own configured developer scope. No listener gives another team member access to
the Mac. When idle, no virtual machine runs. Each request checks Mac CPU load,
memory pressure and the existing disk reserve; a busy or offline Mac refuses it.

The task runs as UID 10001 in a fresh Linux ARM64 VM, at low priority, with a
bounded ext4 disk and no network device. Only the filtered source and declared
results are exchanged over the authenticated SSH connection. Personal browser
profiles, SSH keys and the Mac home directory are not mounted. There is no
public port or SSH key copied to a VPS. One Mac task runs at a time.

The initial guest provides Alpine Linux with musl, Python 3.14 and Node 24.
It does not provide Ubuntu/glibc, x86, a browser, Android, native macOS tools or
Metal. Install dependencies before transfer only when they are compatible with
that guest; jobs requiring network access stay on a suitable VPS or machine.
The filtered source limit is 64 MiB, results are limited to 256 MiB and logs to
18 MiB. Include the source archive and returned files in the calling VPS task's
disk reservation.

## Prepare and activate on the Mac

The Mac needs Apple Silicon, the macOS Virtualization framework, Xcode command
line tools, `e2fsprogs` (`mke2fs`), RTK and the configured local
`workspace-budget.py` and `storage-guard.py` helpers. Prepare from an approved
checkout under its normal disk guard. The installer downloads pinned Alpine
boot files and uses Alpine-signed packages in a temporary bootstrap VM; normal
work VMs have no network.

```sh
rtk proxy python3 ~/.local/bin/workspace-budget.py --temp-gib 1.4 run "$PWD" -- \
  rtk proxy python3 macos/compute/prepare.py --mke2fs /opt/homebrew/bin/mke2fs
rtk proxy python3 macos/compute/activate.py \
  --bundle output/task-tmp/mac-worker-bundle --mke2fs /opt/homebrew/bin/mke2fs
```

Activation verifies the bundle against the current source, keeps any existing
different version, and starts a background LaunchAgent for the current macOS
user. Its default scope is `owner`; `--scope` selects a different developer's
own scope. Only connections whose registered scope matches are used. Preserve
the preparation receipt and remove only that preparation's temporary files
after verifying the installed worker. The Mac's local agents continue to prefer
the VPS for Linux-compatible work.

## Use from a VPS agent

Check availability, then submit from a reserved VPS task. `--output` must be a
new relative directory inside the calling task; it receives `receipt.json`,
`stdout.log` and the verified declared files in `results/`.

```sh
rtk proxy python3 /app/mac_client.py --status
rtk proxy python3 /app/mac_client.py --source . --output mac-result \
  --cpu 1 --ram-mib 512 --disk-mib 128 --timeout 60 --result report.txt -- \
  python3 -c 'open("report.txt", "w").write("hello from the Mac worker")'
```

The caller verifies the archive and every result SHA256 before acknowledging
receipt. The Mac releases only that acknowledged task. If the connection is
lost, retain the reported job ID and recover rather than rerun it. Recovery
waits for the private listener and the current task without repeatedly waking
the agent; `--timeout` bounds that wait in seconds:

```sh
rtk proxy python3 /app/mac_client.py --recover JOB_ID --output recovered-result --timeout 600
```

An incomplete or unacknowledged result remains on the Mac and blocks a new Mac
task until it is recovered or inspected. A refusal happens before execution;
an unknown outcome after admission is not a successful task.
