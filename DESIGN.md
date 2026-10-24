# Atelier VPS native widget

The owner monitors and shares an existing Linux development worker without
interrupting calculations or neighboring projects. SwiftUI/AppKit is the only UI
runtime; no browser, Electron, background web server or new UI dependency.

Use `Paint` in `macos/WidgetApp.swift`: dark forest backgrounds, sage for available
resources, ivory labels, orange for pressure/errors, a small tricolor accent.
Native system type and tabular figures keep real metrics readable. The image in
`docs/assets/widget-concept.png` is a design reference, never a telemetry source.
Preserve the existing ivory analog dials, orange needles and their rounded cards.
The user explicitly requested no restyling of the full widget. Semantic controls, visible
labels, native keyboard navigation and French/English strings apply to both tabs.

Full windows support 360–900 point widths and vertical scrolling down to 240
points high. Mini bars start at 400×96 points, with a minimum width of 340. They
show CPU, RAM reservable now, committed RAM, slots and disk free. Expanding
restores the full frame. Monitoring uses the same samples in both layouts.

The RAM dial's value is reservable RAM, with the needle relative to its task
envelope and warning ticks at the low end. Preserve its original analog style.
Distinguish ceiling, committed reservations and reservable RAM. Unknown values
use a dash, offline values are dimmed and marked, and maintenance is explicit.
Administrative rights come from the separate SSH identity, never a role toggle.
The Management tab appears only after successful authentication. Draft, verified
preview and applied settings are distinct; edits invalidate previews, stale
revisions fail safely, and running work blocks affected changes. No deletion.
Host protection is 1 GiB; worker supervisors and task headroom remain accounted.

Verification: native build and model checks; real restricted SSH samples;
real screenshots at narrow, normal, wide, short, mini and administrator sizes;
full Linux integration and private-user isolation suite. Web-only contracts do
not apply to this native macOS product. Keep concise receipts with screenshots.
