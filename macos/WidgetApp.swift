import AppKit
import SwiftUI
import Darwin

private enum Configuration {
    static let values: [String: String] = {
        let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/atelier-vps/config.json")
        guard let data = try? Data(contentsOf: file), data.count <= 16384,
              let value = try? JSONDecoder().decode([String: String].self, from: data) else { return [:] }
        return value
    }()
    static let host: String = {
        let value = values["ssh_host"] ?? "dev-vps"
        return value.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", options: .regularExpression) == nil ? "dev-vps" : value
    }()
    static let dashboard: URL = {
        if let value = values["coolify_url"], let url = URL(string: value), url.scheme == "https" { return url }
        return URL(string: "https://github.com/guillaume34110/atelier-vps")!
    }()
}

private enum Paint {
    static let background = Color(red: 0.075, green: 0.10, blue: 0.087)
    static let panel = Color(red: 0.13, green: 0.17, blue: 0.145)
    static let green = Color(red: 0.66, green: 0.88, blue: 0.60)
    static let ivory = Color(red: 0.94, green: 0.88, blue: 0.74)
    static let orange = Color(red: 0.96, green: 0.35, blue: 0.20)
    static let muted = Color(red: 0.64, green: 0.69, blue: 0.65)
}

final class Monitor: ObservableObject {
    @Published var document: VPSSnapshot?
    @Published var cpu = CPUReading(previous: nil, current: nil)
    @Published var connected = false
    @Published var refreshing = false
    @Published var received: Date?
    @Published var latency: Double = 0
    @Published var error = "Connecting…"
    var onUpdate: (() -> Void)?
    var successfulSamples = 0
    var raw = Data()
    private var process: Process?
    private var timer: Timer?
    private var failures = 0
    private var visible = true

    func setVisible(_ value: Bool) {
        visible = value
        timer?.invalidate()
        if value { refresh() }
    }

    func refresh() {
        guard process == nil else { return }
        timer?.invalidate()
        refreshing = true
        let task = Process(), output = Pipe(), errors = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                          "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                          "-o", "ServerAliveCountMax=1", "-o", "LogLevel=ERROR", Configuration.host, "status"]
        task.standardInput = FileHandle.nullDevice
        task.standardOutput = output
        task.standardError = errors
        process = task
        let started = ProcessInfo.processInfo.systemUptime
        do { try task.run() }
        catch { process = nil; finish(nil, started: started); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self, weak task] in
            guard let task = task, self?.process === task, task.isRunning else { return }
            task.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak task] in
                if let task = task, task.isRunning { kill(task.processIdentifier, SIGKILL) }
            }
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            func bounded(_ handle: FileHandle, limit: Int) -> Data {
                var data = Data()
                while let part = try? handle.read(upToCount: 4096), !part.isEmpty {
                    data.append(part)
                    if data.count > limit { if task.isRunning { task.terminate() }; break }
                }
                return data
            }
            let data = bounded(output.fileHandleForReading, limit: 65536)
            _ = bounded(errors.fileHandleForReading, limit: 8192)
            task.waitUntilExit()
            let result = task.terminationStatus == 0 ? data : nil
            DispatchQueue.main.async { [weak self] in
                self?.process = nil
                self?.finish(result, started: started)
            }
        }
    }

    private func finish(_ data: Data?, started: Double) {
        refreshing = false
        if let data = data, let next = try? VPSSnapshot.decode(data) {
            cpu = CPUReading(previous: document?.telemetry, current: next.telemetry)
            document = next
            raw = data
            received = Date()
            latency = ProcessInfo.processInfo.systemUptime - started
            connected = true
            failures = 0
            successfulSamples += 1
            error = ""
        } else {
            connected = false
            failures += 1
            error = "SSH unavailable · retrying automatically"
        }
        onUpdate?()
        if visible {
            timer?.invalidate()
            let delay = failures > 0 ? min(60, 5 * pow(2, Double(min(failures - 1, 4)))) : (document?.busy == true || successfulSamples < 2 ? 5.0 : 30.0)
            timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.refresh() }
            timer?.tolerance = delay * 0.1
        }
    }

    func stop() {
        visible = false
        timer?.invalidate()
        if process?.isRunning == true { process?.terminate() }
        // The shared SSH control master may also serve other agents; leave it intact.
    }
}

