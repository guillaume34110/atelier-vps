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

Two analog dials show the authenticated worker's CPU/RAM, normalized to its own
limits. Storage, reservations, agents and recent results belong to that same
developer. Host/system disk and other developers' metrics are not displayed.
The widget distinguishes the memory ceiling, committed reservations and RAM
reservable now. Unknown readings, offline samples and maintenance are identified.
Public labels are limited to 64 characters; keep secrets out of them.

One SSH request is in flight at a time. Polling uses 5 seconds with active jobs and
30 seconds when idle, backs off to 60 seconds on failure and stops while hidden.
There are no per-second timers; a localized timestamp identifies the last sample.
Strict host verification stays enabled. Timeouts stop only the widget's SSH
frontend, preserving the shared multiplexed connection.

Choose French, English or the system language in the menu. Resources, Tasks and
History fold independently, preserving preferences for the SSH profile and user.
Resize the window from 360 points wide; short layouts scroll, and the saved size
is restored within the screen bounds. Data refreshes never overwrite that size.

For a real UI check use `macos/check_live.py --app /path/to/usevps.app` inside a
bounded `TASK_WORKSPACE`. It reads real status twice, exercises controls and
captures its own view plus a small JSON receipt. Add `--require-telemetry` after
deploying the current worker. Screenshots prove that particular live pass.
