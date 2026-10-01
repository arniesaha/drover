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
                Text(activity.title).droverText(.nested, accented: activity.isActive)
                Spacer(minLength: 4)
                if activity.isActive, let start = activity.startedAt, start <= Date.now {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(start, style: .timer).droverText(.mono).monospacedDigit()
                            .accessibilityLabel("Elapsed time")
                            .accessibilityValue(FoldSummary.duration(max(0, context.date.timeIntervalSince(start))))
                    }
                }
            }
            if let detail = activity.detail, !detail.isEmpty {
                Text(detail).droverText(.mono).lineLimit(1).truncationMode(.middle)
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
                .droverText(.subtitle)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.surface)
        .accessibilityIdentifier("chat-activity")
    }
}
