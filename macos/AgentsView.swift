import AppKit
import SwiftUI

struct DesktopSession: Codable {
    let available: Bool
    let active: Bool
    var id: String? = nil
    var phase: String? = nil
    var provider: String? = nil
    var started: Double? = nil
    var expires: Double? = nil
    var ram_bytes: Double? = nil
    var disk_bytes: Double? = nil
    var error: String? = nil

    var valid: Bool {
        (!active || id != nil)
            && (id.map { $0.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil } ?? true)
            && (phase.map { ["preparing", "ready", "closed", "failed"].contains($0) } ?? true)
            && (provider.map { ["chatgpt", "claude"].contains($0) } ?? true)
            && [started, expires, ram_bytes, disk_bytes].compactMap { $0 }.allSatisfy { $0.isFinite && $0 >= 0 && $0 < 1e11 }
            && (error.map { $0.count <= 256 && $0.rangeOfCharacter(from: .controlCharacters) == nil } ?? true)
    }
}

final class RemoteAgentsControl: ObservableObject {
    @Published var waiting: ServerConnection?
    @Published var waitingSession: String?
    @Published var opening = false
    @Published var viewing: String?
    @Published var error: String?
    let editor = ConnectionEditor()
    private var viewer: Process?
    private var viewingSession: String?
    private var acknowledgement = Data()

    func disconnect() {
        if let viewer = viewer, viewer.isRunning { viewer.terminate() }
        viewer = nil; viewing = nil; viewingSession = nil; opening = false
    }

    func start(_ connection: ServerConnection, provider: String, refresh: @escaping () -> Void) {
        guard waiting == nil, !editor.busy else { return }
        error = nil; waiting = connection
        editor.save(arguments: ["--server", connection.id, "desktop", "start", "--provider", provider], received: { [weak self] data in
            guard let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = value["id"] as? String, id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else {
                self?.waiting = nil; self?.error = L("Desktop unavailable. Check the available resources and server version."); return
            }
            self?.waitingSession = id
        }, failed: { [weak self] _ in
            self?.waiting = nil; self?.error = L("Desktop unavailable. Check the available resources and server version.")
        }, completion: refresh)
    }

    func stop(_ connection: ServerConnection, session: String, refresh: @escaping () -> Void) {
        if viewing == connection.id, viewingSession == session { disconnect() }
        editor.save(arguments: ["--server", connection.id, "desktop", "stop", "--id", session], failed: { [weak self] _ in
            self?.error = L("Desktop unavailable. Check the available resources and server version.")
        }, completion: refresh)
    }

    func open(_ connection: ServerConnection, session: String) {
        if viewer?.isRunning == true, viewing == connection.id, viewingSession == session {
            NSWorkspace.shared.launchApplication("Screen Sharing"); return
        }
        disconnect()
        let task = Process(), output = Pipe()
        task.executableURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/atelier-vps")
        task.arguments = ["--server", connection.id, "desktop", "open", "--id", session]
        task.standardInput = FileHandle.nullDevice; task.standardOutput = output; task.standardError = FileHandle.nullDevice
        error = nil; opening = true; waiting = nil; waitingSession = nil; viewer = task; acknowledgement = Data()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self, self.viewer === task else { return }
                self.acknowledgement.append(data)
                guard self.acknowledgement.count <= 4096 else { task.terminate(); return }
                guard self.acknowledgement.contains(10),
                      let value = try? JSONSerialization.jsonObject(with: self.acknowledgement) as? [String: Any],
                      value["opened"] as? Bool == true, value["server"] as? String == connection.id,
                      value["id"] as? String == session else { return }
                self.opening = false; self.viewing = connection.id; self.viewingSession = session
                handle.readabilityHandler = nil
            }
        }
        task.terminationHandler = { [weak self] process in
            output.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self = self, self.viewer === process else { return }
                self.opening = false; self.viewing = nil; self.viewingSession = nil; self.viewer = nil
                if process.terminationStatus != 0 {
                    self.error = L("Desktop unavailable. Check the available resources and server version.")
                }
            }
        }
        do { try task.run() }
        catch { viewer = nil; opening = false; self.error = L("Install the repository client before connecting.") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 75) { [weak self, weak task] in
            guard let self = self, let task = task, self.viewer === task, self.opening, task.isRunning else { return }
            task.terminate(); self.opening = false
            self.error = L("Desktop unavailable. Check the available resources and server version.")
        }
    }
}

struct AgentsView: View {
    @ObservedObject var monitor: Monitor
    @ObservedObject var control: RemoteAgentsControl
    @State private var stopping: ServerConnection?
    @State private var stoppingID: String?

