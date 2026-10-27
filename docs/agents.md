# Programs on your VPS

The optional desktop runs the official ChatGPT/Codex and Claude Desktop
applications **on the selected VPS**. Open **Agents → Start a one-hour desktop**
in the Mac widget, choose **ChatGPT/Codex** or **Claude/Cowork**, then sign in
inside the remote window with your account. One graphical program runs per
member at a time; ordinary jobs can still share the remaining pool.
An inference API key is not required. Never paste account credentials into a
conversation or copy a provider's authentication files between machines.

ChatGPT for Linux supports Ubuntu 24.04 and ARM64 in preview.
[Official Linux installation guide](https://learn.chatgpt.com/docs/linux/linux-app).
Claude Desktop supports Linux ARM64 in beta. New Cowork tasks on Pro/Max run on
Anthropic's infrastructure; the remote app connects the VPS folders you select.
Local Cowork requires KVM, which Hetzner Cloud does not provide.
[Claude availability](https://support.claude.com/en/articles/10065433-install-claude-desktop),
[Cowork execution](https://support.claude.com/en/articles/15520349-use-claude-cowork-on-web-desktop-and-mobile),
[Hetzner virtualization](https://docs.hetzner.com/cloud/servers/faq/).

The desktop shares the existing worker pool. It reserves one CPU, uses measured
RAM with the usual single margin, and keeps the host reserve. A first launch
requests 2.5 GiB RAM and a 6 GiB installation peak. An installed runtime requests
a 2 GiB working peak. Admission can refuse either request; it never reduces the
claim. The existing 3,600-second limit also applies to a desktop session.
An agent can supply a known `ram_gib` peak for a different workload; it must not
shrink a refused claim or apply a second margin. Active claims are preserved.
Finish its tasks before that deadline or before choosing **Close desktop**.

Closing the screen-sharing window disconnects the display. The VPS programs
continue until the session ends. Quitting the widget closes its own tunnel.
The next session reuses the private profile and files in
`/workspace/.desktops/home`; keep projects in its `projects` folder. These
files are excluded from source uploads and from managed-cache retention.
The desktop shares its member's trusted development worker; different team
members retain separate workers and files.
Finished job receipts remain available through the ordinary verified
fetch/preserve/release workflow. Copy selected project results into a normally
reserved task's output when returning them to the Mac.

Program packages are pinned by SHA256. Ubuntu dependencies use signed package
indexes and private apt state; no host package manager or provider installation
script is executed. Disposable runtimes follow the managed 30-day cache policy.
The read-only base toolchain remains available.

Screen sharing uses the existing restricted SSH connection and a local
loopback listener. No VNC port is published. Its temporary password is supplied
privately to the system viewer and never returned by MCP or written to logs.
An 8 MiB, non-executable temporary filesystem holds X server locks; it is owned
by the restricted worker user and counts within the existing container RAM limit.
The original Compose worker allows 512 processes/threads, including control
requests. The Coolify Cloud manifest allows 768: real desktop startup on x86_64
can exceed 512 before exhausting RAM. The real startup check rejects PID refusals
and verifies 32 spare slots at the measured peak. CPU, RAM, job-count and disk
limits remain enforced; existing workers keep their configured limit.
The agent tool `vps_agent_desktop` supports status/start and a confirmed stop;
it never returns a login code or viewer ticket.

```sh
atelier-vps --server my-server desktop status
atelier-vps --server my-server desktop start --provider chatgpt
atelier-vps --server my-server desktop open --id SESSION_ID
# After finishing and saving the remote tasks:
atelier-vps --server my-server desktop stop --id SESSION_ID
```
