import Foundation

struct ServerConnection: Codable, Identifiable, Equatable {
    let id: String
    var name: String
    var team: String
    var ssh_host: String
    var admin_ssh_host: String? = nil
    var managed: Bool? = nil
    var enabled: Bool? = nil
    var scope: String? = nil
    var server_id: String? = nil
    var active: Bool { enabled != false }

    static let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/atelier-vps")
    static func alias(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", options: .regularExpression) != nil
    }
    var valid: Bool {
        guard id.range(of: "^[a-z][a-z0-9-]{0,31}$", options: .regularExpression) != nil,
              Self.alias(ssh_host), admin_ssh_host.map(Self.alias) ?? true else { return false }
        guard [name, team].allSatisfy({ !$0.isEmpty && $0.count <= 64 && $0.rangeOfCharacter(from: .controlCharacters) == nil }) else { return false }
        return (scope.map { $0.range(of: "^[a-z][a-z0-9-]{0,31}$", options: .regularExpression) != nil } ?? true)
            && (server_id.map { $0.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil } ?? true)
    }

    var sshOptions: [String] {
        managed == true ? ["-F", Self.root.appendingPathComponent("connections/\(id)/ssh.conf").path] : []
    }
    func arguments(_ operation: String, administrator: Bool = false) -> [String]? {
        guard valid, let alias = administrator ? admin_ssh_host : ssh_host else { return nil }
        return (administrator ? [] : sshOptions) + ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "-o", "LogLevel=ERROR", alias, operation]
    }

    static func decode(_ data: Data) throws -> [Self] {
        struct Registry: Decodable { let version: Int; let connections: [ServerConnection] }
        guard data.count <= 131072 else { throw MonitorError.invalidData }
        let value = try JSONDecoder().decode(Registry.self, from: data)
        guard value.version == 1, value.connections.count <= 16, value.connections.allSatisfy(\.valid),
              Set(value.connections.map(\.id)).count == value.connections.count,
              Set(value.connections.map(\.ssh_host)).count == value.connections.count else { throw MonitorError.invalidData }
        return value.connections
    }

    static func load(legacy: ServerConnection) throws -> [Self] {
        let url = root.appendingPathComponent("connections.json")
        guard FileManager.default.fileExists(atPath: url.path) else {
            guard legacy.valid else { throw MonitorError.invalidData }
            return [legacy]
        }
        let values = try url.resourceValues(forKeys: [.fileSizeKey, .isSymbolicLinkKey, .isRegularFileKey])
        guard values.isSymbolicLink != true, values.isRegularFile == true,
              (values.fileSize ?? Int.max) <= 131072 else { throw MonitorError.invalidData }
        return try decode(Data(contentsOf: url))
    }
}

struct ServerSample: Identifiable {
    var connection: ServerConnection
    var document: VPSSnapshot? = nil
    var raw = Data()
    var cpu = CPUReading(previous: nil, current: nil)
    var connected = false
    var received: Date? = nil
    var latency: Double = 0
    var successfulSamples = 0
    var id: String { connection.id }
    var identity: String? { document?.server_id ?? connection.server_id }
    var fresh: Bool { connected && (received.map { abs($0.timeIntervalSinceNow) <= 60 } ?? true) }
}

func countedServers(_ samples: [ServerSample]) -> [ServerSample] {
    var seen = Set<String>()
    return samples.filter { sample in
        guard sample.connection.active, sample.fresh, sample.document != nil else { return false }
        guard let identity = sample.identity else { return samples.filter { $0.connection.active }.count == 1 }
        return seen.insert(identity).inserted
    }
}

