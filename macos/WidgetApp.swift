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
    @Published var error = "Connexion au VPS…"
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
            error = "SSH indisponible · nouvelle tentative automatique"
        }
        onUpdate?()
        if visible {
            timer?.invalidate()
            let delay = failures > 0 ? min(60, 5 * pow(2, Double(min(failures - 1, 4)))) : (document?.busy == true ? 5.0 : 10.0)
            timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.refresh() }
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

private struct WidgetView: View {
    @ObservedObject var monitor: Monitor
    let hide: () -> Void
    let menu: () -> Void

    private var snapshot: VPSSnapshot? { monitor.document }
    private var tint: Color { !monitor.connected ? .orange : (snapshot?.busy == true || snapshot?.admit == true ? Paint.green : .orange) }

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 9) {
                Image(systemName: "gauge.with.dots.needle.67percent").font(.system(size: 20)).foregroundColor(Paint.ivory)
                Text("Atelier VPS").font(.system(size: 22, weight: .bold, design: .rounded))
                Spacer()
                Circle().fill(tint).frame(width: 7, height: 7)
                Text(monitor.connected ? "Connecté" : (monitor.received == nil && monitor.refreshing ? "Connexion…" : "Hors ligne"))
                    .font(.system(size: 11, weight: .medium)).foregroundColor(tint)
                Button(action: { monitor.refresh() }) { Image(systemName: "arrow.clockwise") }
                    .help("Actualiser les mesures").accessibilityLabel("Actualiser les mesures")
                Button(action: menu) { Image(systemName: "ellipsis") }
                    .help("Options du widget").accessibilityLabel("Options du widget")
                Button(action: hide) { Image(systemName: "chevron.down") }
                    .help("Masquer dans la barre des menus").accessibilityLabel("Masquer le widget")
            }
            .buttonStyle(.plain)
            HStack(spacing: 0) {
                Color(red: 0.20, green: 0.36, blue: 0.72)
                Paint.ivory
                Paint.orange
            }.frame(height: 2).clipShape(Capsule()).opacity(0.8)
            HStack {
                Text("ARM64 · \(snapshot?.telemetry?.cpu_count.map { String($0) } ?? "—") vCPU · \(snapshot?.agents.count ?? 0) / \(snapshot?.pool?.max_jobs ?? 4) tâches")
                Spacer()
                Text(!monitor.connected ? "HORS LIGNE" : (snapshot?.busy == true ? "OCCUPÉ" : (snapshot?.admit == true ? "PRÊT" : "EN ATTENTE")))
                    .fontWeight(.semibold).foregroundColor(tint)
            }
            .font(.system(size: 9, design: .monospaced)).foregroundColor(Paint.muted)
            HStack(spacing: 10) {
                Dial(title: "CPU VPS", value: percent(monitor.cpu.host), fraction: monitor.cpu.host,
                     detail: "worker \(percent(monitor.cpu.worker))")
                Dial(title: "RAM VPS", value: ramValue, fraction: snapshot?.ramFraction,
                     detail: "\(gib(snapshot?.host_available_ram)) Gio libres · seuil 2 Gio",
                     danger: ramDanger)
            }
            .opacity(monitor.connected ? 1 : 0.5)
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    label("ESPACE AGENTS")
                    Spacer()
                    Text("\(gib(snapshot?.workspace_free_bytes)) Gio libres").font(.system(size: 13, weight: .semibold)).monospacedDigit()
                }
                GeometryReader { geometry in
                    Capsule().fill(.black.opacity(0.25))
                    Capsule().fill((snapshot?.quotaFraction ?? 0) > 0.9 ? Color.orange : Paint.green)
                        .frame(width: max(0, geometry.size.width * (snapshot?.quotaFraction ?? 0)))
                }.frame(height: 7)
                Text("Quota \(gib(snapshot?.budget_bytes)) Gio · marge \(reserveText)")
                    .font(.system(size: 10, design: .monospaced)).foregroundColor(Paint.muted)
                if let pool = snapshot?.pool {
                    Text("Réservé : \(pool.cpu_reserved) CPU · \(gib(pool.ram_reserved)) Gio RAM · \(gib(pool.disk_reserved)) Gio disque")
                        .font(.system(size: 9, design: .monospaced)).foregroundColor(Paint.ivory)
                }
            }.padding(13).background(card)
            VStack(alignment: .leading, spacing: 10) {
                HStack { label("LES AGENTS À L’ATELIER"); Spacer(); Text(stateText).font(.system(size: 10)).foregroundColor(tint) }
                HStack(spacing: 9) {
                    node("laptopcomputer", "Mac", active: false)
                    link
                    node("person.crop.square", "Agent", active: monitor.connected && snapshot?.active != nil)
                    link
                    node("server.rack", "VPS", active: monitor.connected && snapshot?.busy == true)
                }.padding(.horizontal, 14)
                if monitor.connected, let agents = snapshot?.agents, !agents.isEmpty {
                    ForEach(agents) { active in
                    HStack {
                        Circle().fill(Paint.green).frame(width: 7, height: 7)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(active.agent).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                            Text("\(active.project) · \(active.cpus ?? 1) CPU · \(gib(active.ram_bytes)) Gio RAM")
                                .font(.system(size: 10)).foregroundColor(Paint.muted).lineLimit(1)
                        }
                        Spacer()
                        Text(active.orphaned == true ? "À vérifier" : "En cours").font(.system(size: 9))
                            .foregroundColor(active.orphaned == true ? .orange : Paint.green)
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            Text(active.elapsed(at: context.date)).font(.system(size: 11, design: .monospaced))
                        }
                    }.padding(9).background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.035)))
                    }
                } else {
                    Text(!monitor.connected ? "État de l’agent inconnu pendant la déconnexion" : (snapshot?.busy == true ? "Calcul ou maintenance · agent non identifié" : (snapshot?.admit == true ? "Prêt pour le prochain agent" : "En attente de ressources disponibles")))
                        .font(.system(size: 11)).foregroundColor(Paint.muted).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 8)
                }
                if let recent = snapshot?.recent, !recent.isEmpty {
                    label("ACTIVITÉ RÉCENTE")
                    ForEach(Array(recent.prefix(3))) { entry in
                        HStack(spacing: 7) {
                            Image(systemName: entry.status == "passed" ? "checkmark.circle.fill" : "exclamationmark.circle")
                                .foregroundColor(entry.status == "passed" ? Paint.green : .orange)
                            Text(entry.agent).lineLimit(1)
                            Spacer()
                            Text(entry.status == "passed" ? "Terminé" : (entry.status == "failed" ? "Échec" : "Interrompu"))
                            Text(entry.elapsed(at: Date())).monospacedDigit()
                        }.font(.system(size: 10)).foregroundColor(Paint.muted)
                        .accessibilityLabel("Activité récente : \(entry.agent), \(entry.status)")
                    }
                } else {
                    Text("L’activité récente apparaîtra après un calcul nommé.")
                        .font(.system(size: 9)).foregroundColor(Paint.muted)
                }
            }.padding(13).background(card)
            HStack(spacing: 10) {
                Image(systemName: "memorychip")
                Text("Worker \(gib(snapshot?.telemetry?.worker_ram)) / \(gib(snapshot?.telemetry?.worker_ram_limit)) Gio")
                Spacer(minLength: 0)
                Image(systemName: "internaldrive")
                Text("Système \(gib(snapshot?.telemetry?.system_disk_free)) Gio libres")
            }.font(.system(size: 10)).foregroundColor(Paint.muted).padding(.horizontal, 2)
            HStack {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text(freshness(at: context.date)).lineLimit(1)
                }
                Spacer()
                Button(action: { NSWorkspace.shared.open(Configuration.dashboard) }) {
                    Label("Tableau de bord", systemImage: "arrow.up.right.square")
                }.buttonStyle(.plain).accessibilityLabel("Ouvrir le tableau de bord")
            }.font(.system(size: 10)).foregroundColor(monitor.connected ? Paint.muted : Color.orange)
        }
        .padding(16).frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
        .foregroundColor(.white.opacity(0.92))
        .background(LinearGradient(colors: [Paint.panel, Paint.background], startPoint: .topLeading, endPoint: .bottomTrailing))
        .clipShape(RoundedRectangle(cornerRadius: 23))
        .overlay(RoundedRectangle(cornerRadius: 23).stroke(.white.opacity(0.17), lineWidth: 1))
        .preferredColorScheme(.dark)
    }

    private var card: some View { RoundedRectangle(cornerRadius: 13).fill(.black.opacity(0.12)).overlay(RoundedRectangle(cornerRadius: 13).stroke(.white.opacity(0.09))) }
    private func label(_ title: String) -> some View { Text(title).font(.system(size: 9, weight: .bold)).tracking(1.1).foregroundColor(Paint.muted) }
    private var reserveText: String { snapshot.map { "\(Int($0.reserve_bytes / 1048576)) Mio" } ?? "—" }
    private var ramValue: String {
        guard let total = snapshot?.telemetry?.host_total_ram, let free = snapshot?.host_available_ram else { return "—" }
        return "\(gib(max(0, total - free))) / \(gib(total)) Gio"
    }
    private var ramDanger: Double {
        guard let total = snapshot?.telemetry?.host_total_ram, total > 0 else { return 0.75 }
        return max(0, min(1, 1 - 2 * 1073741824 / total))
    }
    private var stateText: String {
        if !monitor.connected { return "Mesures indisponibles" }
        if snapshot?.busy == true { return "Calcul en cours" }
        return snapshot?.admit == true ? "Disponible" : "Ressources insuffisantes"
    }
    private var link: some View {
        GeometryReader { geometry in
            Path { path in path.move(to: CGPoint(x: 0, y: 14)); path.addLine(to: CGPoint(x: geometry.size.width, y: 14)) }
                .stroke(monitor.connected && snapshot?.busy == true ? Paint.green : Paint.muted.opacity(0.3), style: StrokeStyle(lineWidth: 1.2, dash: [4, 4]))
        }.frame(height: 40)
    }
    private func node(_ symbol: String, _ title: String, active: Bool) -> some View {
        VStack(spacing: 4) {
            Image(systemName: symbol).font(.system(size: 19)).frame(width: 44, height: 32)
                .foregroundColor(active ? Paint.green : Paint.muted)
                .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.035)))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(active ? Paint.green.opacity(0.7) : .white.opacity(0.1)))
            Text(title).font(.system(size: 9)).foregroundColor(Paint.muted)
        }
    }
    private func freshness(at date: Date) -> String {
        guard let received = monitor.received else { return monitor.error }
        let age = max(0, Int(date.timeIntervalSince(received)))
        if !monitor.connected { return "Hors ligne · dernière mesure il y a \(age) s" }
        return "Mis à jour il y a \(age) s · SSH \(Int(monitor.latency * 1000)) ms"
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
        let size = host.fittingSize
        panel = WidgetPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "Atelier VPS — VPS et agents"
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
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "usevps")
        statusItem.button?.toolTip = "Atelier VPS — afficher le widget du VPS"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggle)
        panel.orderFrontRegardless()
        monitor.onUpdate = { [weak self] in self?.resizeToContent(); self?.captureIfReady() }
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
        for (title, action) in [("Afficher le widget", #selector(show)), ("Masquer", #selector(hide)), ("Actualiser", #selector(refresh))] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: ""); item.target = self; menu.addItem(item)
        }
        let pinned = NSMenuItem(title: "Toujours au premier plan", action: #selector(pin(_:)), keyEquivalent: "")
        pinned.target = self; pinned.state = panel.level == .floating ? .on : .off; menu.addItem(pinned)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quitter Atelier VPS", action: #selector(quit), keyEquivalent: "q")
        quit.target = self; menu.addItem(quit)
        menu.popUp(positioning: nil, at: NSPoint(x: panel.frame.maxX - 120, y: panel.frame.maxY - 44), in: nil)
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { hide(); return false }
    func applicationWillTerminate(_ notification: Notification) { monitor.stop() }

    private func resizeToContent() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let panel = self?.panel, let view = panel.contentView else { return }
            view.layoutSubtreeIfNeeded()
            let size = view.fittingSize
            guard abs(size.height - panel.frame.height) > 1 else { return }
            var frame = panel.frame
            frame.origin.y += frame.height - size.height
            frame.size = size
            if let screen = panel.screen?.visibleFrame { frame.origin.y = max(screen.minY + 10, frame.origin.y) }
            panel.setFrame(frame, display: true)
        }
    }

    private func captureIfReady() {
        guard !capturing, monitor.successfulSamples >= 2,
              let index = CommandLine.arguments.firstIndex(of: "--capture"), index + 1 < CommandLine.arguments.count else { return }
        capturing = true
        if CommandLine.arguments.contains("--exercise-controls") {
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
                    "connected": monitor.connected, "cpu_fraction": monitor.cpu.host.map { $0 as Any } ?? NSNull(),
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
