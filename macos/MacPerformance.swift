import Darwin
import IOKit
import IOKit.ps
import Metal
import SwiftUI

private struct CPUTicks {
    let user: UInt32
    let system: UInt32
    let nice: UInt32
    let idle: UInt32

    func since(_ previous: Self) -> (busy: UInt64, total: UInt64) {
        let busy = UInt64(user &- previous.user) + UInt64(system &- previous.system) + UInt64(nice &- previous.nice)
        return (busy, busy + UInt64(idle &- previous.idle))
    }
}

private struct MacUsage {
    let used: Double
    let total: Double
    var fraction: Double? { total > 0 ? min(1, max(0, used / total)) : nil }
    var available: Double { max(0, total - used) }
    var label: String { LF("%@ / %@ %@", gib(used), gib(total), L("GiB")) }
}

private struct MacMemory {
    let usage: MacUsage
    let wired: Double
    let compressed: Double
    let cached: Double
    let swap: Double?
}

private struct MacGPU: Identifiable {
    let id: UInt64
    let name: String
    let cores: Int?
    let unified: Bool
    let workingSet: Double?
    let activity: Double?
    let renderer: Double?
    let tiler: Double?
    let memory: Double?
    let dedicated: MacUsage?
}

private struct IOCounters {
    let received: UInt64
    let sent: UInt64
}

private struct MacPerformance {
    var cpu: Double?
    var cores: [Double?] = []
    var memory: MacMemory?
    var disk: MacUsage?
    var gpus: [MacGPU] = []
    var storageRate: (read: Double, write: Double)?
    var networkRate: (received: Double, sent: Double)?
    var battery: (charge: Double, plugged: Bool)?
    var thermal: ProcessInfo.ThermalState = .nominal
}

private final class MacPerformanceSampler {
    private static let host = mach_host_self()
    private let devices = MTLCopyAllDevices()
    private var previousCPU: [CPUTicks] = []
    private var previousDisks: [UInt64: IOCounters] = [:]
    private var previousInterfaces: [String: IOCounters] = [:]
    private var previousTime: Double?

    static func sysctl<Value>(_ name: String, initial: Value) -> Value? {
        var value = initial
        var count = MemoryLayout<Value>.size
        return withUnsafeMutableBytes(of: &value) { bytes in
            sysctlbyname(name, bytes.baseAddress, &count, nil, 0)
        } == 0 && count == MemoryLayout<Value>.size ? value : nil
    }

    private func cpuTicks() -> [CPUTicks] {
        var processors: natural_t = 0
        var size: mach_msg_type_number_t = 0
        var data: processor_info_array_t?
        guard host_processor_info(Self.host, PROCESSOR_CPU_LOAD_INFO, &processors, &data, &size) == KERN_SUCCESS,
              let data else { return [] }
        defer { vm_deallocate(mach_task_self_, vm_address_t(UInt(bitPattern: data)), vm_size_t(size) * vm_size_t(MemoryLayout<integer_t>.stride)) }
        let width = Int(CPU_STATE_MAX)
        guard Int(processors) <= Int(size) / width else { return [] }
        return (0..<Int(processors)).map { core in
            let start = core * width
            return CPUTicks(user: UInt32(bitPattern: data[start + Int(CPU_STATE_USER)]),
                            system: UInt32(bitPattern: data[start + Int(CPU_STATE_SYSTEM)]),
                            nice: UInt32(bitPattern: data[start + Int(CPU_STATE_NICE)]),
                            idle: UInt32(bitPattern: data[start + Int(CPU_STATE_IDLE)]))
        }
    }

    private func memory() -> MacMemory? {
        var vm = vm_statistics64()
        var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &vm) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics64(Self.host, HOST_VM_INFO64, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        let page = Double(vm_kernel_page_size)
        let wired = Double(vm.wire_count) * page
        let compressed = Double(vm.compressor_page_count) * page
        let app = Double(max(0, Int(vm.internal_page_count) - Int(vm.purgeable_count))) * page
        let total = Double(ProcessInfo.processInfo.physicalMemory)
        return MacMemory(usage: MacUsage(used: min(total, app + wired + compressed), total: total),
                         wired: wired, compressed: compressed, cached: Double(vm.external_page_count) * page,
                         swap: Self.sysctl("vm.swapusage", initial: xsw_usage()).map { Double($0.xsu_used) })
    }

    private func startupDisk() -> MacUsage? {
        var volume = statfs()
        guard statfs("/System/Volumes/Data", &volume) == 0 || statfs("/", &volume) == 0 else { return nil }
        let block = Double(volume.f_bsize)
        let physicalUsed = max(0, Double(volume.f_blocks) - Double(volume.f_bfree)) * block
        return MacUsage(used: physicalUsed, total: physicalUsed + Double(volume.f_bavail) * block)
    }

