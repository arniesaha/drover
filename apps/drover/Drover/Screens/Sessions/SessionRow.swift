import SwiftUI
import NexusKit

/// One row in `SessionsView`: harness icon, cwd (last path component), host
/// badge, and a relative timestamp. All formatting/derivation here is purely
/// presentational — the underlying bucket/status logic lives on
/// `SessionSummary`/`SessionStore`.
struct SessionRow: View {
    let session: SessionSummary
    let hostTitle: String
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        let presentation = HarnessPresentation(session.harness)
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: presentation.symbolName)
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(attentionTint)
                    .frame(width: 34, height: 34)
                    .background(attentionTint.opacity(0.13), in: Circle())

                VStack(alignment: .leading, spacing: 5) {
                    Text(title)
                        .font(.headline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)

                    if let subtitle {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }

                Spacer(minLength: 8)

                if let lastActivity = session.lastActivity {
                    Text(lastActivity, format: .relative(presentation: .numeric))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            HStack(spacing: 8) {
                metadataLabel(presentation.name, systemImage: presentation.symbolName)
                StatusChip(attention: session.attention)
                metadataLabel(hostTitle, systemImage: "desktopcomputer")
                if let cwdLabel {
                    metadataLabel(cwdLabel, systemImage: "folder")
                }
            }
            .lineLimit(1)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .background(surfaceTint, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .strokeBorder(borderStyle, lineWidth: 1)
        }
        .shadow(color: .black.opacity(colorScheme == .dark ? 0.28 : 0.08), radius: 14, y: 6)
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

    private var title: String {
        if let preview = session.preview?.trimmingCharacters(in: .whitespacesAndNewlines),
           !preview.isEmpty {
            return preview
        }
        return cwdLastComponent
    }

    private var subtitle: String? {
        var parts: [String] = []
        if title != cwdLastComponent, !cwdLastComponent.isEmpty {
            parts.append(cwdLastComponent)
        }
        if let startedAt = session.startedAt {
            parts.append("started \(startedAt.formatted(.relative(presentation: .named)))")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private var cwdLabel: String? {
        guard let cwd = session.cwd, !cwd.isEmpty else { return nil }
        return URL(fileURLWithPath: cwd).lastPathComponent
    }

    private var surfaceTint: some ShapeStyle {
        if colorScheme == .dark {
            return AnyShapeStyle(.black.opacity(0.34))
        }
        return AnyShapeStyle(.white.opacity(0.72))
    }

    private var borderStyle: some ShapeStyle {
        if colorScheme == .dark {
            return AnyShapeStyle(.white.opacity(0.11))
        }
        return AnyShapeStyle(.black.opacity(0.07))
    }

    private func metadataLabel(_ text: String, systemImage: String) -> some View {
        Label(text, systemImage: systemImage)
            .font(.caption2.weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(.secondary.opacity(0.10), in: Capsule())
    }
}
