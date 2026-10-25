import AppKit
import SwiftUI
import Darwin

enum Configuration {
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
    static let adminHost = values["admin_ssh_host"]
    static let dashboard: URL = {
        if let value = values["coolify_url"], let url = URL(string: value), url.scheme == "https" { return url }
        return URL(string: "https://github.com/guillaume34110/atelier-vps")!
    }()
}

enum Paint {
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
    @Published var compact = false
    @Published var servers: [ServerSample] = []
    @Published var selectedID: String?
    var onUpdate: (() -> Void)?
    var successfulSamples = 0
    var raw = Data()
    private var process: Process?
    private var timer: Timer?
    private var visible = true
    private var queue: [String] = []
    private var retryCount: [String: Int] = [:]
    private var nextSample: [String: Date] = [:]

    init() {
        selectedID = CommandLine.arguments.contains("--capture") ? nil : UserDefaults.standard.string(forKey: "atelier-server-selection")
        reloadConnections(start: false)
    }

    func reloadConnections(start: Bool = true) {
        do {
            let legacy = ServerConnection(id: "default", name: Configuration.values["server_name"] ?? "Atelier VPS",
                team: Configuration.values["team_name"] ?? "Personnel", ssh_host: Configuration.host,
                admin_ssh_host: Configuration.adminHost)
            let connections = try ServerConnection.load(legacy: legacy)
            servers = connections.map { connection in
                if var previous = servers.first(where: { $0.id == connection.id && $0.connection.ssh_host == connection.ssh_host
                    && $0.connection.server_id == connection.server_id && $0.connection.scope == connection.scope }) {
                    previous.connection = connection; return previous
                }
                return ServerSample(connection: connection)
            }
            if let id = selectedID, !servers.contains(where: { $0.id == id }) { selectedID = nil }
        } catch { self.error = "Connection configuration invalid" }
        recompute()
        if start { refresh() }
    }

    func select(_ id: String?) {
        guard id == nil || servers.contains(where: { $0.id == id }) else { return }
        if selectedID != id { document = nil; raw = Data(); received = nil }
        selectedID = id
        if !CommandLine.arguments.contains("--capture") { UserDefaults.standard.set(id, forKey: "atelier-server-selection") }
        recompute(); onUpdate?()
    }

    func target(for activity: Activity) -> ServerConnection? {
        if let id = activity.connection ?? selectedID { return servers.first(where: { $0.id == id })?.connection }
        let counted = countedServers(servers)
        return counted.count == 1 ? counted.first?.connection : nil
    }

    var administratorConnection: ServerConnection? {
        let sample = selectedID.flatMap { id in servers.first { $0.id == id } }
            ?? (servers.count == 1 ? servers.first : nil)
        guard let sample = sample, sample.connection.admin_ssh_host != nil,
              sample.fresh || sample.connection.server_id != nil,
              servers.count == 1 || sample.identity != nil else { return nil }
        var value = sample.connection; value.server_id = sample.identity
        return value
    }

    func setVisible(_ value: Bool) {
        visible = value; timer?.invalidate()
        if value { recompute(); refresh() } else { queue.removeAll() }
    }

    func refresh() {
        guard process == nil else { return }
        timer?.invalidate()
        queue = servers.filter { $0.connection.active }.map(\.id)
        sampleNext()
    }