    private func registry(_ serviceClass: String, _ keys: [String]) -> [UInt64: [String: Any]] {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching(serviceClass), &iterator) == KERN_SUCCESS else { return [:] }
        defer { IOObjectRelease(iterator) }
        var found: [UInt64: [String: Any]] = [:]
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            defer { IOObjectRelease(entry) }
            var id: UInt64 = 0
            guard IORegistryEntryGetRegistryEntryID(entry, &id) == KERN_SUCCESS else { continue }
            var properties: [String: Any] = [:]
            for key in keys {
                properties[key] = IORegistryEntryCreateCFProperty(entry, key as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue()
            }
            found[id] = properties
        }
        return found
    }

    private func gpuReadings() -> [MacGPU] {
        // Driver statistics are optional across hardware and OS versions.
        let accelerators = registry("IOAccelerator", ["PerformanceStatistics", "gpu-core-count"])
        return devices.map { device in
            let properties = accelerators[device.registryID] ??
                (devices.count == 1 && accelerators.count == 1 ? accelerators.values.first : nil)
            let stats = properties?["PerformanceStatistics"] as? [String: NSNumber]
            let used = stats?["vramUsedBytes"]?.doubleValue
            let free = stats?["vramFreeBytes"]?.doubleValue
            let dedicated = (!device.hasUnifiedMemory ? used.flatMap { used in
                free.map { MacUsage(used: used, total: used + $0) }
            } : nil)
            return MacGPU(id: device.registryID, name: device.name,
                          cores: (properties?["gpu-core-count"] as? NSNumber)?.intValue,
                          unified: device.hasUnifiedMemory,
                          workingSet: device.recommendedMaxWorkingSetSize > 0 ? Double(device.recommendedMaxWorkingSetSize) : nil,
                          activity: driverFraction(stats?["Device Utilization %"]?.doubleValue),
                          renderer: driverFraction(stats?["Renderer Utilization %"]?.doubleValue),
                          tiler: driverFraction(stats?["Tiler Utilization %"]?.doubleValue),
                          memory: device.hasUnifiedMemory ? stats?["In use system memory"]?.doubleValue : used,
                          dedicated: dedicated)
        }
    }

    private func network() -> [String: IOCounters] {
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let list else { return [:] }
        defer { freeifaddrs(list) }
        var counters: [String: IOCounters] = [:]
        var current: UnsafeMutablePointer<ifaddrs>? = list
        while let interface = current?.pointee {
            current = interface.ifa_next
            guard interface.ifa_addr?.pointee.sa_family == UInt8(AF_LINK),
                  interface.ifa_flags & UInt32(IFF_UP) != 0,
                  interface.ifa_flags & UInt32(IFF_LOOPBACK) == 0,
                  let data = interface.ifa_data?.assumingMemoryBound(to: if_data.self).pointee,
                  data.ifi_type == IFT_ETHER else { continue }
            counters[String(cString: interface.ifa_name)] = IOCounters(received: UInt64(data.ifi_ibytes), sent: UInt64(data.ifi_obytes))
        }
        return counters
    }

    private func battery() -> (Double, Bool)? {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let sources = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] else { return nil }
        for source in sources {
            guard let description = IOPSGetPowerSourceDescription(info, source)?.takeUnretainedValue() as? [String: Any],
                  description[kIOPSTransportTypeKey] as? String == kIOPSInternalType,
                  let current = description[kIOPSCurrentCapacityKey] as? Double,
                  let maximum = description[kIOPSMaxCapacityKey] as? Double, maximum > 0 else { continue }
            return (min(1, max(0, current / maximum)), description[kIOPSPowerSourceStateKey] as? String == kIOPSACPowerValue)
        }
        return nil
    }

    func sample() -> MacPerformance {
        let ticks = cpuTicks()
        let deltas = zip(ticks, previousCPU).map { $0.since($1) }
        previousCPU = ticks
        let busy = deltas.reduce(0) { $0 + $1.busy }
        let total = deltas.reduce(0) { $0 + $1.total }
        let disks = registry("IOBlockStorageDriver", ["Statistics"]).compactMapValues { properties -> IOCounters? in
            guard let stats = properties["Statistics"] as? [String: NSNumber],
                  let read = stats["Bytes (Read)"], let write = stats["Bytes (Write)"] else { return nil }
            return IOCounters(received: read.uint64Value, sent: write.uint64Value)
        }
        let interfaces = network()
        let now = ProcessInfo.processInfo.systemUptime
        let elapsed = now - (previousTime ?? now)
        let io = rate(disks, since: previousDisks, elapsed: elapsed)
        let traffic = rate(interfaces, since: previousInterfaces, elapsed: elapsed, wrapsAt32Bits: true)
        previousTime = now
        previousDisks = disks
        previousInterfaces = interfaces

        return MacPerformance(cpu: total > 0 ? Double(busy) / Double(total) : nil,
                              cores: ticks.indices.map { index in
                                  guard deltas.indices.contains(index), deltas[index].total > 0 else { return nil }
                                  return Double(deltas[index].busy) / Double(deltas[index].total)
                              }, memory: memory(), disk: startupDisk(), gpus: gpuReadings(),
                              storageRate: io, networkRate: traffic, battery: battery(),
                              thermal: ProcessInfo.processInfo.thermalState)
    }
}

