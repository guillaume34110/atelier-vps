import AppKit
import CryptoKit
import SwiftUI
import Darwin

struct FailureReport: Decodable {
    let version: Int
    let id: String
    let kind: String
    let returncode: Int
    let agent: String
    let project: String
    let started: Double
    let ended: Double
    let reason: String
    let log_tail: String
    let resources: [String: Double]

    var title: String {
        L(["memory": "Task RAM limit exceeded", "host_memory": "Host memory pressure", "disk": "Disk budget exceeded",
           "timeout": "Time limit exceeded", "logs": "Log limit reached", "restart": "Worker restarted",
           "command": "The command failed", "signal": "Process interrupted", "internal": "The worker could not finish"] [kind] ?? "The command failed")
    }
    var explanation: String {
        L(["memory": "The measured RAM peak exceeded this task's reservation. Retry with its learned auto profile, or declare a known higher peak; do not double the margin.",
           "host_memory": "The host RAM floor protected the other projects. Wait for real capacity before retrying.",
           "disk": "The declared peak, workspace quota or physical reserve was reached. Preserve useful results, release only your completed work, then declare the full disk peak.",
           "timeout": "The command exceeded its declared duration. Check the log, then choose a realistic duration within the worker limit.",
           "logs": "The worker could not safely retain more output. Reduce verbose output while keeping the required test assertions.",
           "restart": "The worker restarted before a complete receipt was written. Preserve partial results and rerun the required validation.",
           "command": "The program returned a nonzero exit code. The log excerpt below identifies the command's error; fix it before rerunning the full validation.",
           "signal": "A signal interrupted the process. The signal alone does not prove an out-of-memory error; inspect the log and resource measurements.",
           "internal": "The worker encountered an error before it could verify completion. Read the detail below; this task has not passed."] [kind] ?? "The worker encountered an error before it could verify completion. Read the detail below; this task has not passed.")
    }

    static func decode(_ data: Data, expectedID: String) throws -> FailureReport {
        guard data.count <= 32768 else { throw MonitorError.invalidData }
        let report = try JSONDecoder().decode(Self.self, from: data)
        let keys: Set<String> = ["ram_peak_bytes", "reserved_ram_bytes", "growth_bytes", "declared_disk_bytes", "cpus", "timeout_seconds"]
        guard report.version == 1, report.id == expectedID, report.id.count == 32,
              report.id.allSatisfy(\.isHexDigit),
              ["memory", "host_memory", "disk", "timeout", "logs", "restart", "command", "signal", "internal"].contains(report.kind),
              (-65535...65535).contains(report.returncode), report.agent.count <= 64, report.project.count <= 64,
              report.reason.count <= 1024, report.log_tail.count <= 4096,
              [report.started, report.ended].allSatisfy({ $0.isFinite && $0 >= 0 && $0 < 1e11 }),
              report.resources.count <= 8, Set(report.resources.keys).isSubset(of: keys),
              report.resources.values.allSatisfy({ $0.isFinite && $0 >= 0 && $0 < pow(2, 50) })
        else { throw MonitorError.invalidData }
        return report
    }

    static func decodeArchive(_ data: Data, expectedID: String) throws -> FailureReport {
        guard data.count <= 131072 else { throw MonitorError.invalidData }
        func extract(_ name: String, limit: Int) throws -> Data {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
            task.arguments = ["-xOf", "-", name]
            return try boundedFailureOutput(task, input: data, limit: limit)
        }
        struct Entry: Decodable { let bytes: Int; let sha256: String }
        let manifest = try JSONDecoder().decode([String: Entry].self, from: extract("manifest.json", limit: 4096))
        let body = try extract("diagnostic.json", limit: 32768)
        let digest = SHA256.hash(data: body).map { String(format: "%02x", $0) }.joined()
        guard manifest.count == 1, let entry = manifest["diagnostic.json"],
              entry.bytes == body.count, entry.sha256 == digest else { throw MonitorError.invalidData }
        return try decode(body, expectedID: expectedID)
    }
}

private func boundedFailureOutput(_ task: Process, input: Data, limit: Int) throws -> Data {
    let source = Pipe(), output = Pipe()
    task.standardInput = source; task.standardOutput = output; task.standardError = FileHandle.nullDevice
    try task.run()
    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 30) { [weak task] in
        guard let task = task, task.isRunning else { return }
        task.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 1) { [weak task] in
            if let task = task, task.isRunning { kill(task.processIdentifier, SIGKILL) }
        }
    }
    DispatchQueue.global(qos: .utility).async {
        try? source.fileHandleForWriting.write(contentsOf: input)
        try? source.fileHandleForWriting.close()
    }
    var data = Data()
    while let block = try output.fileHandleForReading.read(upToCount: 4096), !block.isEmpty {
        data.append(block)
        if data.count > limit { task.terminate(); break }
    }
    try? output.fileHandleForReading.close()
    task.waitUntilExit()
    guard task.terminationStatus == 0, data.count <= limit else { throw MonitorError.invalidData }
    return data
}

final class FailureDetails: ObservableObject {
    @Published var selected: Activity?
    @Published var report: FailureReport?
    @Published var loading = false
    @Published var error: String?
    var onUpdate: (() -> Void)?
    private var generation = UUID()
    private var process: Process?
    private let host: String

    init(host: String) { self.host = host }

    func open(_ activity: Activity) {
        guard activity.status == "failed" else { return }
        cancel(); report = nil; error = nil
        selected = activity
    }

    func cancel() {
        generation = UUID()
        if process?.isRunning == true { process?.terminate() }
        process = nil; loading = false
    }

