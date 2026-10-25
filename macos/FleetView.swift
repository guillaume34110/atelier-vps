import AppKit
import SwiftUI

struct FleetPicker: View {
    @ObservedObject var monitor: Monitor
    @ObservedObject var admin: Administration
    @State private var adding = false

    var body: some View {
        HStack {
            Picker(L("Server"), selection: Binding(get: { monitor.selectedID ?? "" }, set: { monitor.select($0.isEmpty ? nil : $0) })) {
                Text(L("Global")).tag("")
                ForEach(monitor.servers) { sample in Text(sample.connection.name + " · " + sample.connection.team).tag(sample.id) }
            }.labelsHidden().disabled(admin.busy).accessibilityIdentifier("server-selection")
            Button { adding = true } label: { Image(systemName: "plus") }
                .help(L("Add a server")).accessibilityLabel(L("Add a server")).disabled(admin.busy)
        }.font(.system(size: 11))
        .onChange(of: monitor.selectedID) { _ in configureAdministrator() }
        .sheet(isPresented: $adding) { ConnectionView { monitor.reloadConnections() } }
    }

    private func configureAdministrator() {
        if admin.configure(monitor.administratorConnection) { admin.load() }
    }
}

struct FleetServerList: View {
    @ObservedObject var monitor: Monitor
    @ObservedObject var admin: Administration

    var body: some View {
        Collapsible("Servers", key: "global.servers") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(monitor.servers) { sample in serverRow(sample) }
                Text(L("Global adds connected servers. Each task runs on one server."))
                    .font(.system(size: 10)).foregroundColor(Paint.muted)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func serverRow(_ sample: ServerSample) -> some View {
        let counted = countedServers(monitor.servers).contains { $0.id == sample.id }
        return VStack(alignment: .leading, spacing: 5) {
            HStack {
                Circle().fill(counted ? Paint.green : .orange).frame(width: 6, height: 6)
                Button(sample.connection.name) { monitor.select(sample.id) }.buttonStyle(.plain).fontWeight(.semibold)
                Text(sample.connection.team).foregroundColor(Paint.muted).lineLimit(1)
                Spacer(minLength: 2)
                if sample.connection.admin_ssh_host != nil {
                    Button(L("Manage")) {
                        monitor.select(sample.id)
                        if admin.configure(monitor.administratorConnection) { admin.load() }
                        admin.showingManagement = true
                    }.disabled(admin.busy || (!sample.connected && !admin.authorized))
                }
            }
            if counted, let document = sample.document, let pool = document.pool {
                Text(LF("CPU %@ · RAM %@ %@ · %d agents", percent(sample.cpu.worker), gib(pool.ram_available), L("GiB"), document.connectedAgents.count))
                    .monospacedDigit().foregroundColor(Paint.green)
                Text(LF("%d / %d tasks · %@ %@ storage available", document.agents.count, pool.max_jobs, gib(document.diskReservable), L("GiB")))
                    .monospacedDigit().foregroundColor(Paint.muted)
            } else {
                Text(L(!sample.connection.active ? "Disabled" : !sample.fresh ? "Offline" : sample.identity == nil ? "Update this server to include it in Global" : "Already counted in Global"))
                    .foregroundColor(Paint.muted)
            }
        }.font(.system(size: 11))
    }
}

final class ConnectionEditor: ObservableObject {
    @Published var busy = false
    @Published var error: String?

    func save(arguments: [String], input: Data = Data(), failed: ((String) -> Void)? = nil, completion: @escaping () -> Void) {
        guard !busy else { return }
        let launcher = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/atelier-vps")
        guard FileManager.default.isExecutableFile(atPath: launcher.path), input.count <= 8193 else {
            let message = L("Install the repository client before connecting.")
            error = message; failed?(message); return
        }
        busy = true; error = nil
        let task = Process(); task.executableURL = launcher; task.arguments = arguments
        DispatchQueue.global(qos: .utility).async {
            let result = Result { try boundedPrivateOutput(task, input: input, limit: 65536, timeout: 120) }
            DispatchQueue.main.async { [weak self] in
                self?.busy = false
                switch result {
                case .success: completion()
                case .failure:
                    let message = L("Connection failed. Check the invitation or SSH alias and retry. Existing connections were preserved.")
                    self?.error = message; failed?(message)
                }
            }
        }
    }
}

struct ConnectionView: View {
    let onSaved: () -> Void
    @StateObject private var editor = ConnectionEditor()
    @State private var link = ""
    @State private var advanced = false
    @State private var identifier = ""
    @State private var name = ""
    @State private var team = ""
    @State private var alias = ""
    @State private var administrator = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("Add a server")).font(.headline).foregroundColor(Paint.ivory)
            Text(L("Paste the private invitation provided by your administrator.")).font(.system(size: 12))
            SecureField(L("Invitation link"), text: $link).accessibilityIdentifier("invitation-link")
            Button(L("Paste invitation")) {
                if let value = NSPasteboard.general.string(forType: .string), value.utf8.count <= 8192 { link = value }
            }
            DisclosureGroup(L("Use an existing SSH alias"), isExpanded: $advanced) {
                VStack(spacing: 8) {
                    TextField(L("Short ID"), text: $identifier)
                    TextField(L("Server name"), text: $name)
                    TextField(L("Team"), text: $team)
                    TextField(L("SSH alias"), text: $alias)
                    TextField(L("Administrator alias (optional)"), text: $administrator)
                }.padding(.top, 8)
            }
            if let error = editor.error { Text(error).font(.system(size: 11)).foregroundColor(.orange) }
            HStack {
                if editor.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button(L("Cancel")) { dismiss() }.disabled(editor.busy)
                Button(L("Connect"), action: connect).disabled(editor.busy || (advanced ? !validAlias : link.isEmpty))
                    .keyboardShortcut(.defaultAction)
            }
        }.padding(20).frame(minWidth: 320, idealWidth: 440, maxWidth: 540)
            .background(Paint.panel).preferredColorScheme(.dark).interactiveDismissDisabled(editor.busy)
    }

    private var validAlias: Bool {
        ServerConnection(id: identifier, name: name, team: team, ssh_host: alias,
                         admin_ssh_host: administrator.isEmpty ? nil : administrator).valid
    }
    private func connect() {
        let completion = { link = ""; onSaved(); dismiss() }
        if advanced {
            var arguments = ["servers", "add", "--id", identifier, "--name", name, "--team", team, "--ssh-host", alias]
            if !administrator.isEmpty { arguments += ["--admin-host", administrator] }
            editor.save(arguments: arguments, completion: completion)
        } else {
            editor.save(arguments: ["connect", "--stdin"], input: Data((link + "\n").utf8), completion: completion)
        }
    }
}
