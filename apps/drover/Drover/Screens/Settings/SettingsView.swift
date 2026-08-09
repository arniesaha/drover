import SwiftUI
import DroverKit

/// Server URL + token onboarding/reconfiguration. The token field is never
/// pre-filled (Keychain contents don't round-trip into the UI); a caption
/// tells the user whether one is already configured. "Test & Save" validates
/// against the live server via `AppEnvironment.configure` before persisting
/// anything.
///
/// This is the first screen a new install sees, so it is drawn in the app's
/// own palette rather than in a system `Form`: grouped-list chrome brought its
/// own greys and its own accent, which meant onboarding looked like a
/// different product from the inbox it hands you to. Every colour here
/// resolves through `DroverColor`, so the screen follows the theme toggle like
/// everything else.
struct SettingsView: View {
    var environment: AppEnvironment
    @Environment(\.dismiss) private var dismiss

    @State private var urlString = ""
    @State private var token = ""
    @State private var isValidating = false
    @State private var statusMessage: String?
    @State private var statusIsError = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                field(label: "Server", hint: "Where drover-server is listening.") {
                    TextField("http://host:7080", text: $urlString)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                }

                field(label: "Token", hint: tokenHint) {
                    SecureField("API token", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        // A shared cluster bearer token is not a website
                        // password; .oneTimeCode keeps iOS from offering to
                        // "Save Password" (and from autofilling unrelated
                        // Keychain entries) every time the user reconfigures.
                        .textContentType(.oneTimeCode)
                }

                if let statusMessage {
                    statusRow(statusMessage)
                }

                saveButton
            }
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 28)
        }
        .background(DroverColor.bg)
        .scrollDismissesKeyboard(.interactively)
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(DroverColor.bg, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .onAppear {
            if urlString.isEmpty, let config = environment.config {
                urlString = config.baseURL.absoluteString
            }
        }
    }

    // MARK: - Pieces

    private var tokenHint: String {
        environment.hasTokenConfigured
            ? "A token is already saved. Typing a new one replaces it."
            : "Stored in the Keychain, never shown again."
    }

    /// One labelled input: small-caps label, the field on its own lifted
    /// ground, then the quiet line that says what it is for. The field itself
    /// is mono — a URL and a bearer token are machine strings, and the type
    /// ramp already says those are set that way.
    private func field(
        label: String,
        hint: String,
        @ViewBuilder content: () -> some View
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).droverText(.h3)

            content()
                .font(.system(.subheadline, design: .monospaced))
                .foregroundStyle(DroverColor.text)
                .padding(.horizontal, 13)
                .padding(.vertical, 12)
                .background(DroverColor.surface,
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(DroverColor.line, lineWidth: 1)
                }

            Text(hint)
                .droverText(.subtitle)
                .foregroundStyle(DroverColor.faint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Both outcomes share the inbox's transient-row shape — glyph, one line,
    /// accent tint — because the palette has exactly one accent and states are
    /// carried by form. The glyph is what separates "saved" from "rejected".
    private func statusRow(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: statusIsError ? "exclamationmark.circle" : "checkmark.circle")
                .font(.system(size: 13, weight: .medium))
            Text(message)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .droverText(.subtitle)
        .foregroundStyle(DroverColor.accentHi)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.accentTint,
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .accessibilityIdentifier(statusIsError ? "settings-error" : "settings-saved")
    }

    /// The same outlined primary the inbox uses for "New Session", so the
    /// first button a new install taps is the shape every later one has.
    private var saveButton: some View {
        Button {
            Task { await testAndSave() }
        } label: {
            Group {
                if isValidating {
                    ProgressView()
                } else {
                    Text("Test & Save")
                }
            }
            .font(.system(.subheadline, design: .default, weight: .medium))
            .foregroundStyle(canSave ? AnyShapeStyle(DroverColor.accentHi)
                                     : AnyShapeStyle(DroverColor.faint))
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
            .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(canSave ? AnyShapeStyle(DroverColor.accent)
                                          : AnyShapeStyle(DroverColor.line),
                                  lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .disabled(!canSave)
        .accessibilityIdentifier("settings-save")
    }

    private var canSave: Bool {
        !isValidating && !urlString.isEmpty && !token.isEmpty
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