    private func sampleNext() {
        guard process == nil, visible else { return }
        guard !queue.isEmpty else { refreshing = false; schedule(); return }
        let id = queue.removeFirst()
        guard let connection = servers.first(where: { $0.id == id })?.connection,
              let arguments = connection.arguments("status") else { sampleNext(); return }
        refreshing = true
        let task = Process(), output = Pipe(), errors = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = arguments
        task.standardInput = FileHandle.nullDevice; task.standardOutput = output; task.standardError = errors
        process = task
        let started = ProcessInfo.processInfo.systemUptime
        do { try task.run() }
        catch { process = nil; finish(nil, started: started, connection: connection); return }
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
                self?.finish(result, started: started, connection: connection)
            }
        }
    }

    private func finish(_ data: Data?, started: Double, connection: ServerConnection) {
        if let index = servers.firstIndex(where: { $0.id == connection.id }) {
            do {
                guard let data = data else { throw MonitorError.connection }
                let next = try VPSSnapshot.decode(data)
                let current = servers[index].connection
                guard current.ssh_host == connection.ssh_host,
                      current.server_id.map({ $0 == next.server_id }) ?? true,
                      current.scope.map({ $0 == next.scope?.id }) ?? true else { throw MonitorError.invalidData }
                servers[index].cpu = CPUReading(previous: servers[index].document?.telemetry, current: next.telemetry)
                servers[index].document = next; servers[index].raw = data
                servers[index].received = Date(); servers[index].connected = true
                servers[index].latency = ProcessInfo.processInfo.systemUptime - started
                servers[index].successfulSamples += 1; retryCount[connection.id] = 0
                let active = !next.connectedAgents.isEmpty || servers[index].successfulSamples < 2
                nextSample[connection.id] = Date().addingTimeInterval(active ? 5 : 30)
            } catch {
                servers[index].connected = false
                let failures = (retryCount[connection.id] ?? 0) + 1; retryCount[connection.id] = failures
                let delay = min(60.0, 5 * pow(2.0, Double(min(failures - 1, 4))))
                nextSample[connection.id] = Date().addingTimeInterval(delay)
            }
        }
        recompute(); onUpdate?()
        if visible { sampleNext() } else { refreshing = false }
    }

    private func recompute() {
        if let id = selectedID, let sample = servers.first(where: { $0.id == id }) {
            if let snapshot = sample.document { document = snapshot.onServer(sample.id); raw = sample.raw }
            connected = sample.fresh && sample.connection.active
            cpu = connected ? sample.cpu : CPUReading(previous: nil, current: nil)
            received = sample.received; latency = sample.latency; successfulSamples = sample.successfulSamples
        } else if let result = aggregateServers(servers) {
            let counted = countedServers(servers)
            document = result.0; cpu = result.1; connected = true
            received = counted.compactMap(\.received).min()
            latency = counted.reduce(0) { $0 + $1.latency } / Double(counted.count)
            successfulSamples = counted.map(\.successfulSamples).min() ?? 0
            raw = counted.count == 1 ? counted[0].raw : (try? JSONEncoder().encode(result.0)) ?? Data()
        } else {
            document = nil; raw = Data(); received = nil
            connected = false; cpu = CPUReading(previous: nil, current: nil); successfulSamples = 0
        }
        error = connected ? "" : "SSH unavailable · retrying automatically"
    }

    private func schedule() {
        guard visible else { return }
        let active = servers.filter { $0.connection.active }
        guard !active.isEmpty else { return }
        let due = active.map { nextSample[$0.id] ?? Date() }.min() ?? Date()
        let delay = max(1, min(60, due.timeIntervalSinceNow))
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self = self, self.process == nil else { return }
            self.queue = self.servers.filter { $0.connection.active && (self.nextSample[$0.id] ?? .distantPast) <= Date() }.map(\.id)
            self.sampleNext()
        }
        timer?.tolerance = min(1, delay * 0.1)
    }

    func stop() {
        visible = false; queue.removeAll(); timer?.invalidate()
        if process?.isRunning == true { process?.terminate() }
        // A shared SSH control master can also serve other agents.
    }
}

private struct GaugeFace: View, Animatable {
    private static let artwork: NSImage = {
        let image = NSImage(contentsOf: Bundle.main.url(forResource: "meter-face", withExtension: "png")!)!
        image.isTemplate = false
        return image
    }()
    var level: Double
    var known: Bool
    var animatableData: Double {
        get { level }
        set { level = newValue }
    }

    var body: some View {
        Image(nsImage: Self.artwork).resizable().interpolation(.high)
            .aspectRatio(16.0 / 9.0, contentMode: .fit)
            .overlay(needle)
            .opacity(known ? 1 : 0.55)
    }

    private var needle: some View {
        Canvas { context, size in
            // Match the fixed 320×180 artwork; never scale its axes independently.
            let scale = size.width / 320
            let pivot = CGPoint(x: 160 * scale, y: 168 * scale)
            let radius = 147.2 * scale
            if known {
                let angle: Double = (200 + 140 * max(0, min(1, level))) * .pi / 180
                let tip = CGPoint(x: pivot.x + CGFloat(cos(angle)) * radius * 0.86,
                                  y: pivot.y + CGFloat(sin(angle)) * radius * 0.86)
                var needle = Path()
                needle.move(to: pivot)
                needle.addLine(to: tip)
                var shadow = context
                shadow.addFilter(.shadow(color: .black.opacity(0.5), radius: 1.5 * scale, x: scale, y: 2 * scale))
                shadow.stroke(needle, with: .color(Paint.orange), style: StrokeStyle(lineWidth: 2.7 * scale, lineCap: .round))
            }
            let cap = Path(ellipseIn: CGRect(x: pivot.x - 7 * scale, y: pivot.y - 7 * scale, width: 14 * scale, height: 14 * scale))
            let gradient = Gradient(colors: [Color.gray, Color.black])
            let capEnd = CGPoint(x: pivot.x + 7 * scale, y: pivot.y + 7 * scale)
            context.fill(cap, with: .linearGradient(gradient, startPoint: pivot, endPoint: capEnd))
            context.stroke(cap, with: .color(.black.opacity(0.8)), lineWidth: 1.2 * scale)
        }
    }
}

private struct Dial: View {
    let title: String
    let value: String
    let fraction: Double?
    let detail: String
    var compact = false

    var body: some View {
        VStack(spacing: 2) {
            GaugeFace(level: fraction ?? 0, known: fraction != nil)
                .frame(height: compact ? 100 : 124)
                .animation(.easeInOut(duration: 0.8), value: fraction)
            Text(title).font(.system(size: 11, weight: .bold)).tracking(1.2)
            Text(value).font(.system(size: compact ? 18 : 20, weight: .semibold, design: .rounded)).monospacedDigit()
            Text(detail).font(.system(size: 9, design: .monospaced)).foregroundColor(Paint.muted).lineLimit(1)
        }
        .padding(.horizontal, 8).padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 14).fill(LinearGradient(colors: [Paint.panel, Paint.background], startPoint: .topLeading, endPoint: .bottomTrailing)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.white.opacity(0.12), lineWidth: 1))
    }
}

struct Collapsible<Content: View>: View {
    let title: String
    let key: String
    let content: Content
    @State private var expanded: Bool

