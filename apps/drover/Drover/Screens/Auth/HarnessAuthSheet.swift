import SwiftUI
import NexusKit

struct HarnessAuthSheet: View {
    @State private var model: AuthFlowModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    init(client: NexusClient, hostID: String, harness: String) {
        _model = State(initialValue: AuthFlowModel(client: client, hostID: hostID, harness: harness))
    }

    var body: some View {
        Form {
            Section("Harness") {
                LabeledContent("Host", value: model.hostID)
                LabeledContent("Harness", value: model.harness)
                LabeledContent("Status", value: model.flow?.state.rawValue ?? model.status?.state.rawValue ?? "unknown")
            }

            if let flow = model.flow {
                Section("Sign In") {
                    if let url = flow.loginURL {
                        Button {
                            openURL(url)
                        } label: {
                            Label("Open Browser", systemImage: "safari")
                        }
                    }
                    if let code = flow.userCode ?? flow.deviceCode {
                        LabeledContent("Code", value: code)
                    }
                    if let message = flow.message {
                        Text(message)
                    }
                    if let error = flow.lastError {
                        Text(error).foregroundStyle(.red)
                    }
                }
            }

            if let error = model.errorMessage {
                Section {
                    Text(error).foregroundStyle(.red)
                }
            }

            Section {
                Button {
                    Task { await model.start() }
                } label: {
                    if model.isStarting {
                        ProgressView()
                    } else {
                        Label("Sign In", systemImage: "person.badge.key")
                    }
                }
                .disabled(model.isStarting)

                if model.flow?.isTerminal == false {
                    Button(role: .destructive) {
                        Task { await model.cancel() }
                    } label: {
                        Label("Cancel", systemImage: "xmark.circle")
                    }
                }
            }
        }
        .navigationTitle("Harness Auth")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Close") { dismiss() }
            }
        }
        .task { await model.refreshStatus() }
    }
}
