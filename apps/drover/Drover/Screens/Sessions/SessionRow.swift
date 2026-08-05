import SwiftUI
import NexusKit

/// One row in `SessionsView`: session snippet, compact metadata, and a
/// relative timestamp. Bucket/status logic lives on `SessionSummary` and
/// `SessionStore`; card copy derivation lives in NexusKit.
struct SessionRow: View {
    let session: SessionSummary
    let hostTitle: String
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        let card = SessionCardPresentation(session: session, hostTitle: hostTitle)
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: card.harness.symbolName)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(attentionTint)
                .frame(width: 30, height: 30)
                .background(attentionTint.opacity(0.13), in: Circle())
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .top, spacing: 8) {
                    Text(card.title)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                        .layoutPriority(1)

                    Spacer(minLength: 4)

                    if let lastActivity = session.lastActivity {
                        Text(lastActivity, format: .relative(presentation: .numeric))
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.82)
                            .frame(minWidth: 58, alignment: .trailing)
                            .padding(.top, 1)
                    }
                }

                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Circle()
                        .fill(attentionTint)
                        .frame(width: 6, height: 6)

                    Text(card.metadataText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(card.metadataText)
            }
        }
        .padding(15)
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
}