private func driverFraction(_ percent: Double?) -> Double? {
    guard let percent, (0...100).contains(percent) else { return nil }
    return percent / 100
}

private func rate<Key>(_ current: [Key: IOCounters], since previous: [Key: IOCounters], elapsed: Double,
                       wrapsAt32Bits: Bool = false) -> (Double, Double)? {
    guard elapsed > 0 else { return nil }
    var received: Double = 0
    var sent: Double = 0
    var matched = false
    for (key, counter) in current {
        guard let old = previous[key] else { continue }
        matched = true
        if wrapsAt32Bits {
            received += Double(UInt32(truncatingIfNeeded: counter.received) &- UInt32(truncatingIfNeeded: old.received))
            sent += Double(UInt32(truncatingIfNeeded: counter.sent) &- UInt32(truncatingIfNeeded: old.sent))
        } else {
            if counter.received >= old.received { received += Double(counter.received - old.received) }
            if counter.sent >= old.sent { sent += Double(counter.sent - old.sent) }
        }
    }
    return matched ? (received / elapsed, sent / elapsed) : nil
}

struct MacPerformanceView: View {
    let layout: WidgetLayout
    let visible: Bool
    var embedded = false
    @State private var sample = MacPerformance()
    private static let performanceCores = MacPerformanceSampler.sysctl("hw.perflevel0.physicalcpu", initial: Int32(0))
    private static let efficiencyCores = MacPerformanceSampler.sysctl("hw.perflevel1.physicalcpu", initial: Int32(0))
    private var compact: Bool { layout == .compact }

    var body: some View {
        Group {
            if embedded {
                VStack(spacing: 6) {
                    HStack {
                        Label("Mac", systemImage: "laptopcomputer").foregroundStyle(Paint.ivory)
                        Spacer()
                        if let gpu = sample.gpus.first { Text(gpu.name).foregroundStyle(Paint.muted) }
                    }.font(.system(size: 11, weight: .semibold, design: .monospaced))
                    MacReadings(sample: sample, dense: layout == .compact, cpuDetail: coreSummary, thermal: L(thermalLabel))
                    Collapsible("Mac details", key: "global.mac.details", initiallyExpanded: false) { details }
                }.padding(10).lcdPanel()
            } else {
                ScrollView { details }
            }
        }.sampleMac(visible: visible, sample: $sample)
    }