private struct GaugeFace: View, Animatable {
    var level: Double
    var known: Bool
    var danger: Double
    var animatableData: Double {
        get { level }
        set { level = newValue }
    }

    var body: some View {
        Canvas { context, size in
            let pivot = CGPoint(x: size.width / 2, y: size.height - 12)
            let radius = min(size.width * 0.46, size.height * 0.86)
            func point(_ fraction: Double, _ scale: Double) -> CGPoint {
                let angle = (200 + 140 * fraction) * .pi / 180
                return CGPoint(x: pivot.x + cos(angle) * radius * scale,
                               y: pivot.y + sin(angle) * radius * scale)
            }
            var face = Path()
            face.move(to: point(0, 1.04))
            for tick in 0...140 { face.addLine(to: point(Double(tick) / 140, 1.04)) }
            face.addQuadCurve(to: CGPoint(x: pivot.x, y: pivot.y + 7),
                              control: CGPoint(x: size.width - 6, y: size.height + 15))
            face.addQuadCurve(to: point(0, 1.04), control: CGPoint(x: 6, y: size.height + 15))
            face.closeSubpath()
            context.fill(face, with: .linearGradient(Gradient(colors: [Paint.ivory.opacity(0.88), Color(red: 1, green: 0.95, blue: 0.80), Paint.ivory]),
                                                     startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
            context.stroke(face, with: .color(.black.opacity(0.7)), lineWidth: 3)
            for tick in 0...50 {
                let value = Double(tick) / 50
                var line = Path()
                line.move(to: point(value, tick % 5 == 0 ? 0.79 : 0.85))
                line.addLine(to: point(value, 0.93))
                context.stroke(line, with: .color(value >= danger ? Paint.orange : .black.opacity(0.78)), lineWidth: tick % 5 == 0 ? 1.2 : 0.65)
                if tick % 10 == 0 {
                    context.draw(Text("\(tick * 2)").font(.system(size: 10, weight: .semibold, design: .rounded)).foregroundColor(.black.opacity(0.8)),
                                 at: point(value, 0.65))
                }
            }
            if known {
                var needle = Path()
                needle.move(to: pivot)
                needle.addLine(to: point(max(0, min(1, level)), 0.86))
                var shadow = context
                shadow.addFilter(.shadow(color: .black.opacity(0.5), radius: 1.5, x: 1, y: 2))
                shadow.stroke(needle, with: .color(Paint.orange), style: StrokeStyle(lineWidth: 2.7, lineCap: .round))
            }
            let cap = Path(ellipseIn: CGRect(x: pivot.x - 7, y: pivot.y - 7, width: 14, height: 14))
            context.fill(cap, with: .linearGradient(Gradient(colors: [.gray, .black]), startPoint: pivot, endPoint: CGPoint(x: pivot.x + 7, y: pivot.y + 7)))
            context.stroke(cap, with: .color(.black.opacity(0.8)), lineWidth: 1.2)
        }
        .opacity(known ? 1 : 0.55)
    }
}

private struct Dial: View {
    let title: String
    let value: String
    let fraction: Double?
    let detail: String
    var danger: Double = 0.85

    var body: some View {
        VStack(spacing: 2) {
            GaugeFace(level: fraction ?? 0, known: fraction != nil, danger: danger)
                .frame(height: 124)
                .animation(.easeInOut(duration: 0.8), value: fraction)
            Text(title).font(.system(size: 11, weight: .bold)).tracking(1.2)
            Text(value).font(.system(size: 20, weight: .semibold, design: .rounded)).monospacedDigit()
            Text(detail).font(.system(size: 9, design: .monospaced)).foregroundColor(Paint.muted).lineLimit(1)
        }
        .padding(.horizontal, 8).padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 14).fill(LinearGradient(colors: [Paint.panel, Paint.background], startPoint: .topLeading, endPoint: .bottomTrailing)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.white.opacity(0.12), lineWidth: 1))
    }
}