    init(_ title: String, key: String, @ViewBuilder content: () -> Content) {
        self.title = title; self.key = key; self.content = content()
        _expanded = State(initialValue: CommandLine.arguments.contains("--capture") ? true : UserDefaults.standard.object(forKey: key) as? Bool ?? true)
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
    let layout: WidgetLayout
    @ObservedObject var monitor: Monitor
    @ObservedObject var admin: Administration
    @ObservedObject var failures: FailureDetails
    let hide: () -> Void
    let menu: () -> Void
    let compact: () -> Void
    @AppStorage("atelier-language") private var language = "system"
    private var snapshot: VPSSnapshot? { monitor.document }
    private var tint: Color { monitor.connected ? Paint.green : .orange }
    private var profile: String { (monitor.selectedID ?? "global") + "." + (snapshot?.scope?.id ?? "owner") }
    private var unit: String { L("GiB") }

    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "gauge.with.dots.needle.67percent").foregroundColor(Paint.ivory)
                Text("Atelier VPS").font(.system(size: 20, weight: .bold, design: .rounded)).lineLimit(1).minimumScaleFactor(0.9)
                Spacer(minLength: 4)
                Circle().fill(tint).frame(width: 7, height: 7).help(L(monitor.connected ? "Connected" : "Offline"))
                    .accessibilityLabel(L(monitor.connected ? "Connected" : "Offline"))
                Button(action: compact) { Image(systemName: "rectangle.compress.vertical") }
                    .help(L("Mini bar")).accessibilityLabel(L("Mini bar")).accessibilityIdentifier("widget-mini")
                Button(action: { monitor.refresh() }) { Image(systemName: "arrow.clockwise") }
                    .help(L("Refresh")).accessibilityLabel(L("Refresh"))
                Button(action: menu) { Image(systemName: "ellipsis") }.help(L("Options")).accessibilityLabel(L("Options"))
                Button(action: hide) { Image(systemName: "chevron.down") }.help(L("Hide widget")).accessibilityLabel(L("Hide widget"))
            }.buttonStyle(.plain)
            HStack(spacing: 0) { Color(red: 0.20, green: 0.36, blue: 0.72); Paint.ivory; Paint.orange }
                .frame(height: 2).clipShape(Capsule())
            FleetPicker(monitor: monitor, admin: admin)
            if admin.authorized {
                Picker(L("Widget section"), selection: $admin.showingManagement) {
                    Text(L("Monitoring")).tag(false)
                    Text(L("Management")).tag(true)
                }.pickerStyle(.segmented).labelsHidden().accessibilityIdentifier("widget-section")
            }
            if admin.authorized && admin.showingManagement {
                AdminView(admin: admin)
            } else {
            HStack {
                Text(L(snapshot?.scope?.mode == "allocated" ? "Your allocation" : "Available capacity"))
                Spacer()
                Text(L(!monitor.connected ? "Offline" : (snapshot?.maintenance == true ? "Maintenance" : (snapshot?.busy == true ? "Working" : (snapshot?.admit == true ? "Ready" : "Waiting")))))
            }.font(.system(size: 10)).foregroundColor(Paint.muted)
            ScrollView {
                VStack(spacing: 10) {
                    Collapsible("Resources", key: profile + ".resources") {
                        HStack(spacing: 8) {
                            Dial(title: L("YOUR CPU"), value: percent(monitor.cpu.worker), fraction: monitor.cpu.worker,
                                 detail: LF("Limit %.2f vCPU", snapshot?.telemetry?.worker_cpu_limit ?? Double(snapshot?.pool?.cpu_total ?? 0)), compact: layout == .compact)
                            Dial(title: L("RESERVABLE RAM"), value: gib(snapshot?.pool?.ram_available) + " " + unit,
                                 fraction: snapshot?.ramUsageFraction,
                                 detail: LF("Task use %@", percent(snapshot?.ramUsageFraction)), compact: layout == .compact)
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
                    if monitor.selectedID == nil { FleetServerList(monitor: monitor, admin: admin) }
                    if layout == .wide {
                        HStack(alignment: .top, spacing: 10) {
                            agentsSection.frame(maxWidth: .infinity)
                            VStack(spacing: 10) { transfersSection; historySection }.frame(maxWidth: .infinity)
                        }
                    } else {
                        agentsSection
                        transfersSection
                        historySection
                    }
                }.id(profile)
            }
            }
            HStack {
                Text(freshness).lineLimit(1)
                Spacer(minLength: 4)
                Button(action: { NSWorkspace.shared.open(Configuration.dashboard) }) {
                    Image(systemName: "arrow.up.right.square")
                }.buttonStyle(.plain).help(L("Dashboard")).accessibilityLabel(L("Dashboard"))
            }.font(.system(size: 10)).foregroundColor(monitor.connected ? Paint.muted : .orange)
        }
        .padding(14).frame(minWidth: 340, idealWidth: 480, maxWidth: .infinity, minHeight: 220, idealHeight: 640, maxHeight: .infinity)
        .foregroundColor(.white.opacity(0.92))
        .background(LinearGradient(colors: [Paint.panel, Paint.background], startPoint: .topLeading, endPoint: .bottomTrailing))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(.white.opacity(0.17), lineWidth: 1))
        .preferredColorScheme(.dark).environment(\.locale, Locale(identifier: UILanguage.code)).id(language)
        .sheet(item: $failures.selected) { activity in FailureDetailView(activity: activity, details: failures) }
    }
    private var agentsSection: some View {
        Collapsible("Connected agents", key: profile + ".tasks") {
            VStack(alignment: .leading, spacing: 8) {
                Text(LF("%d connected · %d / %d tasks", snapshot?.connectedAgents.count ?? 0,
                        snapshot?.agents.count ?? 0, snapshot?.pool?.max_jobs ?? 4)).foregroundColor(Paint.muted)
                    .help(L("An agent is connected while running a task or transferring files."))
                if monitor.connected, let agents = snapshot?.connectedAgents, !agents.isEmpty {
                    ForEach(agents, id: \.uiID) { agent in
                        HStack(alignment: .top, spacing: 7) {
                            Circle().fill(agent.orphaned == true ? .orange : Paint.green).frame(width: 7, height: 7).padding(.top, 3)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(agent.agent).fontWeight(.semibold).lineLimit(2).help(agent.agent)
                                Text(agent.project).foregroundColor(Paint.muted).lineLimit(1)
                                if agent.ram_bytes != nil {
                                    Text(LF("Reserved: %@ %@ RAM", gib(agent.ram_bytes), unit)).foregroundColor(Paint.muted)
                                } else { Text(L("Transferring files")).foregroundColor(Paint.muted) }
                                if layout == .compact { Text(agent.clockRange).foregroundColor(Paint.muted).monospacedDigit() }
                            }
                            Spacer(minLength: 2)
                            Text(agent.elapsed(at: Date())).monospacedDigit()
                        }.font(.system(size: 11)).padding(.vertical, 3)
                    }
                } else { Text(L(monitor.connected ? "No connected agent" : "Task status unavailable")).foregroundColor(Paint.muted) }
            }.frame(maxWidth: .infinity, alignment: .leading).font(.system(size: 11))
        }
    }
    private var transfersSection: some View {
        Collapsible("Transfers", key: profile + ".transfers") {
            VStack(alignment: .leading, spacing: 8) {
                if let transfers = snapshot?.transfers, !transfers.isEmpty {
                    ForEach(Array((transfers.filter { $0.status == "running" } + transfers.filter { $0.status != "running" }).prefix(4)), id: \.uiID) { transfer in
                        HStack(alignment: .top, spacing: 7) {
                            Image(systemName: transfer.direction == "upload" ? "arrow.up.right" : "arrow.down.left")
                                .foregroundColor(transfer.status == "failed" ? .orange : Paint.green)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(transfer.agent).lineLimit(2).help(transfer.agent)
                                Text(L(transfer.direction == "upload" ? "To VPS" : "From VPS") + " · " + transferBytes(transfer.bytes))
                                    .foregroundColor(Paint.muted)
                                Text(transfer.clockRange).font(.system(size: 9)).monospacedDigit().foregroundColor(Paint.muted)
                            }
                            Spacer(minLength: 2)
                            VStack(alignment: .trailing, spacing: 3) {
                                Text(L(transfer.status == "running" ? "In progress" : transfer.status == "passed" ? "Completed" : "Failed"))
                                Text(transferBytes(transfer.transferRate) + "/s").monospacedDigit().help(L("Average transfer speed"))
                            }.font(.system(size: 9)).foregroundColor(Paint.muted)
                        }.font(.system(size: 11))
                    }
                } else { Text(L(snapshot?.telemetry?.transfers == nil ? "Transfers available after worker update" : "No transfer yet")).foregroundColor(Paint.muted) }
            }.frame(maxWidth: .infinity, alignment: .leading).font(.system(size: 11))
                .opacity(monitor.connected ? 1 : 0.45)
        }
    }
    private var historySection: some View {
        Collapsible("History", key: profile + ".history") {
            VStack(alignment: .leading, spacing: 8) {
                if let recent = snapshot?.recent, !recent.isEmpty {
                    ForEach(Array(recent.prefix(3)), id: \.uiID) { entry in
                        if entry.status == "failed" {
                            Button { failures.open(entry) } label: { historyRow(entry) }
                                .buttonStyle(.plain).help(L("Show failure details"))
                                .accessibilityIdentifier("failure-" + entry.id)
                                .accessibilityLabel(LF("Failure details for %@", entry.agent))
                        } else { historyRow(entry) }
                    }
                } else { Text(L("Completed tasks appear here")).font(.system(size: 11)) }
            }.foregroundColor(Paint.muted).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
    private func historyRow(_ entry: Activity) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: entry.status == "passed" ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .foregroundColor(entry.status == "passed" ? Paint.green : .orange)
                Text(entry.agent).lineLimit(layout == .standard ? 1 : 2).help(entry.agent)
                Spacer(minLength: 2)
                Text(entry.elapsed(at: Date())).monospacedDigit()
            }
            Text(entry.clockRange + " · " + L(entry.status == "passed" ? "Completed" : "Failed"))
                .font(.system(size: 9)).monospacedDigit().fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 10)).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
    }
    private var freshness: String {
        guard let received = monitor.received else { return L(monitor.error) }
        let timestamp = received.formatted(.dateTime.hour().minute().second().locale(Locale(identifier: UILanguage.code)))
        return monitor.connected ? LF("Updated %@ · SSH %d ms", timestamp, Int(monitor.latency * 1000)) : LF("Offline · last sample %@", timestamp)
    }
}

