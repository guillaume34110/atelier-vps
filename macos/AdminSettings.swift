import Foundation
import SwiftUI
import Darwin

struct AdminPool: Codable, Equatable {
    var cpus: Double
    var ram_bytes: Int
    var disk_bytes: Int
    var slots: Int

    var valid: Bool {
        cpus.isFinite && cpus >= 0.01 && cpus <= 4096 && ram_bytes > 0 && disk_bytes > 0 && (1...16).contains(slots)
    }
}

struct AdminUser: Codable, Equatable, Identifiable {
    var id: String
    var enabled: Bool?
    var public_key: String?
    var cpu_percent: Double
    var ram_percent: Double
    var disk_percent: Double
    var slots: Int
    var active: Bool { enabled != false }
}

struct AdminPolicy: Codable, Equatable {
    var mode: String
    var pool: AdminPool
    var users: [AdminUser]

    func validate() throws {
        guard ["opportunistic", "allocated"].contains(mode), pool.valid, (1...4).contains(users.count),
              Set(users.map(\.id)).count == users.count else { throw MonitorError.invalidData }
        for user in users {
            guard user.id.range(of: "^[a-z][a-z0-9-]{0,15}$", options: .regularExpression) != nil,
                  !["root", "admin", "usevps"].contains(user.id), (user.public_key?.utf8.count ?? 0) <= 256,
                  [user.cpu_percent, user.ram_percent, user.disk_percent].allSatisfy({ $0.isFinite && $0 > 0 && $0 <= 100 }),
                  (1...4).contains(user.slots) else { throw MonitorError.invalidData }
        }
    }

    mutating func selectMode(_ value: String, capacity: AdminPool, allocatableRAM: Int?) {
        mode = value
        if value == "opportunistic", let owner = users.firstIndex(where: { $0.id == "owner" }) {
            pool.cpus = capacity.cpus; pool.ram_bytes = capacity.ram_bytes; pool.slots = min(4, capacity.slots)
            for index in users.indices { users[index].enabled = index == owner }
            users[owner].cpu_percent = 100; users[owner].ram_percent = 100; users[owner].slots = pool.slots
            // Disabled members keep their data and disk quota.
        } else if value == "allocated", let available = allocatableRAM, available >= 256 * 1024 * 1024 {
            pool.ram_bytes = min(pool.ram_bytes, available / (64 * 1024 * 1024) * (64 * 1024 * 1024))
        }
    }
}

struct AdminMemberMetrics: Decodable, Identifiable {
    let id: String
    let connected: Bool
    let enabled: Bool
    let ram_limit: Double
    let disk_limit: Double
    let ram_used: Double?
    let ram_reserved: Double?
    let cpu_reserved: Double?
    let active: Int?
    let disk_used: Double?
}

struct AdminState: Decodable {
    struct Pending: Decodable { let policy: AdminPolicy? }
    let revision: String
    let policy: AdminPolicy
    let capacity: AdminPool
    let pending: Pending?
    let host_ram_reserve_bytes: Int?
    let allocatable_ram_bytes: Int?
    let server_id: String?
    let overview: [AdminMemberMetrics]?

    static func decode(_ data: Data) throws -> AdminState {
        guard data.count <= 65536 else { throw MonitorError.invalidData }
        let state = try JSONDecoder().decode(Self.self, from: data)
        guard validRevision(state.revision), state.capacity.valid,
              state.host_ram_reserve_bytes.map({ $0 >= 0 }) ?? true,
              state.allocatable_ram_bytes.map({ $0 >= 0 }) ?? true,
              state.server_id.map(validRevision) ?? true else { throw MonitorError.invalidData }
        try state.policy.validate(); try state.pending?.policy?.validate()
        if let metrics = state.overview {
            guard metrics.count <= 4, Set(metrics.map(\.id)).count == metrics.count else { throw MonitorError.invalidData }
            for item in metrics {
                guard state.policy.users.contains(where: { $0.id == item.id }),
                      [item.ram_limit, item.disk_limit, item.ram_used ?? 0, item.ram_reserved ?? 0,
                       item.cpu_reserved ?? 0, item.disk_used ?? 0].allSatisfy({ $0.isFinite && $0 >= 0 }),
                      (0...4).contains(item.active ?? 0) else { throw MonitorError.invalidData }
            }
        }
        return state
    }
}

struct AdminPreview: Decodable {
    let policy: AdminPolicy
    let revision: String
    let candidate_revision: String
    let affected: [String]