    private var details: some View {
            VStack(spacing: 10) {
                HStack(spacing: 8) {
                    Dial(title: L("CPU"), value: percent(sample.cpu), fraction: sample.cpu, detail: coreSummary, compact: compact)
                    Dial(title: L("RAM"), value: percent(sample.memory?.usage.fraction), fraction: sample.memory?.usage.fraction,
                         detail: sample.memory?.usage.label ?? "—", compact: compact)
                    Dial(title: L("Disk"), value: percent(sample.disk?.fraction), fraction: sample.disk?.fraction,
                         detail: sample.disk?.label ?? "—", compact: compact)
                }
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), alignment: .topLeading), count: layout == .wide ? 2 : 1), spacing: 10) {
                    ForEach(sample.gpus) { gpu in gpuCard(gpu) }
                    Collapsible("CPU cores", key: "mac.cpu") {
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 4), spacing: 8) {
                            ForEach(sample.cores.indices, id: \.self) { index in
                                DeviceMeter(title: LF("Core %d", index + 1), value: percent(sample.cores[index]),
                                            fraction: sample.cores[index], small: true)
                            }
                        }
                    }
                    Collapsible("Local memory", key: "mac.memory", glyph: .ram) {
                        if let memory = sample.memory {
                            row("Wired", size(memory.wired))
                            row("Compressed", size(memory.compressed))
                            row("File-backed", size(memory.cached))
                            row("Swap used", size(memory.swap))
                        } else { row("RAM", "—") }
                    }
                    Collapsible("Local storage", key: "mac.storage", glyph: .disk) {
                        row("Startup disk free", size(sample.disk?.available))
                        row("Disk read (all drives)", speed(sample.storageRate?.read))
                        row("Disk write (all drives)", speed(sample.storageRate?.write))
                    }
                    Collapsible("Mac system", key: "mac.system", glyph: .battery) {
                        row("Thermal state", L(thermalLabel))
                        if let battery = sample.battery {
                            row(battery.plugged ? "Battery · AC power" : "Battery", percent(battery.charge))
                        }
                        row("Network received", speed(sample.networkRate?.received))
                        row("Network sent", speed(sample.networkRate?.sent))
                    }
                }.font(.system(size: 11, design: .monospaced)).monospacedDigit()
            }
    }

    fileprivate static func cpuDetail() -> String {
        if let p = Self.performanceCores, let e = Self.efficiencyCores, p > 0, e > 0 {
            return LF("%d P · %d E", p, e)
        }
        return LF("%d cores", ProcessInfo.processInfo.processorCount)
    }

    private var coreSummary: String { Self.cpuDetail() }
    private var thermalLabel: String { sample.thermalLabel }

    private func gpuCard(_ gpu: MacGPU) -> some View {
        Collapsible("Metal GPU", key: "mac.gpu.\(gpu.id)", glyph: .gpu) {
            HStack {
                Text(gpu.name).fontWeight(.semibold).lineLimit(1)
                Spacer()
                if let cores = gpu.cores { Text(LF("%d cores", cores)) }
            }
            DeviceMeter(title: L("GPU activity"), value: gpu.activity.map { percent($0) } ?? L("Unavailable"),
                        fraction: gpu.activity)
            if let renderer = gpu.renderer { row("Renderer", percent(renderer)) }
            if let tiler = gpu.tiler { row("Tiler", percent(tiler)) }
            if gpu.unified {
                row("GPU memory · shared", size(gpu.memory))
            } else {
                row("Dedicated VRAM", gpu.dedicated?.label ?? size(gpu.memory))
            }
            row("Metal recommended working set", size(gpu.workingSet))
        }
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(L(title)).foregroundColor(Paint.muted)
            Spacer(minLength: 8)
            Text(value).multilineTextAlignment(.trailing)
        }.accessibilityElement(children: .combine)
    }

    private func size(_ bytes: Double?) -> String { bytes.map { gib($0) + " " + L("GiB") } ?? "—" }
    private func speed(_ bytes: Double?) -> String { bytes.map { LF("%@/s", transferBytes($0)) } ?? "—" }
}