private struct MiniWidget: View {
    @ObservedObject var monitor: Monitor
    let expand: () -> Void
    let menu: () -> Void
    @AppStorage("atelier-language") private var language = "system"

    private var snapshot: VPSSnapshot? { monitor.document }
    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(monitor.connected ? Paint.green : .orange).frame(width: 6, height: 6)
                    .accessibilityLabel(L(monitor.connected ? "Connected" : "Offline"))
                Text("Atelier VPS").font(.system(size: 11, weight: .semibold))
                Text(L(snapshot?.scope?.mode == "allocated" ? "Team" : "Solo"))
                    .font(.system(size: 9)).foregroundColor(Paint.ivory)
                Spacer(minLength: 2)
                Text(LF("Disk free: %@ %@", gib(snapshot?.workspace_free_bytes), L("GiB")))
                    .font(.system(size: 9)).foregroundColor(Paint.muted).lineLimit(1)
                Button(action: expand) { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                    .help(L("Expand widget")).accessibilityLabel(L("Expand widget"))
                    .accessibilityIdentifier("widget-expand")
                Button(action: menu) { Image(systemName: "ellipsis") }
                    .help(L("Options")).accessibilityLabel(L("Options"))
            }.buttonStyle(.plain)
            HStack(spacing: 10) {
                metric("CPU", percent(monitor.cpu.worker), monitor.cpu.worker)
                metric(L("Reservable"), gib(snapshot?.pool?.ram_available) + " " + L("GiB"), snapshot?.ramUsageFraction)
                metric(L("Committed"), gib(snapshot?.pool?.ram_reserved) + " " + L("GiB"), nil)
                metric(L("Tasks"), "\(snapshot?.agents.count ?? 0)/\(snapshot?.pool?.max_jobs ?? 4)", Double(snapshot?.agents.count ?? 0) / Double(snapshot?.pool?.max_jobs ?? 4))
            }.opacity(monitor.connected ? 1 : 0.45)
            HStack(spacing: 4) {
                Text(LF("%d agents", snapshot?.connectedAgents.count ?? 0))
                Spacer(minLength: 2)
                Image(systemName: "arrow.up.right")
                Text("\(snapshot?.activeTransfers.filter { $0.direction == "upload" }.count ?? 0)")
                Image(systemName: "arrow.down.left")
                Text("\(snapshot?.activeTransfers.filter { $0.direction == "download" }.count ?? 0)")
            }.font(.system(size: 9)).monospacedDigit().foregroundColor(Paint.muted)
                .opacity(monitor.connected ? 1 : 0.45)
            if !monitor.connected || snapshot?.maintenance == true {
                Text(L(monitor.connected ? "Maintenance" : "Offline · values may be stale"))
                    .font(.system(size: 9)).foregroundColor(.orange)
            }
        }
        .padding(10).frame(maxWidth: .infinity, maxHeight: .infinity)
        .foregroundColor(.white.opacity(0.92)).background(Paint.background)
        .overlay(alignment: .top) {
            HStack(spacing: 0) { Color(red: 0.20, green: 0.36, blue: 0.72); Paint.ivory; Paint.orange }
                .frame(width: 30, height: 2)
        }
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Paint.green.opacity(0.3), lineWidth: 1))
        .preferredColorScheme(.dark).environment(\.locale, Locale(identifier: UILanguage.code)).id(language)
    }

    private func metric(_ title: String, _ value: String, _ fraction: Double?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 9)).foregroundColor(Paint.muted).lineLimit(1)
            Text(value).font(.system(size: 12, weight: .semibold, design: .rounded))
                .monospacedDigit().lineLimit(1).minimumScaleFactor(0.85)
            ProgressView(value: min(1, max(0, fraction ?? 0))).tint(Paint.green)
                .controlSize(.mini).opacity(fraction == nil ? 0 : 1)
        }.frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

