import SwiftUI

struct AdminView: View {
    @ObservedObject var admin: Administration

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Label(L("Administrator management"), systemImage: "gearshape")
                        .font(.system(size: 15, weight: .semibold))
                    Spacer(minLength: 4)
                    if admin.busy { ProgressView().controlSize(.small) }
                }
                Text(L("Applied mode: ") + L(admin.state?.policy.mode == "allocated" ? "Team" : "Solo"))
                    .font(.system(size: 11)).foregroundColor(Paint.muted)
                Picker(L("Mode"), selection: Binding(get: { admin.draft.mode }, set: { admin.selectMode($0) })) {
                    Text(L("Solo")).tag("opportunistic")
                    Text(L("Team")).tag("allocated")
                }.pickerStyle(.segmented).labelsHidden().accessibilityIdentifier("admin-mode")
                Text(L("Solo lends all CPU and RAM to the owner. Other members are disabled; their files and disk quotas are kept."))
                    .font(.system(size: 11)).foregroundColor(Paint.muted).fixedSize(horizontal: false, vertical: true)

                VStack(alignment: .leading, spacing: 10) {
                    Text(L("Resource pool")).fontWeight(.semibold)
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 10) {
                        numberField("CPU (vCPU)", value: $admin.draft.pool.cpus)
                        numberField("RAM (GiB)", value: bytes($admin.draft.pool.ram_bytes))
                        numberField("Storage (GiB)", value: bytes($admin.draft.pool.disk_bytes))
                        Stepper(value: $admin.draft.pool.slots, in: 1...4) {
                            Text(L("Tasks") + ": \(admin.draft.pool.slots)")
                        }
                    }
                    if let state = admin.state {
                        Text(LF("Host protection: %@ %@", gib(state.host_ram_reserve_bytes.map(Double.init)), L("GiB")))
                        if let available = state.allocatable_ram_bytes {
                            Text(LF("Team RAM available now: %@ %@", gib(Double(available)), L("GiB")))
                        }
                    }
                }.font(.system(size: 11)).padding(12).background(card)

                HStack {
                    Text(L("Team members")).font(.system(size: 13, weight: .semibold))
                    Spacer(minLength: 4)
                    Text(L(admin.draft == admin.state?.policy ? "Applied" : "Draft"))
                        .font(.system(size: 10)).foregroundColor(Paint.green)
                }
                Text(L("Percentages refer to the pool above. Disabled members retain their disk allocation."))
                    .font(.system(size: 11)).foregroundColor(Paint.muted)
                ForEach(admin.draft.users.indices, id: \.self) { index in
                    member(index)
                }
                totals
                if admin.draft.mode == "allocated", admin.draft.users.count < 4 {
                    Button(L("Add member")) {
                        var suffix = 1
                        while admin.draft.users.contains(where: { $0.id == "member\(suffix)" }) { suffix += 1 }
                        admin.draft.users.append(AdminUser(id: "member\(suffix)", enabled: true, public_key: "",
                            cpu_percent: 25, ram_percent: 25, disk_percent: 25, slots: 1))
                    }.accessibilityIdentifier("admin-add-member")
                }
                if let error = admin.error {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundColor(.orange)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                if let notice = admin.notice { Text(notice).foregroundColor(Paint.green) }
                if admin.state?.pending != nil {
                    Text(L("An unfinished update must be resumed with the same settings.")).foregroundColor(.orange)
                }
                Button(L("Preview changes")) { admin.preview() }
                    .accessibilityIdentifier("admin-preview")
                if let plan = admin.plan, plan.policy == admin.draft {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(L("Verified preview")).fontWeight(.semibold)
                        Text(plan.affected.isEmpty ? L("No resource or access changes.") : L("Affected members: ") + plan.affected.joined(separator: ", "))
                        Text(L("Active tasks block changes. Existing files are preserved. Deployment can take several minutes."))
                            .foregroundColor(Paint.muted)
                        Button(L("Apply verified changes")) { admin.apply() }
                            .buttonStyle(.borderedProminent).tint(Paint.green).foregroundColor(.black)
                            .disabled(!admin.canApply).accessibilityIdentifier("admin-apply")
                    }.padding(12).background(card)
                }
                Button(L("Reload applied settings")) { admin.notice = nil; admin.load() }
                if admin.applying { Text(L("Applying settings… Keep the widget open.")).foregroundColor(Paint.green) }
            }
            .font(.system(size: 12)).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            .disabled(admin.busy)
        }
        .onChange(of: admin.draft) { _ in admin.plan = nil; admin.notice = nil }
        .onChange(of: admin.draft.pool.slots) { slots in
            if admin.draft.mode == "opportunistic", let owner = admin.draft.users.firstIndex(where: { $0.id == "owner" }) {
                admin.draft.users[owner].slots = slots
            }
        }
    }

    private var card: some View {
        RoundedRectangle(cornerRadius: 12).fill(.black.opacity(0.15))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.10), lineWidth: 1))
    }

    private func numberField(_ label: String, value: Binding<Double>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(L(label)).foregroundColor(Paint.muted)
            TextField(L(label), value: value, format: .number.precision(.fractionLength(0...3)))
                .textFieldStyle(.roundedBorder).accessibilityLabel(L(label))
        }
    }

    private func bytes(_ binding: Binding<Int>) -> Binding<Double> {
        Binding(get: { Double(binding.wrappedValue) / 1073741824 }, set: { value in
            if value.isFinite && value > 0 && value <= 1048576 {
                binding.wrappedValue = Int(value * 1024) * 1048576
            }
        })
    }

    private func member(_ index: Int) -> some View {
        let binding = $admin.draft.users[index]
        let existing = admin.state?.policy.users.contains(where: { $0.id == binding.wrappedValue.id }) == true
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: binding.wrappedValue.id == "owner" ? "crown" : "person")
                    .foregroundColor(Paint.ivory)
                TextField(L("Member ID"), text: binding.id).textFieldStyle(.roundedBorder)
                    .disabled(existing).accessibilityLabel(L("Member ID"))
                Toggle(L("Enabled"), isOn: Binding(get: { binding.wrappedValue.active }, set: { binding.wrappedValue.enabled = $0 }))
                    .toggleStyle(.switch).controlSize(.small)
                    .disabled(admin.draft.mode == "opportunistic")
            }
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 8) {
                numberField("CPU (%)", value: binding.cpu_percent).disabled(admin.draft.mode == "opportunistic")
                numberField("RAM (%)", value: binding.ram_percent).disabled(admin.draft.mode == "opportunistic")
                numberField("Storage (%)", value: binding.disk_percent)
                Stepper(value: binding.slots, in: 1...4) { Text(L("Tasks") + ": \(binding.wrappedValue.slots)") }
                    .disabled(admin.draft.mode == "opportunistic")
            }
            if binding.wrappedValue.id != "owner" {
                TextField(L("SSH public key (ssh-ed25519…)"), text: Binding(get: { binding.wrappedValue.public_key ?? "" }, set: { binding.wrappedValue.public_key = $0 }))
                    .textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced))
                    .accessibilityLabel(L("SSH public key (ssh-ed25519…)"))
            }
        }.font(.system(size: 11)).padding(12).background(card)
    }

    private var totals: some View {
        let active = admin.draft.users.filter(\.active)
        let cpu = active.reduce(0) { $0 + $1.cpu_percent }, ram = active.reduce(0) { $0 + $1.ram_percent }
        let disk = admin.draft.users.reduce(0) { $0 + $1.disk_percent }
        let slots = active.reduce(0) { $0 + $1.slots }
        return Text(LF("Totals: CPU %.0f%% · RAM %.0f%% · disk %.0f%% · %d tasks", cpu, ram, disk, slots))
            .font(.system(size: 10)).foregroundColor(cpu > 100 || ram > 100 || disk > 100 || slots > admin.draft.pool.slots ? .orange : Paint.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}
