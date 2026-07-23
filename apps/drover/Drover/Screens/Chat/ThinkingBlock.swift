import SwiftUI
import NexusKit

/// One collapsed row per thinking run (consecutive thinking messages,
/// grouped by `TranscriptItem.group`): a quiet brain-icon caption that
/// expands to the run's text behind a leading accent bar. Deliberately
/// recessive next to real output bubbles — thinking is context, not content.
struct ThinkingBlock: View {
    let run: [HarnessMessage]
    /// The newest run keeps streaming into this row; label it accordingly.
    let isStreaming: Bool
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "brain")
                        .symbolEffect(.pulse, isActive: isStreaming)
                    Text(isStreaming ? "Thinking…" : "Thought for a bit")
                        .italic()
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isExpanded ? "Collapse thinking" : "Expand thinking")

            if isExpanded {
                HStack(alignment: .top, spacing: 10) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(.secondary.opacity(0.35))
                        .frame(width: 2)
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(run) { message in
                            Text(message.text)
                                .font(.callout)
                                .italic()
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