private struct WidgetRoot: View {
    @ObservedObject var monitor: Monitor
    @ObservedObject var admin: Administration
    @ObservedObject var failures: FailureDetails
    let hide: () -> Void
    let menu: () -> Void
    let compact: () -> Void

    var body: some View {
        GeometryReader { geometry in
            let layout = WidgetLayout.resolve(width: geometry.size.width, height: geometry.size.height)
            if layout == .mini {
                MiniWidget(monitor: monitor, expand: compact, menu: menu)
            } else {
                WidgetView(layout: layout, monitor: monitor, admin: admin, failures: failures, hide: hide, menu: menu, compact: compact)
            }
        }
    }
}

private final class WidgetPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class WidgetDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let monitor = Monitor()
    let administration = Administration(host: Configuration.adminHost)
    let failures = FailureDetails()
    private var panel: WidgetPanel!
    private var statusItem: NSStatusItem!
    private var capturing = false
    private var fullFrame: NSRect?
    private var adminCheckStage = 0
    private var adminCheckPolicy: AdminPolicy?
    private var adminCheckApplied = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let root = WidgetRoot(monitor: monitor, admin: administration, failures: failures, hide: { [weak self] in self?.hide() },
                              menu: { [weak self] in self?.showMenu() }, compact: { [weak self] in self?.toggleCompact() })
        let host = NSHostingView(rootView: root)
        // Window bounds own resizing; automatic hosting minima retain the full view in mini mode.
        host.sizingOptions = []
        let size = NSSize(width: 480, height: 670)
        panel = WidgetPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel, .resizable], backing: .buffered, defer: false)
        panel.minSize = NSSize(width: 340, height: 96)
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
        if let screen = panel.screen?.visibleFrame {
            var frame = panel.frame
            frame.size.width = min(900, max(360, frame.width), screen.width)
            frame.size.height = min(1200, max(240, frame.height), screen.height)
            frame.origin.x = min(max(screen.minX, frame.minX), screen.maxX - frame.width)
            frame.origin.y = min(max(screen.minY, frame.minY), screen.maxY - frame.height)
            panel.setFrame(frame, display: false)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "usevps")
        statusItem.button?.toolTip = L("Show widget")
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggle)
        if UserDefaults.standard.bool(forKey: "atelier-mini"), !CommandLine.arguments.contains("--capture") { setCompact(true) }
        panel.orderFrontRegardless()
        monitor.onUpdate = { [weak self] in
            guard let self = self else { return }
            if self.administration.configure(self.monitor.administratorConnection) { self.administration.load() }
            self.captureIfReady()
        }
        failures.onUpdate = { [weak self] in self?.captureIfReady() }
        failures.resolveConnection = { [weak self] activity in self?.monitor.target(for: activity) }
        administration.onUpdate = { [weak self] in self?.captureIfReady() }
        administration.onApplied = { [weak self] in self?.adminCheckApplied = true; self?.monitor.refresh() }
        monitor.refresh()
        administration.configure(monitor.administratorConnection)
        administration.load()
        if CommandLine.arguments.contains("--capture") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 45) { [weak self] in
                if self?.capturing != true { fputs("Live widget capture did not receive two real SSH samples\n", stderr); exit(1) }
            }
        }
    }

    private func toggleCompact() { setCompact(!monitor.compact) }

    private func setCompact(_ value: Bool) {
        guard monitor.compact != value else { return }
        if value {
            fullFrame = panel.frame
            monitor.compact = true
            panel.setContentSize(NSSize(width: 400, height: 96))
        } else {
            monitor.compact = false
            if let frame = fullFrame { panel.setFrame(frame, display: true) }
            else { panel.setContentSize(NSSize(width: 480, height: 670)) }
        }
        if !CommandLine.arguments.contains("--capture") { UserDefaults.standard.set(value, forKey: "atelier-mini") }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if administration.applying {
            administration.notice = L("Applying settings… Keep the widget open.")
            return .terminateCancel
        }
        return .terminateNow
    }

    @objc func toggle() { if panel.isVisible { hide() } else { show() } }
    @objc func show() { panel.orderFrontRegardless(); monitor.setVisible(true) }
    @objc func hide() { panel.orderOut(nil); monitor.setVisible(false) }
    @objc func refresh() { monitor.refresh() }
    @objc func quit() {
        if administration.applying { NSApp.terminate(nil); return }
        failures.selected = nil
        if let sheet = panel.attachedSheet { panel.endSheet(sheet); sheet.orderOut(nil) }
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }
    @objc func pin(_ sender: NSMenuItem) {
        panel.level = panel.level == .floating ? .normal : .floating
        sender.state = panel.level == .floating ? .on : .off
    }
    @objc func miniMenu() { toggleCompact() }
    @objc func reloadAdmin() { administration.load() }
    @objc func showMenu() {
        let menu = NSMenu()
        let mini = NSMenuItem(title: L("Mini bar layout"), action: #selector(miniMenu), keyEquivalent: "m")
        mini.target = self; mini.state = monitor.compact ? .on : .off; menu.addItem(mini)
        if Configuration.adminHost != nil {
            let admin = NSMenuItem(title: L("Administrator access"), action: #selector(reloadAdmin), keyEquivalent: "")
            admin.target = self; admin.isEnabled = !administration.busy; menu.addItem(admin)
        }
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
    func windowDidResize(_ notification: Notification) {
        guard let window = notification.object as? NSWindow, window === panel else { return }
        var frame = window.frame
        frame.size.width = min(window.maxSize.width, max(window.minSize.width, frame.width))
        frame.size.height = min(window.maxSize.height, max(window.minSize.height, frame.height))
        if frame != window.frame { window.setFrame(frame, display: true) }
        monitor.compact = WidgetLayout.resolve(width: frame.width, height: frame.height) == .mini
        if !CommandLine.arguments.contains("--capture") {
            UserDefaults.standard.set(monitor.compact, forKey: "atelier-mini")
            if !monitor.compact {
                if frame.height >= 320 { fullFrame = frame }
                window.saveFrame(usingName: "usevps-widget")
            }
        }
    }
    func windowDidMove(_ notification: Notification) {
        if !monitor.compact && !CommandLine.arguments.contains("--capture") { panel.saveFrame(usingName: "usevps-widget") }
    }
    func applicationWillTerminate(_ notification: Notification) { monitor.stop(); failures.cancel() }

    @objc private func setLanguage(_ sender: NSMenuItem) {
        guard let code = sender.representedObject as? String, ["system", "fr", "en"].contains(code) else { return }
        UserDefaults.standard.set(code, forKey: "atelier-language")
        statusItem.button?.toolTip = L("Show widget")
    }

    private func captureIfReady() {
        guard !capturing, monitor.successfulSamples >= 2,
              let index = CommandLine.arguments.firstIndex(of: "--capture"), index + 1 < CommandLine.arguments.count else { return }
        if CommandLine.arguments.contains("--capture-failure") {
            captureFailure(index: index)
            return
        }
        if CommandLine.arguments.contains("--capture-layouts"), Configuration.adminHost != nil,
           administration.busy || !administration.authorized { return }
        if CommandLine.arguments.contains("--exercise-admin") {
            guard administration.authorized, !administration.busy, let state = administration.state else { return }
            if adminCheckStage == 0 {
                adminCheckPolicy = state.policy
                administration.draft = state.policy
                adminCheckStage = 1; administration.preview(); return
            }
            if adminCheckStage == 1 {
                guard let plan = administration.plan, plan.affected.isEmpty, plan.policy == adminCheckPolicy else {
                    fputs("Administrator no-op preview failed; no changes applied\n", stderr); exit(1)
                }
                adminCheckStage = 2; administration.apply(); return
            }
            if adminCheckStage == 2 {
                guard adminCheckApplied else { fputs("Administrator no-op application failed\n", stderr); exit(1) }
                adminCheckStage = 3; administration.load(); return
            }
            guard state.policy == adminCheckPolicy else { fputs("Administrator no-op changed the policy\n", stderr); exit(1) }
        }
        capturing = true
        panel.setContentSize(NSSize(width: 480, height: 640))
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
            let original = panel.frame
            setCompact(true)
            guard monitor.compact && panel.frame.height == 96 else {
                fputs("Mini resize failed: expected 96, actual \(panel.frame)\n", stderr); exit(1)
            }
            setCompact(false)
            guard !monitor.compact && panel.frame == original else {
                fputs("Full frame restoration failed: expected \(original), actual \(panel.frame)\n", stderr); exit(1)
            }
            for (width, height, expected) in [(439.0, 640.0, WidgetLayout.compact), (440, 640, .standard),
                                              (699, 640, .standard), (700, 640, .wide),
                                              (480, 219, .mini), (480, 220, .standard)] {
                panel.setContentSize(NSSize(width: width, height: height))
                precondition(WidgetLayout.resolve(width: panel.frame.width, height: panel.frame.height) == expected)
                precondition(monitor.compact == (expected == .mini))
            }
            panel.setFrame(original, display: true)
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
                let facePixels = meterFacePixels(bitmap)
                guard facePixels >= 1000 else { throw MonitorError.invalidData }
                guard let png = bitmap.representation(using: .png, properties: [:]) else { throw MonitorError.invalidData }
                try png.write(to: path, options: .withoutOverwriting)
                let evidence: [String: Any] = ["source": "real restricted SSH", "samples": monitor.successfulSamples,
                    "connected": monitor.connected, "cpu_fraction": monitor.cpu.worker.map { $0 as Any } ?? NSNull(),
                    "ram_fraction": monitor.document?.ramUsageFraction.map { $0 as Any } ?? NSNull(),
                    "meter_face_pixels": facePixels,
                    "bundle": Bundle.main.bundlePath, "window_width": view.bounds.width, "window_height": view.bounds.height,
                    "controls_exercised": CommandLine.arguments.contains("--exercise-controls"),
                    "automatic_breakpoints_exercised": CommandLine.arguments.contains("--exercise-controls"),
                    "connected_agents": monitor.document?.connectedAgents.count ?? 0,
                    "active_transfers": monitor.document?.activeTransfers.count ?? 0,
                    "history_times": monitor.document?.recent.prefix(3).map(\.clockRange) ?? [],
                    "administrator_authenticated": administration.authorized,
                    "administrator_roundtrip_verified": adminCheckStage == 3 && adminCheckApplied,
                    "status": try JSONSerialization.jsonObject(with: monitor.raw)]
                try JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys, .prettyPrinted])
                    .write(to: path.deletingPathExtension().appendingPathExtension("json"), options: .withoutOverwriting)
                print("Real native widget capture saved: \(path.path)")
                if CommandLine.arguments.contains("--capture-layouts") { captureLayouts(path) }
                else if CommandLine.arguments.contains("--exit-after-capture") { quit() }
            } catch { fputs("Widget capture failed: \(error)\n", stderr); exit(1) }
        }
    }

    private func meterFacePixels(_ bitmap: NSBitmapImageRep) -> Int {
        var count = 0
        for y in stride(from: 0, to: bitmap.pixelsHigh, by: 2) {
            for x in stride(from: 0, to: bitmap.pixelsWide, by: 2) {
                if let c = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB),
                   c.alphaComponent > 0.5, c.redComponent > 0.8, c.greenComponent > 0.7,
                   c.blueComponent > 0.45, c.blueComponent < 0.86, c.redComponent - c.blueComponent > 0.08 {
                    count += 1
                }
            }
        }
        return count
    }

    private func captureFailure(index: Int) {
        if failures.selected == nil {
            let arguments = CommandLine.arguments
            let expected = arguments.firstIndex(of: "--failure-id").flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
            guard let activity = monitor.document?.recent.first(where: { $0.status == "failed" && (expected == nil || $0.id == expected) }) else {
                fputs("No real failed activity available for the failure-card scenario\n", stderr); exit(1)
            }
            failures.open(activity)
            return
        }
        guard !failures.loading, let report = failures.report else {
            if failures.error != nil { fputs("Real failure details could not be verified\n", stderr); exit(1) }
            return
        }
        capturing = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in
            do {
                let path = URL(fileURLWithPath: CommandLine.arguments[index + 1])
                guard let workspace = ProcessInfo.processInfo.environment["TASK_WORKSPACE"],
                      path.standardizedFileURL.path.hasPrefix(URL(fileURLWithPath: workspace).standardizedFileURL.path + "/"),
                      !FileManager.default.fileExists(atPath: path.path),
                      let view = panel.attachedSheet?.contentView,
                      let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw MonitorError.invalidData }
                view.cacheDisplay(in: view.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { throw MonitorError.invalidData }
                try png.write(to: path, options: .withoutOverwriting)
                let receipt: [String: Any] = ["source": "real restricted SSH", "failure_id": report.id,
                    "failure_kind": report.kind, "sha256_verified": true, "samples": monitor.successfulSamples,
                    "window_width": view.bounds.width, "window_height": view.bounds.height,
                    "resources": report.resources]
                try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
                    .write(to: path.deletingPathExtension().appendingPathExtension("json"), options: .withoutOverwriting)
                print("Real authenticated failure sheet captured")
                if CommandLine.arguments.contains("--exit-after-capture") { quit() }
            } catch { fputs("Failure sheet capture failed: \(error)\n", stderr); exit(1) }
        }
    }

    private func captureLayouts(_ path: URL) {
        var layouts: [(String, Double, Double, Bool, Bool)] = [
            ("narrow", 360, 600, false, false), ("standard", 480, 640, false, false),
            ("wide", 720, 800, false, false), ("short", 360, 300, false, false),
            ("mini-small", 340, 96, true, false), ("mini", 400, 96, true, false),
            ("mini-wide", 600, 96, true, false),
            ("compact-edge", 439, 760, false, false), ("standard-edge", 440, 760, false, false),
            ("column-before", 699, 800, false, false), ("column-after", 700, 800, false, false),
            ("auto-mini", 480, 219, true, false), ("auto-full", 480, 220, false, false)]
        if administration.authorized {
            layouts += [("admin-narrow", 360, 620, false, true), ("admin", 480, 740, false, true),
                        ("admin-wide", 720, 800, false, true)]
        }
        let originalFrame = panel.frame, originalPolicy = administration.draft
        var evidence: [[String: Any]] = []
        func next(_ index: Int) {
            guard index < layouts.count else {
                do {
                    try JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys, .prettyPrinted])
                        .write(to: path.deletingLastPathComponent().appendingPathComponent("widget-layouts.json"), options: .withoutOverwriting)
                    setCompact(false); panel.setFrame(originalFrame, display: true)
                    administration.showingManagement = false; administration.draft = originalPolicy
                    if CommandLine.arguments.contains("--exit-after-capture") { quit() }
                } catch { fputs("Layout receipt failed: \(error)\n", stderr); exit(1) }
                return
            }
            let (name, width, height, mini, admin) = layouts[index]
            administration.showingManagement = admin
            if admin { administration.selectMode("allocated") }
            panel.setContentSize(NSSize(width: width, height: height))
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [self] in
                do {
                    guard let view = panel.contentView else { throw MonitorError.invalidData }
                    view.layoutSubtreeIfNeeded()
                    guard view.bounds.width == width && view.bounds.height == height && monitor.compact == mini else {
                        fputs("Layout \(name) expected \(width)×\(height), actual \(view.bounds)\n", stderr); exit(1)
                    }
                    guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw MonitorError.invalidData }
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    let facePixels = meterFacePixels(bitmap)
                    if !mini && !admin && height >= 500 { precondition(facePixels >= 1000) }
                    guard let png = bitmap.representation(using: .png, properties: [:]) else { throw MonitorError.invalidData }
                    let image = path.deletingLastPathComponent().appendingPathComponent("widget-\(name).png")
                    try png.write(to: image, options: .withoutOverwriting)
                    evidence.append(["layout": name, "width": width, "height": height, "image": image.lastPathComponent,
                                     "mini": mini, "management": admin, "administrator_authenticated": administration.authorized,
                                     "resolved_layout": WidgetLayout.resolve(width: width, height: height).rawValue,
                                     "meter_face_pixels": facePixels,
                                     "server_mode": monitor.document?.scope?.mode ?? "unknown", "settings_applied": false])
                    next(index + 1)
                } catch { fputs("Layout capture failed: \(error)\n", stderr); exit(1) }
            }
        }
        next(0)
    }
}

@main
struct WidgetMain {
    static func main() {
        if CommandLine.arguments.contains("--self-test") {
            do { try runModelChecks(); try runFleetChecks(); try runAdminChecks(); try runFailureChecks() } catch { fputs("Mac model checks failed: \(error)\n", stderr); exit(1) }
            return
        }
        let application = NSApplication.shared
        let delegate = WidgetDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
