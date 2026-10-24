import Foundation

struct Activity: Decodable, Identifiable {
    let id: String
    let agent: String
    let project: String
    let status: String
    let started: Double
    let ended: Double?
    var cpus: Int? = nil
    var ram_bytes: Double? = nil
    var disk_bytes: Double? = nil
    var orphaned: Bool? = nil

    func elapsed(at date: Date) -> String {
        let seconds = Int(max(0, min(86400, (ended ?? date.timeIntervalSince1970) - started)))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}

struct RAMAccounting: Decodable {
    let used_bytes: Double?
}

struct ResourcePool: Decodable {
    let cpu_total: Int
    let cpu_reserved: Int
    let ram_reserved: Double
    let disk_reserved: Double
    let active: [Activity]
    let active_count: Int
    let max_jobs: Int
    var ram_available: Double? = nil
    var ram_accounting: RAMAccounting? = nil
}

struct Telemetry: Decodable {
    let sample_time: Double
    let arch: String?
    let cpu_count: Int?
    let cpu_total: Double?
    let cpu_idle: Double?
    let host_total_ram: Double?
    let worker_ram: Double?
    let worker_ram_limit: Double?
    let worker_cpu_usec: Double?
    let system_disk_total: Double?
    let system_disk_free: Double?
    let activity: [Activity]
    let active: Activity?
    var worker_cpu_limit: Double? = nil
}

struct UserScope: Decodable {
    let id: String
    let mode: String
}

struct VPSSnapshot: Decodable {
    let ok: Bool
    let busy: Bool
    let admit: Bool
    let budget_bytes: Double
    let workspace_free_bytes: Double
    let used_bytes: Double
    let free_bytes: Double
    let reserve_bytes: Double
    let host_available_ram: Double
    let telemetry: Telemetry?
    let pool: ResourcePool?
    var scope: UserScope? = nil
    var maintenance: Bool? = nil

    static func decode(_ data: Data) throws -> VPSSnapshot {
        guard data.count <= 65536 else { throw MonitorError.invalidData }
        let value = try JSONDecoder().decode(VPSSnapshot.self, from: data)
        let numbers = [value.budget_bytes, value.workspace_free_bytes, value.used_bytes,
                       value.free_bytes, value.reserve_bytes, value.host_available_ram]
        guard value.ok, value.budget_bytes > 0,
              numbers.allSatisfy({ $0.isFinite && $0 >= 0 }),
              (value.telemetry?.activity.count ?? 0) <= 8 else { throw MonitorError.invalidData }
        if let t = value.telemetry {
            let metrics = [t.cpu_total, t.cpu_idle, t.host_total_ram, t.worker_ram,
                           t.worker_ram_limit, t.worker_cpu_usec, t.system_disk_total, t.system_disk_free, t.worker_cpu_limit]
            guard t.sample_time.isFinite && t.sample_time >= 0,
                  metrics.compactMap({ $0 }).allSatisfy({ $0.isFinite && $0 >= 0 }),
                  t.cpu_count.map({ 1...4096 ~= $0 }) ?? true else { throw MonitorError.invalidData }
            for record in t.activity + (t.active.map { [$0] } ?? []) + (value.pool?.active ?? []) {
                guard record.id.count == 32, record.id.allSatisfy({ $0.isHexDigit }),
                      record.agent.count <= 64, record.project.count <= 64,
                      ["running", "passed", "failed"].contains(record.status),
                      record.started.isFinite && record.started >= 0 && record.started < 1e11,
                      record.ended.map({ $0.isFinite && $0 >= 0 && $0 < 1e11 }) ?? true else { throw MonitorError.invalidData }
            }
        }
        if let pool = value.pool {
            guard pool.active.count <= 4, pool.active_count == pool.active.count,
                  (1...4096).contains(pool.cpu_total), (0...pool.cpu_total).contains(pool.cpu_reserved),
                  pool.ram_reserved.isFinite && pool.ram_reserved >= 0,
                  pool.disk_reserved.isFinite && pool.disk_reserved >= 0,
                  pool.active.allSatisfy({ ($0.cpus ?? 0) > 0 && ($0.cpus ?? 0) <= pool.cpu_total
                      && ($0.ram_bytes ?? -1).isFinite && ($0.ram_bytes ?? -1) >= 0
                      && ($0.disk_bytes ?? -1).isFinite && ($0.disk_bytes ?? -1) >= 0 }) else { throw MonitorError.invalidData }
            guard pool.ram_available.map({ $0.isFinite && $0 >= 0 }) ?? true else { throw MonitorError.invalidData }
            guard pool.ram_accounting?.used_bytes.map({ $0.isFinite && $0 >= 0 }) ?? true else { throw MonitorError.invalidData }
        }
        if let scope = value.scope {
            guard scope.id.range(of: "^[a-z][a-z0-9-]{0,31}$", options: .regularExpression) != nil,
                  ["opportunistic", "allocated"].contains(scope.mode) else { throw MonitorError.invalidData }
        }
        return value
    }