    static func decode(_ data: Data, policy: AdminPolicy, revision: String) throws -> AdminPreview {
        guard data.count <= 65536 else { throw MonitorError.invalidData }
        let plan = try JSONDecoder().decode(Self.self, from: data)
        guard plan.policy == policy, plan.revision == revision, validRevision(plan.candidate_revision),
              plan.affected.count <= 4, plan.affected.allSatisfy({ name in policy.users.contains { $0.id == name } })
        else { throw MonitorError.invalidData }
        return plan
    }
}

private func validRevision(_ value: String) -> Bool {
    value.count == 64 && value.allSatisfy({ "0123456789abcdef".contains($0) })
}

private struct AdminRequest: Encodable {
    let action: String
    var policy: AdminPolicy? = nil
    var expected_revision: String? = nil
    var expected_server_id: String? = nil
}

private struct AdminFailure: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = String(message.prefix(1024)) }
}

final class Administration: ObservableObject {
    @Published var state: AdminState?
    @Published var draft = AdminPolicy(mode: "opportunistic", pool: AdminPool(cpus: 4, ram_bytes: 5 * 1073741824, disk_bytes: 1073741824, slots: 4), users: [])
    @Published var plan: AdminPreview?
    @Published var busy = false
    @Published var applying = false
    @Published var showingManagement = false
    @Published var error: String?
    @Published var notice: String?
    var onApplied: (() -> Void)?
    var onUpdate: (() -> Void)?
    var authorized: Bool { state != nil }
    var canApply: Bool { !busy && plan?.policy == draft && plan?.revision == state?.revision }
    private var host: String?
    private(set) var connectionID: String?
    private var expectedServerID: String?
    private let invitationEditor = ConnectionEditor()

    init(host: String?) {
        self.host = host.flatMap { value in
            value.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", options: .regularExpression) != nil ? value : nil
        }
    }

    @discardableResult func configure(_ connection: ServerConnection?) -> Bool {
        guard !busy else { return false }
        let target = connection.flatMap { $0.valid ? $0 : nil }
        guard connectionID != target?.id || host != target?.admin_ssh_host || expectedServerID != target?.server_id else { return false }
        connectionID = target?.id; host = target?.admin_ssh_host
        expectedServerID = target?.server_id
        state = nil; plan = nil; error = nil; notice = nil; showingManagement = false
        return true
    }

    func load() {
        guard host != nil else { return }
        perform(AdminRequest(action: "get")) { [weak self] data in
            guard let self = self else { return }
            let next = try AdminState.decode(data)
            guard self.expectedServerID == next.server_id else { throw MonitorError.invalidData }
            if let identity = next.server_id { self.expectedServerID = identity }
            self.state = next; self.draft = next.pending?.policy ?? next.policy; self.plan = nil
            if next.pending != nil { self.notice = L("An unfinished update must be resumed with the same settings.") }
        }
    }

    func selectMode(_ mode: String) {
        guard !busy, let state = state else { return }
        draft.selectMode(mode, capacity: state.capacity, allocatableRAM: state.allocatable_ram_bytes)
        plan = nil
    }

    func addMember() {
        guard !busy, let state = state, draft.users.count < 4,
              draft.users.filter(\.active).count < draft.pool.slots else { return }
        draft.selectMode("allocated", capacity: state.capacity, allocatableRAM: state.allocatable_ram_bytes)
        var number = draft.users.count
        while draft.users.contains(where: { $0.id == "member-\(number)" }) { number += 1 }
        // This random public placeholder has no corresponding private key; access starts with an invitation.
        let bytes = [UInt8]([0, 0, 0, 11]) + Array("ssh-ed25519".utf8) + [0, 0, 0, 32] + (0..<32).map { _ in UInt8.random(in: 0...255) }
        draft.users.append(AdminUser(id: "member-\(number)", enabled: true,
            public_key: "ssh-ed25519 " + Data(bytes).base64EncodedString(), cpu_percent: 1, ram_percent: 1, disk_percent: 1, slots: 1))
        let active = draft.users.indices.filter { draft.users[$0].active }
        let disk = 100 - draft.users.filter { !$0.active }.reduce(0) { $0 + $1.disk_percent }
        let share = floor(10000 / Double(active.count)) / 100
        for index in active {
            draft.users[index].cpu_percent = share; draft.users[index].ram_percent = share
            draft.users[index].disk_percent = floor(disk * 100 / Double(active.count)) / 100
            let cpuSlots = max(1, Int(ceil(draft.pool.cpus * share / 100)))
            draft.users[index].slots = min(4, cpuSlots, max(1, draft.pool.slots / active.count))
        }
        plan = nil; notice = L("Adjust the shares, preview and apply, then copy the member’s invitation.")
    }

