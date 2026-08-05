import SwiftUI
import NexusKit

/// One collapsed row per status run (consecutive status messages, grouped by
/// `TranscriptItem.group`). Deliberately recessive and styled to match
/// `ThinkingBlock` so every fold in the transcript reads as one family.
/// Purely presentational — the labels come from `SessionEventSummary`.
struct SessionEventsRow: View {
    let run: [HarnessMessage]
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gearshape")
                    Text(SessionEventSummary.title(for: run))
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("session-events-row")
            .accessibilityLabel(isExpanded ? "Collapse session events" : "Expand session events")

            if isExpanded {
                HStack(alignment: .top, spacing: 10) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(.secondary.opacity(0.35))
                        .frame(width: 2)
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(run) { message in
                            Text(SessionEventSummary.detail(for: message))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
