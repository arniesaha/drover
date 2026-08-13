import SwiftUI
import DroverKit

/// Sign-in for one harness on one host.
///
/// Two shapes, chosen by what the harness's CLI can actually do:
///
/// * **Managed flow** (claude-code, codex) — harnessd runs the login command
///   and reports a URL, a device code, and whether it is waiting to be typed
///   into. `claude auth login` needs the code from the browser pasted back,
///   so a flow reporting `supportsInput` gets a field here; codex's device
///   flow finishes entirely in the browser and gets none.
/// * **Terminal** (agy) — the CLI ships no login command at all, only a
///   full-screen TUI. Nothing here can scrape or answer that, so the sheet
///   hands the user a real PTY session instead of a flow that could only
///   fail with "bubbletea: error opening TTY".
struct HarnessAuthSheet: View {
    @State private var model: AuthFlowModel
    @State private var terminalSession: TerminalSession?
    @State private var isOpeningTerminal = false
    @State private var terminalError: String?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    private let client: DroverClient

    /// `navigationDestination(item:)` needs an `Identifiable`; a bare session
    /// id string is not one.
    private struct TerminalSession: Identifiable, Hashable {
        let id: String
    }

    init(client: DroverClient, hostID: String, harness: String) {
        self.client = client
        _model = State(initialValue: AuthFlowModel(client: client, hostID: hostID, harness: harness))
    }

    var body: some View {
        @Bindable var model = model

        Form {
            Section("Harness") {
                LabeledContent("Host", value: model.hostID)
                LabeledContent("Harness", value: model.harness)
                LabeledContent("Status", value: model.flow?.state.rawValue ?? model.status?.state.rawValue ?? "unknown")
            }

            if model.requiresTerminalSignIn {
                terminalSignInSection
            } else {
                flowSection(model: model)
                actionSection
            }

            if let error = model.errorMessage {
                Section {
                    Text(error).foregroundStyle(.red)
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
        .navigationDestination(item: $terminalSession) { session in
            TerminalScreen(client: client, sessionID: session.id, harness: model.harness)
        }
        .task { await model.refreshStatus() }
    }

    // MARK: - Terminal sign-in

    @ViewBuilder
    private var terminalSignInSection: some View {
        Section("Sign In") {
            Text("\(model.harness) signs in through its own full-screen interface. Open a terminal session and follow the prompts there.")
                .font(.footnote)
                .foregroundStyle(.secondary)

            Button {
                Task { await openTerminal() }
            } label: {
                if isOpeningTerminal {
                    ProgressView()
                } else {
                    Label("Open Terminal", systemImage: "terminal")
                }
            }
            .disabled(isOpeningTerminal)
            .accessibilityIdentifier("auth-open-terminal")

            if let terminalError {
                Text(terminalError).foregroundStyle(.red)
            }
        }
    }

    private func openTerminal() async {
        isOpeningTerminal = true
        defer { isOpeningTerminal = false }

        do {
            let sessionID = try await client.createSession(
                hostID: model.hostID, harness: model.harness, mode: "pty",
                prompt: nil, cwd: nil)
            terminalError = nil
            terminalSession = TerminalSession(id: sessionID)
        } catch {
            terminalError = "Could not open a terminal session — \(error)"
        }
    }

    // MARK: - Managed flow

    @ViewBuilder
    private func flowSection(model: AuthFlowModel) -> some View {
        @Bindable var model = model

        if let flow = model.flow {
            Section("Sign In") {
                if let url = flow.loginURL {
                    Button {
                        openURL(url)
                    } label: {
                        Label("Open Browser", systemImage: "safari")
                    }
                    .accessibilityIdentifier("auth-open-browser")
                }
                if let code = flow.userCode ?? flow.deviceCode {
                    LabeledContent("Code", value: code)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("auth-user-code")
                }
                if let message = flow.message {
                    Text(message)
                }
                if let error = flow.lastError {
                    Text(error).foregroundStyle(.red)
                }
            }

            if model.canSubmitCode {
                Section("Paste Code") {
                    Text("Sign in with the browser, then paste the code it gives you.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    TextField("Code", text: $model.codeEntry)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.done)
                        .onSubmit { Task { await model.submitCode() } }
                        .accessibilityIdentifier("auth-code-field")
                    Button {
                        Task { await model.submitCode() }
                    } label: {
                        if model.isSubmitting {
                            ProgressView()
                        } else {
                            Label("Submit Code", systemImage: "arrow.right.circle")
                        }
                    }
                    .disabled(model.isSubmitting
                              || model.codeEntry.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("auth-submit-code")
                }
            }
        }
    }

    @ViewBuilder
    private var actionSection: some View {
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
}