private extension View {
    func sampleMac(visible: Bool, sample: Binding<MacPerformance>) -> some View {
        task(id: visible) { @MainActor in
            guard visible else { return }
            let sampler = MacPerformanceSampler()
            while !Task.isCancelled {
                sample.wrappedValue = sampler.sample()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }
}

struct DeviceMeter: View {
    let title: String
    let value: String
    let fraction: Double?
    var detail: String? = nil
    var glyph: DeviceGlyph? = nil
    var small = false

    var body: some View {
        VStack(alignment: .leading, spacing: small ? 1 : 2) {
            HStack(spacing: 3) {
                if let glyph { DeviceIcon(glyph: glyph, size: small ? 12 : 14) }
                Text(title).font(.system(size: 9, weight: .medium)).foregroundStyle(Paint.muted)
            }
            Text(value).font(.system(size: small ? 15 : 16, weight: .semibold, design: .monospaced))
                .foregroundStyle((fraction ?? 0) >= 0.85 ? Paint.orange : Paint.green)
            HStack(spacing: 2) {
                ForEach(0..<10) { segment in
                    RoundedRectangle(cornerRadius: 1).fill(Double(segment) + 0.5 <= (fraction ?? 0) * 10
                                     ? (segment > 7 ? Paint.orange : Paint.violet) : Paint.violet.opacity(0.16))
                }
            }.frame(height: 4).accessibilityHidden(true)
            if let detail { Text(detail).font(.system(size: small ? 8 : 9)).foregroundStyle(Paint.ivory) }
        }.lineLimit(1).minimumScaleFactor(0.70)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
    }
}

private extension MacPerformance {
    var thermalLabel: String {
        switch thermal {
        case .nominal: return "Nominal"
        case .fair: return "Fair"
        case .serious: return "Serious"
        case .critical: return "Critical"
        @unknown default: return "Unavailable"
        }
    }
}

struct MiniMacPerformance: View {
    let visible: Bool
    let dense: Bool
    let expandedReadout: Bool
    @State private var sample = MacPerformance()
    var body: some View {
        MacReadings(sample: sample, dense: dense, cpuDetail: MacPerformanceView.cpuDetail(),
                    thermal: L(sample.thermalLabel), mini: !expandedReadout)
            .sampleMac(visible: visible, sample: $sample)
    }
}

private struct MacReadings: View {
    let sample: MacPerformance
    let dense: Bool
    let cpuDetail: String
    let thermal: String
    var mini = false

    var body: some View {
        VStack(spacing: mini ? 3 : 4) {
            HStack(alignment: .top, spacing: mini ? 5 : 6) {
                DeviceMeter(title: "CPU", value: percent(sample.cpu), fraction: sample.cpu,
                            detail: dense ? nil : cpuDetail, glyph: .cpu, small: mini)
                DeviceMeter(title: "RAM", value: percent(sample.memory?.usage.fraction), fraction: sample.memory?.usage.fraction,
                            detail: dense ? nil : ramDetail, glyph: .ram, small: mini)
                DeviceMeter(title: mini ? "GPU" : "GPU Metal", value: percent(sample.gpus.first?.activity), fraction: sample.gpus.first?.activity,
                            detail: dense ? nil : gpuDetail, glyph: .gpu, small: mini)
                DeviceMeter(title: L("Disk"), value: percent(sample.disk?.fraction), fraction: sample.disk?.fraction,
                            detail: dense ? nil : diskDetail, glyph: .disk, small: mini)
            }
            if !dense {
                HStack(spacing: 4) {
                    if sample.battery != nil { DeviceIcon(glyph: .battery, size: 12) }
                    ViewThatFits(in: .horizontal) {
                        if !mini { Text(systemStats + "   ·   " + traffic) }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(systemStats)
                            if !mini { Text(traffic) }
                        }
                    }
                }.font(.system(size: mini ? 8 : 9, design: .monospaced)).lineLimit(1).minimumScaleFactor(0.8)
                    .foregroundStyle(Paint.muted).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var ramDetail: String? {
        sample.memory.map { mini ? size($0.usage.total) : $0.usage.label }
    }
    private var diskDetail: String? {
        sample.disk.map { mini ? size($0.available) : LF("Free: %@ %@", gib($0.available), L("GiB")) }
    }
    private var gpuDetail: String? {
        guard let gpu = sample.gpus.first else { return nil }
        if mini { return gpu.cores.map { LF("%d cores", $0) } }
        return [gpu.cores.map { LF("%d cores", $0) }, gpu.memory.map { size($0) }].compactMap { $0 }.joined(separator: " · ")
    }
    private var systemStats: String {
        var values: [String] = []
        if let battery = sample.battery { values.append((mini ? "" : L("Battery") + " ") + percent(battery.charge)) }
        values.append("Swap " + size(sample.memory?.swap))
        values.append(mini ? thermal : L("Thermal state") + " " + thermal)
        return values.joined(separator: " · ")
    }
    private var traffic: String {
        L("Disk") + " ↓" + speed(sample.storageRate?.read) + " ↑" + speed(sample.storageRate?.write)
            + "   NET ↓" + speed(sample.networkRate?.received) + " ↑" + speed(sample.networkRate?.sent)
    }
    private func size(_ bytes: Double?) -> String { bytes.map { gib($0) + " " + L("GiB") } ?? "—" }
    private func speed(_ bytes: Double?) -> String { bytes.map { LF("%@/s", transferBytes($0)) } ?? "—" }
}

func runMacPerformanceChecks() {
    let previous = CPUTicks(user: UInt32.max, system: 2, nice: 0, idle: 5)
    let delta = CPUTicks(user: 1, system: 3, nice: 0, idle: 8).since(previous)
    precondition(delta.busy == 3 && delta.total == 6)
    precondition(driverFraction(0) == 0 && driverFraction(65) == 0.65 && driverFraction(101) == nil)
    precondition(MacUsage(used: 25, total: 100).fraction == 0.25)
    let old = ["en0": IOCounters(received: UInt64(UInt32.max), sent: 10)]
    let new = ["en0": IOCounters(received: 5, sent: 14), "en1": IOCounters(received: 100, sent: 100)]
    let throughput = rate(new, since: old, elapsed: 2, wrapsAt32Bits: true)
    precondition(throughput?.0 == 3 && throughput?.1 == 2)
    let live = MacPerformanceSampler().sample()
    precondition(!live.cores.isEmpty && live.cores.allSatisfy { $0 == nil })
    precondition(live.memory?.usage.fraction != nil && live.disk?.fraction != nil)
}
