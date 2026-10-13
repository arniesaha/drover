import DroverKit
import SwiftUI

struct SessionActivityView: View {
    let activity: SessionActivityPresentation
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: activity.isActive ? "circle.dotted" : "circle")
                    .symbolEffect(.pulse, isActive: activity.isActive && !reduceMotion)
                    .accessibilityHidden(true)
                Text(activity.title)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.primary)
                if activity.isActive, let start = activity.startedAt, start <= Date.now {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(start, style: .timer)
                            .font(.caption.monospaced()).foregroundStyle(.secondary)
                            .monospacedDigit()
                            .fixedSize(horizontal: true, vertical: true)
                            .accessibilityLabel("Elapsed time")
                            .accessibilityValue(FoldSummary.duration(max(0, context.date.timeIntervalSince(start))))
                    }
                }
            }
            if let detail = activity.detail, !detail.isEmpty {
                Text(detail).font(.caption.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            if activity.isActive {
                FlowLayout(spacing: 8, lineSpacing: 4) {
                    if activity.completedSteps > 0 {
                        Label("\(activity.completedSteps) steps completed", systemImage: "checkmark")
                    }
                    if let updated = activity.updatedAt {
                        HStack(spacing: 4) {
                            Text("Last event")
                            Text(updated, format: .relative(presentation: .numeric))
                        }
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .fixedSize(horizontal: false, vertical: true)
        .background(.ultraThinMaterial.opacity(0.75), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(.secondary.opacity(0.18), lineWidth: 1)
        }
        .accessibilityIdentifier("chat-activity")
    }
}