    var ramUsageFraction: Double? {
        guard let pool = pool, let available = pool.ram_available,
              let used = pool.ram_accounting?.used_bytes else { return nil }
        let budget = pool.ram_reserved + available
        guard budget.isFinite else { return nil }
        if budget == 0 { return used == 0 ? 0 : nil }
        return max(0, min(1, used / budget))
    }

    var quotaFraction: Double { max(0, min(1, used_bytes / budget_bytes)) }
    var agents: [Activity] { pool?.active ?? (busy ? telemetry?.active.map { [$0] } ?? [] : []) }
    var active: Activity? { agents.first }
    var recent: [Activity] { (telemetry?.activity ?? []).filter { item in !agents.contains { $0.id == item.id } } }
}

enum MonitorError: Error { case invalidData, timeout, connection }

struct CPUReading {
    let host: Double?
    let worker: Double?

    init(previous: Telemetry?, current: Telemetry?) {
        guard let old = previous, let new = current, new.sample_time > old.sample_time else {
            host = nil; worker = nil; return
        }
        if let total = new.cpu_total, let before = old.cpu_total,
           let idle = new.cpu_idle, let beforeIdle = old.cpu_idle,
           total > before, idle >= beforeIdle, idle - beforeIdle <= total - before {
            host = max(0, min(1, 1 - (idle - beforeIdle) / (total - before)))
        } else { host = nil }
        if let used = new.worker_cpu_usec, let before = old.worker_cpu_usec,
           let count = new.worker_cpu_limit ?? new.cpu_count.map(Double.init), count > 0, used >= before {
            worker = max(0, min(1, (used - before) / 1_000_000 / (new.sample_time - old.sample_time) / count))
        } else { worker = nil }
    }
}

func gib(_ bytes: Double?) -> String {
    guard let bytes = bytes, bytes.isFinite, bytes >= 0 else { return "—" }
    return String(format: "%.1f", locale: Locale(identifier: UILanguage.code), bytes / 1_073_741_824)
}

enum UILanguage {
    static var code: String {
        let choice = UserDefaults.standard.string(forKey: "atelier-language") ?? "system"
        return choice == "fr" || choice == "en" ? choice : (Locale.preferredLanguages.first?.hasPrefix("fr") == true ? "fr" : "en")
    }
}

func L(_ key: String, language: String? = nil) -> String {
    let code = language ?? UILanguage.code
    guard let path = Bundle.main.path(forResource: code, ofType: "lproj"), let bundle = Bundle(path: path) else { return key }
    return bundle.localizedString(forKey: key, value: key, table: "Localizable")
}

func LF(_ key: String, _ values: CVarArg...) -> String {
    String(format: L(key), locale: Locale(identifier: UILanguage.code), arguments: values)
}

func percent(_ fraction: Double?) -> String {
    guard let fraction = fraction else { return "—" }
    return String(format: "%.0f %%", fraction * 100)
}

func runModelChecks() throws {
    let base = """
    {"ok":true,"busy":true,"admit":false,"budget_bytes":28781314048,
    "workspace_free_bytes":28000000000,"used_bytes":781314048,"free_bytes":29000000000,
    "reserve_bytes":536870912,"host_available_ram":4294967296,
    "telemetry":{"sample_time":10,"arch":"aarch64","cpu_count":4,"cpu_total":1000,
    "cpu_idle":500,"host_total_ram":8589934592,"worker_ram":1073741824,
    "worker_ram_limit":5368709120,"worker_cpu_usec":1000000,"activity":[],"active":null}}
    """
    let first = try VPSSnapshot.decode(Data(base.utf8))
    precondition(first.ramUsageFraction == nil)
    precondition(CPUReading(previous: nil, current: first.telemetry).host == nil)
    let second = try VPSSnapshot.decode(Data(base.replacingOccurrences(of: "\"sample_time\":10", with: "\"sample_time\":12")
        .replacingOccurrences(of: "\"cpu_total\":1000", with: "\"cpu_total\":1200")
        .replacingOccurrences(of: "\"cpu_idle\":500", with: "\"cpu_idle\":650")
        .replacingOccurrences(of: "\"worker_cpu_usec\":1000000", with: "\"worker_cpu_usec\":3000000").utf8))
    let cpu = CPUReading(previous: first.telemetry, current: second.telemetry)
    precondition(cpu.host == 0.25 && cpu.worker == 0.25)
    precondition(CPUReading(previous: second.telemetry, current: first.telemetry).host == nil)
    var legacy = try JSONSerialization.jsonObject(with: Data(base.utf8)) as! [String: Any]
    legacy.removeValue(forKey: "telemetry")
    let oldServer = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: legacy))
    precondition(oldServer.busy && !oldServer.admit && oldServer.active == nil)
    precondition(oldServer.ramUsageFraction == nil)
    var sharing = try JSONSerialization.jsonObject(with: Data(base.utf8)) as! [String: Any]
    sharing["pool"] = ["cpu_total": 4, "cpu_reserved": 2, "ram_reserved": 1073741824,
        "disk_reserved": 2147483648, "active_count": 2, "max_jobs": 4,
        "active": ["a", "b"].map { ["id": String(repeating: $0, count: 32), "agent": "Tests \($0)",
            "project": "Widget", "status": "running", "started": 100, "cpus": 1,
            "ram_bytes": 536870912, "disk_bytes": 1073741824] as [String: Any] }]
    var pool = sharing["pool"] as! [String: Any]
    pool["ram_available"] = 1073741824
    pool["ram_accounting"] = ["used_bytes": 536870912]
    sharing["pool"] = pool
    let shared = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: sharing))
    precondition(shared.agents.count == 2 && shared.agents.allSatisfy { $0.cpus == 1 })
    precondition(shared.ramUsageFraction == 0.25)
    pool["active"] = []; pool["active_count"] = 0; pool["cpu_reserved"] = 0
    pool["ram_reserved"] = 0; pool["ram_available"] = 3221225472
    pool["ram_accounting"] = ["used_bytes": 0]
    sharing["pool"] = pool
    let idle = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: sharing))
    precondition(idle.telemetry?.worker_ram == 1073741824 && idle.ramUsageFraction == 0)
    pool["ram_available"] = 0; sharing["pool"] = pool
    let noHeadroom = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: sharing))
    precondition(noHeadroom.ramUsageFraction == 0)
    do { _ = try VPSSnapshot.decode(Data(base.replacingOccurrences(of: "28781314048", with: "-1").utf8)); preconditionFailure("accepted invalid quota") }
    catch MonitorError.invalidData { }
    do { _ = try VPSSnapshot.decode(Data(repeating: 32, count: 65537)); preconditionFailure("accepted oversized JSON") }
    catch MonitorError.invalidData { }
    precondition(gib(1073741824) == (UILanguage.code == "fr" ? "1,0" : "1.0"))
    precondition(L("Resources", language: "fr") == "Ressources")
    precondition(L("Resources", language: "en") == "Resources")
    var tenant = try JSONSerialization.jsonObject(with: Data(base.utf8)) as! [String: Any]
    tenant["scope"] = ["id": "alice", "mode": "allocated"]
    var own = tenant["telemetry"] as! [String: Any]
    own["worker_cpu_limit"] = 0.5
    own.removeValue(forKey: "host_total_ram")
    own.removeValue(forKey: "cpu_total"); own.removeValue(forKey: "cpu_idle")
    tenant["telemetry"] = own
    let onlyOwn = try VPSSnapshot.decode(JSONSerialization.data(withJSONObject: tenant))
    precondition(onlyOwn.scope?.id == "alice" && onlyOwn.ramUsageFraction == nil)
    let tenantCPU = CPUReading(previous: onlyOwn.telemetry, current: second.telemetry)
    precondition(tenantCPU.worker != nil)
    precondition(Activity(id: "a", agent: "Tests", project: "Widget", status: "failed", started: 100, ended: 132).elapsed(at: Date()) == "00:32")
    precondition(Activity(id: "a", agent: "Tests", project: "Widget", status: "failed", started: 0, ended: 1e300).elapsed(at: Date()) == "1440:00")
    print("Mac model: decoding, real CPU deltas, counter reset, unavailable data, bounds and failed duration passed")
}