func aggregateServers(_ samples: [ServerSample]) -> (VPSSnapshot, CPUReading)? {
    let eligible = countedServers(samples)
    guard !eligible.isEmpty else { return nil }
    if eligible.count == 1, let sample = eligible.first, let document = sample.document { return (document.onServer(sample.id), sample.cpu) }
    let documents = eligible.compactMap(\.document)
    let pools = documents.compactMap(\.pool)
    guard pools.count == documents.count else { return nil }
    func sum(_ key: KeyPath<VPSSnapshot, Double>) -> Double { documents.reduce(0) { $0 + $1[keyPath: key] } }
    func metric(_ get: (ServerSample) -> Double?) -> Double? {
        let values = eligible.compactMap(get)
        return values.count == eligible.count ? values.reduce(0, +) : nil
    }
    func activities(_ get: (VPSSnapshot) -> [Activity]) -> [Activity] {
        eligible.flatMap { sample in
            get(sample.document!).map { item in var value = item; value.connection = sample.id; return value }
        }
    }
    let active = activities { $0.agents }
    let history = Array(activities { $0.telemetry?.activity ?? [] }.sorted { $0.started > $1.started }.prefix(8))
    let transfers = Array(activities { $0.transfers }.sorted { $0.started > $1.started }.prefix(16))
    let ramAvailable = metric { $0.document?.pool?.ram_available }
    let used = metric { $0.document?.pool?.ram_accounting?.used_bytes }
    let pool = ResourcePool(cpu_total: pools.reduce(0) { $0 + $1.cpu_total },
        cpu_reserved: pools.reduce(0) { $0 + $1.cpu_reserved }, ram_reserved: pools.reduce(0) { $0 + $1.ram_reserved },
        disk_reserved: pools.reduce(0) { $0 + $1.disk_reserved }, active: active, active_count: active.count,
        max_jobs: pools.reduce(0) { $0 + $1.max_jobs }, ram_available: ramAvailable,
        ram_accounting: RAMAccounting(used_bytes: used), disk_available: documents.reduce(0) { $0 + $1.diskReservable },
        ram_total: metric { $0.document?.pool?.ram_total })
    let telemetry = Telemetry(sample_time: Date().timeIntervalSince1970, arch: nil,
        cpu_count: pools.reduce(0) { $0 + $1.cpu_total }, cpu_total: nil, cpu_idle: nil, host_total_ram: nil,
        worker_ram: metric { $0.document?.telemetry?.worker_ram },
        worker_ram_limit: metric { $0.document?.telemetry?.worker_ram_limit }, worker_cpu_usec: nil,
        system_disk_total: nil, system_disk_free: nil, activity: history, active: active.first,
        worker_cpu_limit: metric { $0.document?.telemetry?.worker_cpu_limit }, transfers: transfers)
    let document = VPSSnapshot(ok: true, busy: documents.contains { $0.busy }, admit: documents.contains { $0.admit },
        budget_bytes: sum(\.budget_bytes), workspace_free_bytes: sum(\.workspace_free_bytes), used_bytes: sum(\.used_bytes),
        free_bytes: sum(\.free_bytes), reserve_bytes: sum(\.reserve_bytes), host_available_ram: sum(\.host_available_ram),
        telemetry: telemetry, pool: pool, maintenance: documents.allSatisfy { $0.maintenance == true })
    let weights = eligible.map { $0.document?.telemetry?.worker_cpu_limit ?? Double($0.document?.pool?.cpu_total ?? 0) }
    let fractions = eligible.map { $0.cpu.worker }
    let total = weights.reduce(0, +)
    let fraction: Double? = total > 0 && fractions.allSatisfy { $0 != nil }
        ? zip(fractions, weights).reduce(0) { $0 + ($1.0 ?? 0) * $1.1 } / total : nil
    return (document, CPUReading(host: nil, worker: fraction))
}

func administratorTarget(_ servers: [ServerSample], selectedID: String?) -> ServerConnection? {
    func eligible(_ sample: ServerSample) -> Bool {
        sample.connection.admin_ssh_host != nil && (sample.fresh || sample.connection.server_id != nil)
            && (servers.count == 1 || sample.identity != nil)
    }
    let sample: ServerSample?
    if let id = selectedID { sample = servers.first { $0.id == id } }
    else { sample = servers.first(where: eligible) }
    guard let sample = sample, eligible(sample) else { return nil }
    var connection = sample.connection; connection.server_id = sample.identity
    return connection
}