private struct Collapsible<Content: View>: View {
    let title: String
    let key: String
    let content: Content
    @State private var expanded: Bool

    init(_ title: String, key: String, @ViewBuilder content: () -> Content) {
        self.title = title; self.key = key; self.content = content()
        _expanded = State(initialValue: UserDefaults.standard.object(forKey: key) as? Bool ?? true)
    }
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) { content.padding(.top, 8) } label: {
            Text(L(title)).font(.system(size: 11, weight: .semibold)).foregroundColor(Paint.ivory)
        }
        .onChange(of: expanded) { UserDefaults.standard.set(expanded, forKey: key) }
        .padding(12).background(RoundedRectangle(cornerRadius: 13).fill(.black.opacity(0.12)))
        .tint(Paint.green)
    }
}

private struct WidgetView: View {
    @ObservedObject var monitor: Monitor
    let hide: () -> Void
    let menu: () -> Void
    @AppStorage("atelier-language") private var language = "system"
    private var snapshot: VPSSnapshot? { monitor.document }
    private var tint: Color { monitor.connected ? Paint.green : .orange }
    private var profile: String { Configuration.host + "." + (snapshot?.scope?.id ?? "owner") }
    private var unit: String { L("GiB") }

    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "gauge.with.dots.needle.67percent").foregroundColor(Paint.ivory)
                Text("Atelier VPS").font(.system(size: 20, weight: .bold, design: .rounded))
                Spacer(minLength: 4)
                Circle().fill(tint).frame(width: 7, height: 7)
                Text(L(monitor.connected ? "Connected" : "Offline")).font(.system(size: 10)).foregroundColor(tint)
                Button(action: { monitor.refresh() }) { Image(systemName: "arrow.clockwise") }
                    .help(L("Refresh")).accessibilityLabel(L("Refresh"))
                Button(action: menu) { Image(systemName: "ellipsis") }.help(L("Options")).accessibilityLabel(L("Options"))
                Button(action: hide) { Image(systemName: "chevron.down") }.help(L("Hide widget")).accessibilityLabel(L("Hide widget"))
            }.buttonStyle(.plain)
            HStack(spacing: 0) { Color(red: 0.20, green: 0.36, blue: 0.72); Paint.ivory; Paint.orange }
                .frame(height: 2).clipShape(Capsule())
            HStack {
                Text(L(snapshot?.scope?.mode == "allocated" ? "Your allocation" : "Available capacity"))
                Spacer()
                Text(L(!monitor.connected ? "Offline" : (snapshot?.busy == true ? "Working" : (snapshot?.admit == true ? "Ready" : "Waiting"))))
            }.font(.system(size: 10)).foregroundColor(Paint.muted)
            ScrollView {
                VStack(spacing: 10) {
                    Collapsible("Resources", key: profile + ".resources") {
                        HStack(spacing: 8) {
                            Dial(title: L("YOUR CPU"), value: percent(monitor.cpu.worker), fraction: monitor.cpu.worker,
                                 detail: LF("Limit %.2f vCPU", snapshot?.telemetry?.worker_cpu_limit ?? Double(snapshot?.pool?.cpu_total ?? 0)))
                            Dial(title: L("YOUR RAM"), value: gib(snapshot?.telemetry?.worker_ram) + " " + unit,
                                 fraction: snapshot?.ramFraction,
                                 detail: LF("Ceiling %@ %@", gib(snapshot?.telemetry?.worker_ram_limit), unit))
                        }.opacity(monitor.connected ? 1 : 0.5)
                        VStack(alignment: .leading, spacing: 7) {
                            Text(LF("Reservable now: %@ %@ RAM", gib(snapshot?.pool?.ram_available), unit))
                                .foregroundColor(Paint.green)
                            if let pool = snapshot?.pool {
                                Text(LF("Committed: %@ %@ RAM", gib(pool.ram_reserved), unit))
                            }
                            Divider()
                            HStack { Text(L("Your storage")); Spacer(); Text(LF("%@ / %@ %@", gib(snapshot?.used_bytes), gib(snapshot?.budget_bytes), unit)) }
                            ProgressView(value: snapshot?.quotaFraction ?? 0).tint(Paint.green)
                            Text(LF("Free: %@ %@", gib(snapshot?.workspace_free_bytes), unit)).foregroundColor(Paint.muted)
                        }.font(.system(size: 11)).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Collapsible("Tasks", key: profile + ".tasks") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(LF("%d / %d tasks", snapshot?.agents.count ?? 0, snapshot?.pool?.max_jobs ?? 4)).foregroundColor(Paint.muted)
                            if monitor.connected, let agents = snapshot?.agents, !agents.isEmpty {
                                ForEach(agents) { agent in
                                    HStack {
                                        Circle().fill(agent.orphaned == true ? .orange : Paint.green).frame(width: 7, height: 7)
                                        VStack(alignment: .leading, spacing: 3) {
                                            Text(agent.agent).fontWeight(.semibold).lineLimit(1)
                                            Text(agent.project).foregroundColor(Paint.muted).lineLimit(1)
                                            Text(LF("Reserved: %@ %@ RAM", gib(agent.ram_bytes), unit)).font(.system(size: 10)).foregroundColor(Paint.muted)
                                        }
                                        Spacer(minLength: 4)
                                        Text(agent.elapsed(at: Date())).monospacedDigit()
                                    }.font(.system(size: 11)).padding(.vertical, 3)
                                }
                            } else { Text(L(monitor.connected ? "No active task" : "Task status unavailable")).foregroundColor(Paint.muted) }
                        }.frame(maxWidth: .infinity, alignment: .leading).font(.system(size: 11))
                    }
                    Collapsible("History", key: profile + ".history") {
                        VStack(alignment: .leading, spacing: 8) {
                            if let recent = snapshot?.recent, !recent.isEmpty {
                                ForEach(Array(recent.prefix(3))) { entry in
                                    HStack(spacing: 7) {
                                        Image(systemName: entry.status == "passed" ? "checkmark.circle.fill" : "exclamationmark.circle")
                                            .foregroundColor(entry.status == "passed" ? Paint.green : .orange)
                                        Text(entry.agent).lineLimit(1)
                                        Spacer(minLength: 4)
                                        Text(L(entry.status == "passed" ? "Completed" : "Failed"))
                                        Text(entry.elapsed(at: Date())).monospacedDigit()
                                    }.font(.system(size: 10))
                                }
                            } else { Text(L("Completed tasks appear here")).font(.system(size: 11)) }
                        }.foregroundColor(Paint.muted).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.id(profile)
            }
            HStack {
                Text(freshness).lineLimit(1)
                Spacer(minLength: 4)
                Button(action: { NSWorkspace.shared.open(Configuration.dashboard) }) {
                    Image(systemName: "arrow.up.right.square")
                }.buttonStyle(.plain).help(L("Dashboard")).accessibilityLabel(L("Dashboard"))
            }.font(.system(size: 10)).foregroundColor(monitor.connected ? Paint.muted : .orange)
        }
        .padding(14).frame(minWidth: 360, idealWidth: 480, maxWidth: .infinity, minHeight: 180, idealHeight: 670, maxHeight: .infinity)
        .foregroundColor(.white.opacity(0.92))
        .background(LinearGradient(colors: [Paint.panel, Paint.background], startPoint: .topLeading, endPoint: .bottomTrailing))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(.white.opacity(0.17), lineWidth: 1))
        .preferredColorScheme(.dark).environment(\.locale, Locale(identifier: UILanguage.code)).id(language)
    }
    private var freshness: String {
        guard let received = monitor.received else { return L(monitor.error) }
        let age = max(0, Int(Date().timeIntervalSince(received)))
        return monitor.connected ? LF("Updated %d s ago · SSH %d ms", age, Int(monitor.latency * 1000)) : LF("Offline · last sample %d s ago", age)
    }
}

