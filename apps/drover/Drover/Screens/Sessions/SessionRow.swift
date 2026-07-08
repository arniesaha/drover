import SwiftUI
import NexusKit

/// One row in `SessionsView`: harness icon, cwd (last path component), host
/// badge, and a relative timestamp. All formatting/derivation here is purely
/// presentational — the underlying bucket/status logic lives on
/// `SessionSummary`/`SessionStore`.
struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: harnessSymbolName)
                .foregroundStyle(attentionTint)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 2) {
                Text(cwdLastComponent)
                    .font(.body)
                    .lineLimit(1)

                HStack(spacing: 6) {
                    Text(session.hostID)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.secondary.opacity(0.15), in: Capsule())

                    if let lastActivity = session.lastActivity {
                        Text(lastActivity, format: .relative(presentation: .named))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Spacer()
        }
        .padding(.vertical, 2)
    }

    private var harnessSymbolName: String {
        switch session.harness {
        case "claude-code": "brain"
        case "codex": "chevron.left.forwardslash.chevron.right"
        case "gemini": "sparkles"
        default: "terminal"
        }
    }

    private var attentionTint: Color {
        switch session.attention {
        case .needsApproval: .orange
        case .needsInput: .blue
        case .working: .green
        case .done: .gray
        case .errored: .red
        }
    }

    private var cwdLastComponent: String {
        guard let cwd = session.cwd, !cwd.isEmpty else { return session.id }
        return URL(fileURLWithPath: cwd).lastPathComponent
    }
}