func runFleetChecks() throws {
    let first = ServerConnection(id: "paris", name: "Paris", team: "Équipe Paris", ssh_host: "paris-worker")
    precondition(first.valid)
    precondition(Array(first.arguments("status")!.suffix(2)) == ["paris-worker", "status"])
    let data = try JSONEncoder().encode(["not": "a registry"])
    do { _ = try ServerConnection.decode(data); preconditionFailure("malformed registry accepted") } catch {}
    var second = first; second.ssh_host = "-oProxyCommand=invalid"; precondition(!second.valid)
    let raw: [String: Any] = ["ok": true, "busy": false, "admit": true,
        "budget_bytes": 8000, "workspace_free_bytes": 7000, "used_bytes": 1000,
        "free_bytes": 9000, "reserve_bytes": 100, "host_available_ram": 8000,
        "server_id": String(repeating: "a", count: 64),
        "pool": ["cpu_total": 4, "cpu_reserved": 0, "ram_reserved": 0, "disk_reserved": 0,
                 "active": [], "active_count": 0, "max_jobs": 4, "ram_available": 4000,
                 "ram_total": 5000, "disk_available": 6000, "ram_accounting": ["used_bytes": 0]]]
    let snapshot = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: raw))
    var sample = ServerSample(connection: first); sample.document = snapshot; sample.connected = true
    var administrator = sample; administrator.connection.admin_ssh_host = "paris-admin"
    precondition(administratorTarget([sample], selectedID: nil) == nil)
    precondition(administratorTarget([sample, administrator], selectedID: nil)?.admin_ssh_host == "paris-admin")
    var member = ServerSample(connection: ServerConnection(id: "member", name: "Member", team: "Paris", ssh_host: "member-worker"))
    member.document = snapshot; member.connected = true
    precondition(administratorTarget([member, administrator], selectedID: "member") == nil)
    precondition(administratorTarget([member, administrator], selectedID: "missing") == nil)
    precondition(countedServers([sample, sample]).count == 1)
    var stale = sample; stale.connected = false
    precondition(countedServers([stale]).isEmpty)
    stale = sample; stale.received = Date().addingTimeInterval(-90)
    precondition(countedServers([stale]).isEmpty)
    precondition(aggregateServers([sample, sample])?.0.pool?.ram_available == 4000)
    precondition(aggregateServers([sample, stale])?.0.ramUsageFraction == 0)
    var otherRaw = raw
    otherRaw["server_id"] = String(repeating: "b", count: 64)
    var otherPool = raw["pool"] as! [String: Any]
    otherPool["cpu_total"] = 2; otherPool["cpu_reserved"] = 1
    otherPool["ram_available"] = 2000; otherPool["ram_reserved"] = 1000
    otherPool["ram_accounting"] = ["used_bytes": 500]; otherRaw["pool"] = otherPool
    var other = ServerSample(connection: ServerConnection(id: "lyon", name: "Lyon", team: "Équipe Lyon", ssh_host: "lyon-worker"))
    other.document = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: otherRaw))
    other.connected = true; other.cpu = CPUReading(host: nil, worker: 0.75)
    sample.cpu = CPUReading(host: nil, worker: 0.25)
    let combined = aggregateServers([sample, other])!
    precondition(combined.0.pool?.ram_available == 6000 && combined.0.pool?.ram_reserved == 1000)
    precondition(combined.0.diskReservable == 12000 && combined.0.pool?.cpu_total == 6)
    precondition(abs(combined.1.worker! - (2.5 / 6)) < 1e-9)
    precondition(abs(combined.0.ramUsageFraction! - (500.0 / 7000)) < 1e-9)
    other.connected = false
    precondition(aggregateServers([sample, other])?.0.pool?.cpu_total == 4)
    print("Fleet registry, physical-server deduplication and stale capacity checks passed")
}
