import Foundation
import Dispatch
import Virtualization

final class Delegate: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        print("guest_stopped")
        fflush(stdout)
        exit(0)
    }

    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        fputs("guest_failed: \(error)\n", stderr)
        exit(1)
    }
}

guard (CommandLine.arguments.count == 10 ||
       (CommandLine.arguments.count == 11 && CommandLine.arguments[10] == "bootstrap")),
      let cpus = Int(CommandLine.arguments[7]), cpus >= 1,
      cpus <= ProcessInfo.processInfo.activeProcessorCount,
      let memory = UInt64(CommandLine.arguments[8]), memory >= 128 * 1024 * 1024,
      memory <= ProcessInfo.processInfo.physicalMemory,
      let timeout = Int(CommandLine.arguments[9]), (1...3600).contains(timeout) else { exit(2) }

let boot = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: CommandLine.arguments[1]))
boot.initialRamdiskURL = URL(fileURLWithPath: CommandLine.arguments[2])
boot.commandLine = "console=hvc0 loglevel=3"
let config = VZVirtualMachineConfiguration()
config.bootLoader = boot
config.cpuCount = cpus
config.memorySize = memory

func directory(_ tag: String, _ path: String, _ readOnly: Bool) -> VZVirtioFileSystemDeviceConfiguration {
    let device = VZVirtioFileSystemDeviceConfiguration(tag: tag)
    let share = VZSharedDirectory(url: URL(fileURLWithPath: path), readOnly: readOnly)
    device.share = VZSingleDirectoryShare(directory: share)
    return device
}
config.directorySharingDevices = [directory("atelier", CommandLine.arguments[3], false),
                                  directory("tools", CommandLine.arguments[4], true),
                                  directory("modules", CommandLine.arguments[5], true)]
guard let disk = try? VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: CommandLine.arguments[6]), readOnly: false),
      let input = FileHandle(forReadingAtPath: "/dev/null") else { exit(2) }
config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: disk)]
if CommandLine.arguments.count == 11 {
    let network = VZVirtioNetworkDeviceConfiguration()
    network.attachment = VZNATNetworkDeviceAttachment()
    config.networkDevices = [network]
}
let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: input, fileHandleForWriting: .standardError)
config.serialPorts = [serial]
do { try config.validate() } catch { fputs("invalid VM configuration: \(error)\n", stderr); exit(1) }

let queue = DispatchQueue(label: "atelier-vps.compute-vm")
let machine = VZVirtualMachine(configuration: config, queue: queue)
let delegate = Delegate()
machine.delegate = delegate
queue.async {
    machine.start { result in
        switch result {
        case .failure(let error): fputs("guest_start_failed: \(error)\n", stderr); exit(1)
        case .success:
            print("guest_started")
            fflush(stdout)
            queue.asyncAfter(deadline: .now() + .seconds(timeout)) {
                machine.stop { _ in
                    fputs("guest_timeout\n", stderr)
                    exit(124)
                }
            }
        }
    }
}
dispatchMain()
