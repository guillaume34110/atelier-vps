# Atelier VPS native widget

The owner monitors and shares an existing Linux development worker without
interrupting calculations or neighboring projects. SwiftUI/AppKit is the only UI
runtime; no browser, Electron, background web server or new UI dependency.

Use `Paint` in `macos/WidgetApp.swift`: dark forest backgrounds, sage for available
resources, ivory labels, orange for pressure/errors, a small tricolor accent.
Native system type and tabular figures keep real metrics readable. The image in
`docs/assets/widget-concept.png` is a design reference, never a telemetry source.
Preserve the existing ivory analog dials, orange needles and their rounded cards.
Use `macos/assets/meter-face.png` for both faces: 640×360 pixels, 55,518 bytes,
lossless PNG, displayed at a fixed 16:9 aspect ratio. Only the live needle and
pivot overlay are drawn, in uniform 320×180 artwork coordinates. Never stretch
either axis independently when resizing.
The user explicitly requested no restyling of the full widget. Semantic controls, visible
labels, native keyboard navigation and French/English strings apply to both tabs.

Windows support 340–900 point widths. Heights below 220 points automatically
switch to a mini bar; growing the window restores monitoring. Widths below 440
use compact dials and rows, 440–699 use the standard stack, and 700+ use columns
for agents and activity. Full views scroll. Mini bars start at 400×96 and show
CPU, reservable RAM, committed RAM, slots, disk free, connected agents and transfer
directions. Expanding restores the full frame. Both layouts share the same samples.

An agent is connected while a real calculation or transfer channel is active.
Do not imply that idle chat windows are registered. Transfer counters measure
actual archive bytes, with average speeds; completed transfers remain bounded.
History displays start/end clock times in the Mac's local timezone, with the
date for older activity. Offline figures are visibly stale.

The RAM dial's large value is reservable RAM. Its needle and percentage caption
show actual task use divided by reserved plus still-reservable RAM: 0% when idle,
unknown when task-use accounting is unavailable, warning ticks at the high end.
Supervisor/cache memory does not imply an active task. Preserve the analog style.
Distinguish ceiling, committed reservations and reservable RAM. Unknown values
use a dash, offline values are dimmed and marked, and maintenance is explicit.
Administrative rights come from the separate SSH identity, never a role toggle.
The Management tab appears only after successful authentication. Draft, verified
preview and applied settings are distinct; edits invalidate previews, stale
revisions fail safely, and running work blocks affected changes. No deletion.
Host protection is 1 GiB; worker supervisors and task headroom remain accounted.

Failed history entries are native buttons. Their sheet explains the reported
cause, exit code, measured budgets and bounded log excerpt from the same
authenticated worker. It stays private, verifies the manifest, supports text
selection, close/Escape and explicit retry, and never changes task resources.
Keep original receipts/results separate from the eight derived private cards.

Verification: native build and model checks; real restricted SSH samples;
real screenshots at narrow, normal, wide, short, mini and administrator sizes;
full Linux integration and private-user isolation suite. Web-only contracts do
not apply to this native macOS product. Keep concise receipts with screenshots.
