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

The original analog CPU dial shows authenticated worker use. The RAM dial shows
RAM **reservable now** in large text. Its needle shows actual task RAM use divided
by the current task envelope (reserved plus still reservable), with a percentage
caption and warning marks at the high end. An idle pool reads 0%, even when worker
supervisors or caches occupy memory. Missing usage readings remain unavailable.
It does not present the container ceiling as available memory.
Storage, reservations, agents and recent results belong to that same
developer. Host/system disk and other developers' metrics are not displayed.
The widget distinguishes the memory ceiling, committed reservations and RAM
reservable now. Unknown readings, offline samples and maintenance are identified.
Public labels are limited to 64 characters; keep secrets out of them.

One monitoring SSH request is in flight at a time. Polling uses 5 seconds with active jobs or transfers and
30 seconds when idle, backs off to 60 seconds on failure and stops while hidden.
There are no per-second timers; a localized timestamp identifies the last sample.
Strict host verification stays enabled. Timeouts stop only the widget's SSH
frontend, preserving the shared multiplexed connection.

Choose French, English or the system language in the menu. Resources, Connected
agents, Transfers and History fold independently, preserving preferences for the
SSH profile and user. Agents are connected during a calculation or an active
file transfer, not while an idle chat window happens to be open. Job IDs deduplicate
an upload and its calculation. Transfers show real archive bytes, direction,
average throughput and start/end clock times. History shows the same local clock
times, plus a date for older tasks. No commands, paths or secrets are published.

Resize the window from 340 points wide; short layouts scroll, and the saved size
is restored within the screen bounds. Data refreshes never overwrite that size.
Below 220 points high it automatically becomes a mini bar. Growing it switches
back to the full view. Below 440 points wide dials/rows compact themselves;
from 700 points agents and activity use columns. The same authenticated data and
administrator draft survive these layout transitions.

The mini-bar button (or Options → Mini bar layout) reduces the panel to 400×96
points, resizable from 340 points wide. It keeps CPU, reservable RAM, committed
RAM, task slots, free storage, connected agents and transfer directions visible.
The expand button restores the full
window; the chosen layout survives relaunch. Offline values are dimmed and marked.

An **administrator-authenticated** connection adds Monitoring / Management tabs.
Set `admin_ssh_host` in the private client configuration to the separately
provisioned administrator SSH alias. The widget only opens the Management tab
after a successful `settings get`; a developer connection cannot grant this role.
Administrator actions use a separate bounded SSH request and do not poll.

Management edits the Solo / Team mode, pool CPU/RAM/disk/slots, member allocations,
enabled access and public ed25519 keys. Preview validates the complete candidate
on the server; Apply uses that exact preview and revision. Editing invalidates
the preview. Active tasks block affected changes; stale revisions require reload.
Interrupted applications retain their exact candidate for resumption. Solo
restores the owner's full CPU/RAM pool and disables other members while retaining
their files and disk quota. No account or data is automatically deleted.

The 1 GiB host floor protects the other projects and still limits reservable RAM;
the 5 GiB ceiling does not mean 5 GiB are available at every moment.

The [design reference](assets/widget-concept.png) is a mockup. The running app is
implemented with native controls and actual authenticated telemetry.

For a real UI check use `macos/check_live.py --app /path/to/usevps.app` inside a
bounded `TASK_WORKSPACE`. It reads real status twice, exercises controls and
captures its own view plus a small JSON receipt. Add `--require-telemetry` after
deploying the current worker. Add `--layouts` to capture narrow, standard, wide,
short, three mini-bar sizes and administrator layouts when authenticated. This
changes local drafts only; it does not apply policy. Screenshots prove that pass.
The optional `--admin` check also exercises the real administrator
get/preview/apply transport with an unchanged policy, then verifies it by reading
the policy again. It refuses to apply any preview that affects a member.