    func load(_ activity: Activity) {
        cancel(); report = nil; error = nil; loading = true
        let token = generation, task = Process()
        guard host.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", options: .regularExpression) != nil,
              activity.id.count == 32, activity.id.allSatisfy(\.isHexDigit) else { loading = false; error = L("Failure details unavailable"); return }
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                          "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "-o", "LogLevel=ERROR", host, "fetch"]
        process = task
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let value: Result<FailureReport, Error> = Result {
                var input = try JSONSerialization.data(withJSONObject: ["id": activity.id, "paths": ["diagnostic.json"]])
                input.append(10)
                return try FailureReport.decodeArchive(boundedFailureOutput(task, input: input, limit: 131072), expectedID: activity.id)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.generation == token else { return }
                self.loading = false; self.process = nil
                switch value {
                case .success(let report): self.report = report
                case .failure: self.error = L("Details could not be read. The worker may be unavailable, or this older failure was not retained. Retry when connected.")
                }
                self.onUpdate?()
            }
        }
    }
}

struct FailureDetailView: View {
    let activity: Activity
    @ObservedObject var details: FailureDetails
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(L("Failure details"), systemImage: "exclamationmark.circle").font(.headline).foregroundColor(Paint.orange)
                Spacer()
                Button(L("Close")) { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text(activity.agent).fontWeight(.semibold).textSelection(.enabled)
            Text(activity.clockRange).font(.caption).foregroundColor(Paint.muted)
            if details.loading { ProgressView(L("Loading failure details…")) }
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let report = details.report {
                        Text(report.title).font(.headline)
                        Text(report.explanation).fixedSize(horizontal: false, vertical: true)
                        Text(LF("Exit code: %d", report.returncode)).font(.caption).monospacedDigit()
                        if !report.reason.isEmpty { Text(report.reason).font(.system(size: 11, design: .monospaced)).textSelection(.enabled) }
                        if let peak = report.resources["ram_peak_bytes"] {
                            Text(LF("RAM peak: %@ · reserved: %@", transferBytes(peak), transferBytes(report.resources["reserved_ram_bytes"])))
                        }
                        if let peak = report.resources["growth_bytes"] {
                            Text(LF("Disk used: %@ · budget: %@", transferBytes(peak), transferBytes(report.resources["declared_disk_bytes"])))
                        }
                        if let seconds = report.resources["timeout_seconds"] { Text(LF("Time budget: %.0f s", seconds)) }
                        if !report.log_tail.isEmpty {
                            Text(L("Log excerpt")).fontWeight(.semibold)
                            Text(report.log_tail).font(.system(size: 10, design: .monospaced)).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                .background(Paint.background).clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                    } else if let error = details.error {
                        Text(error).foregroundColor(Paint.muted)
                        Button(L("Retry")) { details.load(activity) }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(18).frame(minWidth: 320, idealWidth: 480, maxWidth: 680, minHeight: 280, idealHeight: 520)
            .foregroundColor(Paint.ivory).background(Paint.panel).preferredColorScheme(.dark)
            .onAppear { details.load(activity) }.onDisappear { details.cancel() }
    }
}

func runFailureChecks() throws {
    let ident = String(repeating: "a", count: 32)
    let object: [String: Any] = ["version": 1, "id": ident, "kind": "memory", "returncode": -15,
        "agent": "Tests", "project": "Widget", "started": 100, "ended": 120,
        "reason": "task memory limit", "log_tail": "AssertionError: expected 42",
        "resources": ["ram_peak_bytes": 167772160, "reserved_ram_bytes": 134217728]]
    let body = try JSONSerialization.data(withJSONObject: object)
    let first = try FailureReport.decode(body, expectedID: ident)
    precondition(first.kind == "memory")
    do { _ = try FailureReport.decode(body, expectedID: String(repeating: "b", count: 32)); preconditionFailure("accepted another task's details") }
    catch MonitorError.invalidData { }
    do { _ = try FailureReport.decodeArchive(Data(repeating: 0, count: 131073), expectedID: ident); preconditionFailure("accepted oversized archive") }
    catch MonitorError.invalidData { }
    guard let workspace = ProcessInfo.processInfo.environment["TASK_WORKSPACE"] else { throw MonitorError.invalidData }
    let directory = URL(fileURLWithPath: workspace).appendingPathComponent("failure-check-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: directory) }
    try body.write(to: directory.appendingPathComponent("diagnostic.json"), options: .withoutOverwriting)
    let sha = SHA256.hash(data: body).map { String(format: "%02x", $0) }.joined()
    func archive(_ digest: String) throws -> Data {
        let manifest: [String: Any] = ["diagnostic.json": ["bytes": body.count, "sha256": digest]]
        try JSONSerialization.data(withJSONObject: manifest).write(to: directory.appendingPathComponent("manifest.json"))
        let task = Process(); task.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
        task.arguments = ["-cf", "-", "-C", directory.path, "manifest.json", "diagnostic.json"]
        return try boundedFailureOutput(task, input: Data(), limit: 131072)
    }
    let verified = try FailureReport.decodeArchive(archive(sha), expectedID: ident)
    precondition(verified.log_tail.contains("expected 42"))
    do { _ = try FailureReport.decodeArchive(archive(String(repeating: "0", count: 64)), expectedID: ident); preconditionFailure("accepted modified failure details") }
    catch MonitorError.invalidData { }
    let asset = Bundle.main.url(forResource: "meter-face", withExtension: "png")!
    let assetData = try Data(contentsOf: asset)
    let pixels = NSBitmapImageRep(data: assetData)!
    precondition(pixels.pixelsWide == 640 && pixels.pixelsHigh == 360)
    precondition(assetData.count <= 65536)
    print("Private failure SHA256, payload bounds and fixed-aspect PNG checks passed")
}
