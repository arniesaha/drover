import SwiftUI
import NexusKit

/// One row in `SessionsView`: harness icon, cwd (last path component), host
/// badge, and a relative timestamp. All formatting/derivation here is purely
/// presentational — the underlying bucket/status logic lives on
/// `SessionSummary`/`SessionStore`.
struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        let presentation = HarnessPresentation(session.harness)
        HStack(spacing: 12) {
            Image(systemName: presentation.symbolName)
                .foregroundStyle(attentionTint)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 2) {
                Text(cwdLastComponent)
                    .font(.body)
                    .lineLimit(1)

                HStack(spacing: 6) {
                    Text(presentation.name)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.tint.opacity(0.14), in: Capsule())

                    StatusChip(attention: session.attention)

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

    private var attentionTint: Color {
        switch session.attention {
        case .needsApproval: .orange
        case .needsInput: .blue
        case .working: .green
        case .done: .gray
        case .errored: .red
        }
    }

    /// Row title: the cwd's last path component when we have one, else a
    /// human harness label instead of the raw `harness-<uuid>` session id
    /// (which is unreadable and identical-looking across shell sessions).
    private var cwdLastComponent: String {
        if let cwd = session.cwd, !cwd.isEmpty {
            return URL(fileURLWithPath: cwd).lastPathComponent
        }
        switch session.harness {
        case "shell": return "Shell session"
        case "": return "Session"
        default: return "\(session.harness) session"
        }
    }
}