    private var ready: String {
        guard let waiting = control.waiting,
              let sample = monitor.servers.first(where: { $0.id == waiting.id && $0.connection.ssh_host == waiting.ssh_host }),
              sample.fresh, sample.document?.desktop?.phase == "ready", sample.document?.desktop?.active == true,
              sample.document?.desktop?.id == control.waitingSession
        else { return "" }
        return sample.document?.desktop?.id ?? ""
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(L("Your programs run on the VPS. Sign in to your account inside the remote desktop."))
                    .font(.system(size: 12)).foregroundColor(Paint.ivory).fixedSize(horizontal: false, vertical: true)
                ForEach(monitor.servers.filter { monitor.selectedID == nil || $0.id == monitor.selectedID }) { sample in
                    server(sample)
                }
                if let error = control.error { Text(error).foregroundColor(.orange).font(.system(size: 11)).fixedSize(horizontal: false, vertical: true) }
                Text(L("Cowork Pro/Max computes in Anthropic's cloud. The VPS desktop connects your VPS files. Local Cowork requires KVM."))
                    .font(.system(size: 10)).foregroundColor(Paint.muted).fixedSize(horizontal: false, vertical: true)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
        }
        .onChange(of: ready) { value in
            if !value.isEmpty, let connection = control.waiting { control.open(connection, session: value) }
        }
        .onReceive(monitor.$servers) { samples in
            guard let waiting = control.waiting, let id = control.waitingSession,
                  let sample = samples.first(where: { $0.id == waiting.id }), sample.fresh,
                  let desktop = sample.document?.desktop, desktop.id == id, !desktop.active else { return }
            control.waiting = nil; control.waitingSession = nil
            control.error = L("Desktop unavailable. Check the available resources and server version.")
        }
        .alert(L("Close the remote desktop?"), isPresented: Binding(get: { stopping != nil }, set: { if !$0 { stopping = nil } })) {
            Button(L("Cancel"), role: .cancel) { stopping = nil }
            Button(L("Close desktop"), role: .destructive) {
                if let connection = stopping, let id = stoppingID { control.stop(connection, session: id) { monitor.refresh() } }
                stopping = nil
            }
        } message: {
            Text(L("Finish its tasks before closing. Your profile and files stay on the VPS."))
        }
    }

    private func server(_ sample: ServerSample) -> some View {
        let desktop = sample.document?.desktop
        return VStack(alignment: .leading, spacing: 9) {
            Text(sample.connection.name + " · " + sample.connection.team).font(.system(size: 12, weight: .semibold)).foregroundColor(Paint.ivory)
            Text("Codex · ChatGPT  /  Claude Desktop · Cowork").font(.system(size: 11)).foregroundColor(Paint.green)
            if desktop?.active == true {
                Text(desktop?.provider == "claude" ? "Claude Desktop / Cowork" : "ChatGPT / Codex")
                    .font(.system(size: 11, weight: .medium)).foregroundColor(Paint.ivory)
            }
            if let expires = desktop?.expires, desktop?.active == true {
                Text(LF("Session until %@", Date(timeIntervalSince1970: expires).formatted(date: .omitted, time: .shortened)))
                    .font(.system(size: 11)).foregroundColor(Paint.muted)
            }
            if let ram = desktop?.ram_bytes, desktop?.active == true {
                Text(LF("Committed: %@ %@ RAM", gib(ram), L("GiB")))
                    .font(.system(size: 11)).foregroundColor(Paint.muted)
            }
            if !sample.fresh {
                Text(L("Offline")).font(.system(size: 11))
            } else if desktop?.available != true {
                Text(L("Install the agents update on this server.")).font(.system(size: 11)).foregroundColor(Paint.muted)
            } else if desktop?.active == true {
                if desktop?.phase == "ready", let id = desktop?.id {
                    HStack {
                        Button(L("Open remote desktop")) { control.open(sample.connection, session: id) }
                            .disabled(control.opening).accessibilityIdentifier("agents-open-" + sample.id)
                        Spacer()
                        Button(L("Close")) { stoppingID = id; stopping = sample.connection }
                    }
                } else {
                    HStack {
                        ProgressView().controlSize(.small); Text(L("Preparing the VPS programs…"))
                        Spacer()
                        if let id = desktop?.id {
                            Button(L("Cancel")) { stoppingID = id; stopping = sample.connection }
                        }
                    }.font(.system(size: 11))
                }
            } else {
                Text(L("Start a one-hour desktop")).font(.system(size: 11))
                HStack {
                    Button("ChatGPT / Codex") { control.start(sample.connection, provider: "chatgpt") { monitor.refresh() } }
                        .accessibilityIdentifier("agents-chatgpt-" + sample.id)
                    Button("Claude / Cowork") { control.start(sample.connection, provider: "claude") { monitor.refresh() } }
                        .accessibilityIdentifier("agents-claude-" + sample.id)
                }.disabled(control.waiting != nil || sample.document?.maintenance == true)
                Text(L("Uses a reserved share of the pool. Profiles and files persist; managed tools expire after 30 unused days."))
                    .font(.system(size: 10)).foregroundColor(Paint.muted).fixedSize(horizontal: false, vertical: true)
            }
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 13).fill(.black.opacity(0.12)))
    }
}

func runAgentChecks() throws {
    let idle = try JSONDecoder().decode(DesktopSession.self, from: Data(#"{"available":true,"active":false}"#.utf8))
    precondition(idle.valid)
    let missingIdentity = DesktopSession(available: true, active: true, phase: "ready")
    precondition(!missingIdentity.valid)
    let invalid = DesktopSession(available: true, active: false, id: "../other", expires: -1)
    precondition(!invalid.valid)
    let ready = DesktopSession(available: true, active: true, id: String(repeating: "a", count: 32), phase: "ready", expires: 1792947719)
    precondition(ready.valid)
}
