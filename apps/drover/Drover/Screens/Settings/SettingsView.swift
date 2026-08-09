import SwiftUI
import DroverKit

/// Server URL + token onboarding/reconfiguration. The token field is never
/// pre-filled (Keychain contents don't round-trip into the UI); a caption
/// tells the user whether one is already configured. "Test & Save" validates
/// against the live server via `AppEnvironment.configure` before persisting
/// anything.
struct SettingsView: View {
    var environment: AppEnvironment
    @Environment(\.dismiss) private var dismiss

    @State private var urlString = ""
    @State private var token = ""
    @State private var isValidating = false
    @State private var statusMessage: String?
    @State private var statusIsError = false

    var body: some View {
        Form {
            Section("Server") {
                TextField("http://host:7080", text: $urlString)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }

            Section("Token") {
                SecureField("API token", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    // A shared cluster bearer token is not a website password;
                    // .oneTimeCode keeps iOS from offering to "Save Password"
                    // (and from autofilling unrelated Keychain entries) every
                    // time the user reconfigures.
                    .textContentType(.oneTimeCode)
                if environment.hasTokenConfigured {
                    Text("Token configured ✓")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if let statusMessage {
                Section {
                    Text(statusMessage)
                        .foregroundStyle(statusIsError ? .red : .green)
                }
            }

            if let client = environment.client {
                ContentAnalysisSettings(client: client)
            }

            Section {
                Button {
                    Task { await testAndSave() }
                } label: {
                    if isValidating {
                        ProgressView()
                    } else {
                        Text("Test & Save")
                    }
                }
                .disabled(isValidating || urlString.isEmpty || token.isEmpty)
            }
        }
        .navigationTitle("Settings")
        .onAppear {
            if urlString.isEmpty, let config = environment.config {
                urlString = config.baseURL.absoluteString
            }
        }
    }

    private func testAndSave() async {
        isValidating = true
        statusMessage = nil
        let outcome = await environment.configure(urlString: urlString, token: token)
        isValidating = false
        switch outcome {
        case .success:
            statusIsError = false
            statusMessage = "Saved."
            token = ""
            dismiss()
        case .failure(let message):
            statusIsError = true
            statusMessage = message
        }
    }
}
