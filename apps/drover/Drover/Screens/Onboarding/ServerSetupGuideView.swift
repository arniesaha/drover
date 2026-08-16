import SwiftUI
import DroverKit

enum ServerSetupMode: String, CaseIterable, Identifiable, Sendable {
    case hub = "Primary Hub"
    case host = "Worker Host"

    var id: String { rawValue }

    var subtitle: String {
        switch self {
        case .hub:
            return "First Machine"
        case .host:
            return "Additional Machine"
        }
    }

    var descriptionText: String {
        switch self {
        case .hub:
            return "Runs `drover-server` to coordinate sessions, manage credentials, and serve this app."
        case .host:
            return "Runs `drover-harnessd` on another Mac, Linux box, or NAS to execute agent sessions."
        }
    }

    var installCommand: String {
        switch self {
        case .hub:
            return "curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash"
        case .host:
            return "curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --join '<hub-url>'"
        }
    }

    var copyButtonLabel: String {
        switch self {
        case .hub:
            return "Copy Command"
        case .host:
            return "Copy Join Command"
        }
    }
}

/// Reusable setup instructions for Primary Hub (first machine) and Worker Host (additional machines).
/// Styled strictly with `DroverColor` and `DroverText`.
struct ServerSetupContent: View {
    @Binding var selectedMode: ServerSetupMode
    var knownServerURL: String? = nil

    @State private var hasCopied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            // Mode selector
            HStack(spacing: 10) {
                ForEach(ServerSetupMode.allCases) { mode in
                    Button {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            selectedMode = mode
                            hasCopied = false
                        }
                    } label: {
                        VStack(spacing: 4) {
                            Text(mode.rawValue)
                                .font(.system(.subheadline, design: .default, weight: .medium))
                                .foregroundStyle(selectedMode == mode ? DroverColor.accentHi : DroverColor.text)
                            Text(mode.subtitle)
                                .droverText(.subtitle)
                                .foregroundStyle(selectedMode == mode ? DroverColor.accentHi : DroverColor.muted)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .padding(.horizontal, 8)
                        .background(
                            selectedMode == mode ? DroverColor.accentTint : DroverColor.surface,
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                        )
                        .overlay {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(
                                    selectedMode == mode ? DroverColor.accent : DroverColor.line,
                                    lineWidth: 1
                                )
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("server-setup-mode-\(mode == .hub ? "hub" : "host")")
                }
            }

            // Description
            VStack(alignment: .leading, spacing: 6) {
                Text("Role").droverText(.h3)
                Text(selectedMode.descriptionText)
                    .droverText(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Install Command Card
            VStack(alignment: .leading, spacing: 8) {
                Text("Install Command").droverText(.h3)

                Text(commandToDisplay)
                    .font(.system(.subheadline, design: .monospaced))
                    .foregroundStyle(DroverColor.text)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }
                    .textSelection(.enabled)
                    .accessibilityIdentifier("server-setup-command-text")
            }

            // Action Buttons (Copy & Share)
            VStack(spacing: 10) {
                Button {
                    copyCommand()
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: hasCopied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 14, weight: .medium))
                        Text(hasCopied ? "Copied!" : selectedMode.copyButtonLabel)
                            .font(.system(.subheadline, design: .default, weight: .medium))
                    }
                    .foregroundStyle(hasCopied ? DroverColor.accentHi : DroverColor.accentHi)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(hasCopied ? DroverColor.accentTint : DroverColor.surface,
                                in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(hasCopied ? DroverColor.accentHi : DroverColor.accentHi, lineWidth: 1)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("server-setup-copy-button")

                ShareLink(item: commandToDisplay) {
                    HStack(spacing: 8) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 14, weight: .medium))
                        Text("Share / AirDrop to Mac")
                            .font(.system(.subheadline, design: .default, weight: .medium))
                    }
                    .foregroundStyle(DroverColor.muted)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("server-setup-share-button")
            }

            // Guidance
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "terminal")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .padding(.top, 2)
                Text("Run the command in your terminal on your machine. When it finishes, it will print a pairing QR code.")
                    .droverText(.nested)
                    .foregroundStyle(DroverColor.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
            .accessibilityIdentifier("server-setup-guidance")
        }
        .onChange(of: selectedMode) { _, _ in
            hasCopied = false
        }
    }

    private var commandToDisplay: String {
        if selectedMode == .host, let knownServerURL, !knownServerURL.isEmpty {
            return "curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --join '\(knownServerURL)'"
        }
        return selectedMode.installCommand
    }

    private func copyCommand() {
        UIPasteboard.general.string = commandToDisplay
        withAnimation(.easeInOut(duration: 0.2)) {
            hasCopied = true
        }
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            withAnimation(.easeInOut(duration: 0.2)) {
                hasCopied = false
            }
        }
    }
}

/// Standalone sheet for viewing the server setup instructions from Settings or elsewhere.
struct ServerSetupGuideView: View {
    @Environment(\.dismiss) private var dismiss
    var knownServerURL: String? = nil

    @State private var selectedMode: ServerSetupMode

    init(initialMode: ServerSetupMode = .hub, knownServerURL: String? = nil) {
        self.knownServerURL = knownServerURL
        _selectedMode = State(initialValue: initialMode)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                ServerSetupContent(
                    selectedMode: $selectedMode,
                    knownServerURL: knownServerURL
                )
            }
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 28)
        }
        .background(DroverColor.bg)
        .navigationTitle("Server Setup Guide")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(DroverColor.bg, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Done") { dismiss() }
                    .accessibilityIdentifier("server-setup-guide-done")
            }
        }
    }
}
