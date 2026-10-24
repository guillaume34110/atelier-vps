# Mac widget

A native SwiftUI/AppKit floating panel, available on all Spaces with a menu-bar
control. It is not a Notification Center extension: that platform budgets refresh
frequency. Build with macOS 14+ and Xcode command-line tools.

```sh
python3 scripts/install.py --host dev-vps --coolify-url https://coolify.example.com
TASK_WORKSPACE=/your/bounded/build-directory python3 macos/build.py
```

The directory must exist. The script compiles the app, runs model self-checks,
writes a receipt and verifies an ad-hoc signature. Copy `usevps.app` to
`~/Applications/Atelier VPS.app` and open it. No Apple credential or persistent
local server is installed.

Two analog dials show host CPU/RAM. Worker memory, system disk, workspace quota,
reservations, active agents and recent results remain visible. Unknown and stale
readings are identified. Public labels are limited to 64 characters; keep secrets
and confidential names out of them.

One SSH request is in flight at a time. Polling uses 5 seconds when busy and
10 seconds when idle, backs off to 60 seconds on failure and stops while hidden.
Strict host verification stays enabled. Timeouts stop only the widget's SSH
frontend, preserving the shared multiplexed connection.

For a real UI check use `macos/check_live.py --app /path/to/usevps.app` inside a
bounded `TASK_WORKSPACE`. It reads real status twice, exercises controls and
captures its own view plus a small JSON receipt. Add `--require-telemetry` after
deploying the current worker. Screenshots prove that particular live pass.