private final class WidgetPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class WidgetDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let monitor = Monitor()
    private var panel: WidgetPanel!
    private var statusItem: NSStatusItem!
    private var capturing = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let root = WidgetView(monitor: monitor, hide: { [weak self] in self?.hide() }, menu: { [weak self] in self?.showMenu() })
        let host = NSHostingView(rootView: root)
        let size = NSSize(width: 480, height: 670)
        panel = WidgetPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel, .resizable], backing: .buffered, defer: false)
        panel.minSize = NSSize(width: 360, height: 180)
        panel.maxSize = NSSize(width: 900, height: 1200)
        panel.title = "Atelier VPS"
        panel.contentView = host
        panel.delegate = self
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        if !panel.setFrameUsingName("usevps-widget") {
            let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 900)
            panel.setFrameOrigin(NSPoint(x: screen.maxX - size.width - 24, y: max(screen.minY + 24, screen.maxY - size.height - 30)))
        }
        panel.setFrameAutosaveName("usevps-widget")
        if let screen = panel.screen?.visibleFrame {
            var frame = panel.frame
            frame.size.width = min(max(360, frame.width), screen.width)
            frame.size.height = min(max(180, frame.height), screen.height)
            frame.origin.x = min(max(screen.minX, frame.minX), screen.maxX - frame.width)
            frame.origin.y = min(max(screen.minY, frame.minY), screen.maxY - frame.height)
            panel.setFrame(frame, display: false)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "usevps")
        statusItem.button?.toolTip = L("Show widget")
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggle)
        panel.orderFrontRegardless()
        monitor.onUpdate = { [weak self] in self?.captureIfReady() }
        monitor.refresh()
        if CommandLine.arguments.contains("--capture") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 45) { [weak self] in
                if self?.capturing != true { fputs("Live widget capture did not receive two real SSH samples\n", stderr); exit(1) }
            }
        }
    }

    @objc func toggle() { if panel.isVisible { hide() } else { show() } }
    @objc func show() { panel.orderFrontRegardless(); monitor.setVisible(true) }
    @objc func hide() { panel.orderOut(nil); monitor.setVisible(false) }
    @objc func refresh() { monitor.refresh() }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func pin(_ sender: NSMenuItem) {
        panel.level = panel.level == .floating ? .normal : .floating
        sender.state = panel.level == .floating ? .on : .off
    }
    @objc func showMenu() {
        let menu = NSMenu()
        for (title, action) in [("Show widget", #selector(show)), ("Hide widget", #selector(hide)), ("Refresh", #selector(refresh))] {
            let item = NSMenuItem(title: L(title), action: action, keyEquivalent: ""); item.target = self; menu.addItem(item)
        }
        let pinned = NSMenuItem(title: L("Always on top"), action: #selector(pin(_:)), keyEquivalent: "")
        pinned.target = self; pinned.state = panel.level == .floating ? .on : .off; menu.addItem(pinned)
        menu.addItem(.separator())
        let languages = NSMenuItem(title: L("Language"), action: nil, keyEquivalent: "")
        let choices = NSMenu()
        for (code, title) in [("system", L("System language")), ("fr", "Français"), ("en", "English")] {
            let item = NSMenuItem(title: title, action: #selector(setLanguage(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = code
            item.state = (UserDefaults.standard.string(forKey: "atelier-language") ?? "system") == code ? .on : .off
            choices.addItem(item)
        }
        languages.submenu = choices; menu.addItem(languages)
        let quit = NSMenuItem(title: L("Quit Atelier VPS"), action: #selector(quit), keyEquivalent: "q")
        quit.target = self; menu.addItem(quit)
        menu.popUp(positioning: nil, at: NSPoint(x: panel.frame.maxX - 120, y: panel.frame.maxY - 44), in: nil)
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { hide(); return false }
    func applicationWillTerminate(_ notification: Notification) { monitor.stop() }

    @objc private func setLanguage(_ sender: NSMenuItem) {
        guard let code = sender.representedObject as? String, ["system", "fr", "en"].contains(code) else { return }
        UserDefaults.standard.set(code, forKey: "atelier-language")
        statusItem.button?.toolTip = L("Show widget")
    }

    private func captureIfReady() {
        guard !capturing, monitor.successfulSamples >= 2,
              let index = CommandLine.arguments.firstIndex(of: "--capture"), index + 1 < CommandLine.arguments.count else { return }
        capturing = true
        if CommandLine.arguments.contains("--exercise-controls") {
            precondition(panel.styleMask.contains(.resizable))
            let originalSize = panel.frame.size
            panel.setContentSize(NSSize(width: 390, height: 360))
            precondition(panel.frame.width == 390 && panel.frame.height == 360)
            panel.setContentSize(originalSize)
            let selected = UserDefaults.standard.string(forKey: "atelier-language")
            let languageItem = NSMenuItem(); languageItem.representedObject = "en"
            setLanguage(languageItem); precondition(L("Resources") == "Resources")
            languageItem.representedObject = "fr"
            setLanguage(languageItem); precondition(L("Resources") == "Ressources")
            UserDefaults.standard.set(selected ?? "system", forKey: "atelier-language")
            hide(); precondition(!panel.isVisible)
            show(); precondition(panel.isVisible)
            let before = panel.level
            let item = NSMenuItem()
            pin(item); precondition(panel.level != before)
            pin(item); precondition(panel.level == before)
            refresh(); precondition(monitor.refreshing)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [self] in
            do {
                let path = URL(fileURLWithPath: CommandLine.arguments[index + 1])
                guard let workspace = ProcessInfo.processInfo.environment["TASK_WORKSPACE"],
                      path.standardizedFileURL.path.hasPrefix(URL(fileURLWithPath: workspace).standardizedFileURL.path + "/"),
                      !FileManager.default.fileExists(atPath: path.path),
                      let view = panel.contentView,
                      let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw MonitorError.invalidData }
                view.cacheDisplay(in: view.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { throw MonitorError.invalidData }
                try png.write(to: path, options: .withoutOverwriting)
                let evidence: [String: Any] = ["source": "real restricted SSH", "samples": monitor.successfulSamples,
                    "connected": monitor.connected, "cpu_fraction": monitor.cpu.worker.map { $0 as Any } ?? NSNull(),
                    "bundle": Bundle.main.bundlePath, "window_width": view.bounds.width, "window_height": view.bounds.height,
                    "controls_exercised": CommandLine.arguments.contains("--exercise-controls"),
                    "status": try JSONSerialization.jsonObject(with: monitor.raw)]
                try JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys, .prettyPrinted])
                    .write(to: path.deletingPathExtension().appendingPathExtension("json"), options: .withoutOverwriting)
                print("Real native widget capture saved: \(path.path)")
                if CommandLine.arguments.contains("--exit-after-capture") { quit() }
            } catch { fputs("Widget capture failed: \(error)\n", stderr); exit(1) }
        }
    }
}

@main
struct WidgetMain {
    static func main() {
        if CommandLine.arguments.contains("--self-test") {
            do { try runModelChecks() } catch { fputs("Mac model checks failed: \(error)\n", stderr); exit(1) }
            return
        }
        let application = NSApplication.shared
        let delegate = WidgetDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
