import SwiftUI
import NexusKit

/// The "new session" sheet: host/harness pickers, a cwd field with a
/// suggestions menu, and (for structured harnesses only) a starting prompt.
/// All defaulting/validation/network logic lives in `LaunchModel` — this view
/// only renders it and reports the launched session back to the caller.
struct LaunchView: View {
    @State private var model: LaunchModel
    @Environment(\.dismiss) private var dismiss
    @State private var isLaunching = false
    @State private var showAuth = false
    private let client: NexusClient

    /// Called once `launch()` succeeds, with the new session's id and
    /// whether it's structured (so the caller knows Chat vs. terminal).
    let onLaunched: (String, Bool) -> Void

    init(client: NexusClient, snapshot: HarnessSnapshot?, onLaunched: @escaping (String, Bool) -> Void) {
        _model = State(initialValue: LaunchModel(client: client, snapshot: snapshot))
        self.client = client
        self.onLaunched = onLaunched
    }

    var body: some View {
        @Bindable var model = model

        Form {
            Section("Host") {
                Picker("Host", selection: $model.hostID) {
                    ForEach(model.availableHosts) { host in
                        Text(host.displayName.isEmpty ? host.id : host.displayName)
                            .tag(host.id)
                    }
                }
            }

            Section("Harness") {
                Picker("Harness", selection: $model.harness) {
                    ForEach(model.availableHarnesses, id: \.self) { name in
                        Text(name).tag(name)
                    }
                }

                if model.supportsInteractiveAuth {
                    Button {
                        showAuth = true
                    } label: {
                        Label("Sign in to \(model.harness)", systemImage: "person.badge.key")
                    }
                    .disabled(model.hostID.isEmpty)
                }
            }

            Section("Working directory") {
                HStack {
                    TextField("cwd (optional)", text: $model.cwd)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    if !model.cwdSuggestions.isEmpty {
                        Menu {
                            ForEach(model.cwdSuggestions, id: \.self) { suggestion in
                                Button(suggestion) { model.cwd = suggestion }
                            }
                        } label: {
                            Image(systemName: "clock.arrow.circlepath")
                        }
                    }
                }
            }

            if model.isStructured {
                Section("Starting prompt") {
                    TextEditor(text: $model.prompt)
                        .frame(minHeight: 100)
                }
            }

            if let launchError = model.launchError {
                Section {
                    Text(launchError).foregroundStyle(.red)
                }
            }

            Section {
                Button {
                    Task { await launch() }
                } label: {
                    if isLaunching {
                        ProgressView()
                    } else {
                        Text("Launch")
                    }
                }
                .disabled(isLaunching || model.hostID.isEmpty || model.harness.isEmpty)
                .accessibilityIdentifier("launch-confirm-button")
            }
        }
        .navigationTitle("New Session")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }
            }
        }
        .sheet(isPresented: $showAuth) {
            NavigationStack {
                HarnessAuthSheet(client: client, hostID: model.hostID, harness: model.harness)
            }
        }
    }

    private func launch() async {
        isLaunching = true
        defer { isLaunching = false }
        guard let sessionID = await model.launch() else { return }
        onLaunched(sessionID, model.isStructured)
        dismiss()
    }
}