    func copyInvitation(member: String) {
        guard !busy, let id = connectionID, let state = state, draft == state.policy,
              state.pending == nil, state.policy.users.contains(where: { $0.id == member && $0.active }) else { return }
        busy = true; error = nil
        invitationEditor.save(arguments: ["--server", id, "invitations", "create", "--member", member,
            "--expected-revision", state.revision, "--clipboard"], failed: { [weak self] message in
                self?.busy = false; self?.error = message
            }, completion: { [weak self] in
                self?.busy = false; self?.notice = L("Invitation copied. Valid for 30 minutes and one device. Share it privately.")
            })
    }

    func preview() {
        guard let state = state else { return }
        let candidate = draft
        do { try candidate.validate() }
        catch { self.error = L("Check member IDs, percentages and pool values."); return }
        perform(AdminRequest(action: "preview", policy: candidate, expected_server_id: expectedServerID)) { [weak self] data in
            guard let self = self, self.draft == candidate else { return }
            self.plan = try AdminPreview.decode(data, policy: candidate, revision: state.revision)
        }
    }

    func apply() {
        guard canApply, let plan = plan else { return }
        applying = true
        perform(AdminRequest(action: "apply", policy: draft, expected_revision: plan.revision, expected_server_id: expectedServerID)) { [weak self] data in
            struct Applied: Decodable { let applied: Bool; let revision: String }
            let result = try JSONDecoder().decode(Applied.self, from: data)
            guard result.applied, result.revision == plan.candidate_revision else { throw MonitorError.invalidData }
            self?.notice = L("Settings applied."); self?.onApplied?()
            DispatchQueue.main.async { self?.load() }
        }
    }

    private func perform(_ request: AdminRequest, accept: @escaping (Data) throws -> Void) {
        guard !busy, let host = host else { applying = false; return }
        busy = true; error = nil
        let task = Process(), output = Pipe(), errors = Pipe(), input = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                          "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR", host, "settings"]
        task.standardInput = input; task.standardOutput = output; task.standardError = errors
        do {
            var payload = try JSONEncoder().encode(request); payload.append(10)
            guard payload.count <= 65536 else { throw MonitorError.invalidData }
            try task.run()
            let timeout = request.action == "apply" ? 1500.0 : 30.0
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
                if task.isRunning { task.terminate() }
            }
            DispatchQueue.global(qos: .utility).async {
                func bounded(_ handle: FileHandle, limit: Int) -> Data {
                    var data = Data()
                    while let part = try? handle.read(upToCount: 4096), !part.isEmpty {
                        data.append(part)
                        if data.count > limit { if task.isRunning { task.terminate() }; break }
                    }
                    return data
                }
                try? input.fileHandleForWriting.write(contentsOf: payload)
                try? input.fileHandleForWriting.close()
                let data = bounded(output.fileHandleForReading, limit: 65536)
                let failure = bounded(errors.fileHandleForReading, limit: 8192)
                task.waitUntilExit()
                DispatchQueue.main.async { [weak self] in
                    guard let self = self else { return }
                    self.busy = false; self.applying = false
                    do {
                        guard task.terminationStatus == 0, data.count <= 65536, failure.count <= 8192 else {
                            let details = (try? JSONSerialization.jsonObject(with: failure)) as? [String: Any]
                            throw AdminFailure(details?["error"] as? String ?? L("Administrator SSH unavailable. Refresh before retrying."))
                        }
                        try accept(data)
                    } catch {
                        self.error = error is MonitorError || error is DecodingError ? L("Invalid administrator response. Refresh before retrying.") : error.localizedDescription
                    }
                    self.onUpdate?()
                }
            }
        } catch {
            busy = false; applying = false; self.error = L("Administrator SSH unavailable. Refresh before retrying.")
        }
    }
}

func runAdminChecks() throws {
    let pool = AdminPool(cpus: 4, ram_bytes: 5 * 1073741824, disk_bytes: 26 * 1073741824, slots: 4)
    var policy = AdminPolicy(mode: "allocated", pool: pool, users: [
        AdminUser(id: "owner", cpu_percent: 50, ram_percent: 50, disk_percent: 50, slots: 2),
        AdminUser(id: "alice", public_key: "ssh-ed25519 public-fixture", cpu_percent: 50, ram_percent: 50, disk_percent: 50, slots: 2)])
    try policy.validate()
    var teamPolicy = policy
    let teamCapacity = AdminPool(cpus: 6, ram_bytes: 48 * 1073741824, disk_bytes: 76 * 1073741824, slots: 6)
    teamPolicy.pool = teamCapacity
    teamPolicy.users[0].slots = 3; teamPolicy.users[1].slots = 3
    try teamPolicy.validate()
    let teamState = try AdminState.decode(JSONSerialization.data(withJSONObject: [
        "revision": String(repeating: "a", count: 64),
        "policy": try JSONSerialization.jsonObject(with: JSONEncoder().encode(teamPolicy)),
        "capacity": try JSONSerialization.jsonObject(with: JSONEncoder().encode(teamCapacity))]))
    precondition(teamState.capacity.slots == 6 && teamState.policy.users.map(\.slots) == [3, 3])
    teamPolicy.selectMode("opportunistic", capacity: teamCapacity, allocatableRAM: nil)
    precondition(teamPolicy.pool.slots == 4 && teamPolicy.users[0].slots == 4)
    try teamPolicy.validate()
    policy.selectMode("opportunistic", capacity: pool, allocatableRAM: 3 * 1073741824)
    precondition(policy.users[0].cpu_percent == 100 && policy.users[0].slots == 4 && !policy.users[1].active)
    precondition(policy.users[1].disk_percent == 50 && policy.users[0].disk_percent == 50)
    policy.selectMode("allocated", capacity: pool, allocatableRAM: 3 * 1073741824 + 1)
    precondition(policy.pool.ram_bytes == 3 * 1073741824 && policy.users[0].active)
    policy.users[1].id = "root"
    do { try policy.validate(); preconditionFailure("accepted reserved developer ID") } catch MonitorError.invalidData { }
    do { _ = try AdminState.decode(Data(repeating: 32, count: 65537)); preconditionFailure("accepted oversized settings") } catch MonitorError.invalidData { }
    precondition(!Administration(host: nil).authorized && !Administration(host: "-oProxyCommand=bad").authorized)
    let revision = String(repeating: "a", count: 64)
    policy.users[1].id = "alice"
    let policyJSON = try JSONSerialization.jsonObject(with: JSONEncoder().encode(policy))
    let state = try AdminState.decode(JSONSerialization.data(withJSONObject: ["revision": revision,
        "policy": policyJSON, "capacity": try JSONSerialization.jsonObject(with: JSONEncoder().encode(pool))]))
    let preview = try AdminPreview.decode(JSONSerialization.data(withJSONObject: ["policy": policyJSON,
        "revision": revision, "candidate_revision": revision, "affected": []]), policy: policy, revision: revision)
    let admin = Administration(host: "valid-alias")
    admin.state = state; admin.draft = policy; admin.plan = preview
    precondition(admin.authorized && admin.canApply)
    admin.draft.pool.cpus = 2
    precondition(!admin.canApply)
    do {
        _ = try AdminPreview.decode(JSONSerialization.data(withJSONObject: ["policy": policyJSON,
            "revision": revision, "candidate_revision": revision, "affected": []]), policy: policy,
            revision: String(repeating: "b", count: 64))
        preconditionFailure("accepted stale preview")
    } catch MonitorError.invalidData { }
    admin.state = state; admin.draft = state.policy
    let retainedDisk = admin.draft.users[1].disk_percent
    admin.addMember()
    try admin.draft.validate()
    precondition(admin.draft.mode == "allocated" && admin.draft.users.count == 3)
    precondition(admin.draft.users[1].disk_percent == retainedDisk && !admin.draft.users[1].active)
    precondition(admin.draft.users.filter(\.active).reduce(0) { $0 + $1.cpu_percent } <= 100)
    let placeholder = admin.draft.users.last!.public_key!.split(separator: " ").last!
    precondition(Data(base64Encoded: String(placeholder))?.count == 51)
    precondition(admin.configure(ServerConnection(id: "paris", name: "Paris", team: "Paris", ssh_host: "paris-worker", admin_ssh_host: "paris-admin")))
    precondition(!admin.authorized && !admin.canApply)
    admin.state = state
    var invalidTarget = ServerConnection(id: "lyon", name: "Lyon", team: "Lyon", ssh_host: "lyon-worker")
    invalidTarget.admin_ssh_host = "-oProxyCommand=invalid"
    precondition(admin.configure(invalidTarget) && !admin.authorized && admin.connectionID == nil)
    print("Mac administrator: solo/team transitions, retained disk, RAM rounding, identity and response bounds passed")
}
