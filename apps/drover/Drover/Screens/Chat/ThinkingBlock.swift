import SwiftUI
import DroverKit

/// A thinking run as a fold: `Thought for 12s · 1.2K tokens` collapsed, the
/// reasoning behind an accent bar expanded. Recessive on purpose — thinking is
/// context, not content — and it wears the same container as the other two
/// fold species so the transcript reads as one family.
struct ThinkingBlock: View {
    let run: [HarnessMessage]
    /// Running total from the harness's `thinking_tokens` events, folded in
    /// by `TranscriptItem.group`. Nil for harnesses that never report it.
    let estimatedTokens: Int?
    /// The newest run keeps streaming into this row; label it accordingly.
    let isStreaming: Bool

    var body: some View {
        FoldRow(
            systemImage: "brain",
            summary: FoldSummary.thinking(run: run,
                                          estimatedTokens: estimatedTokens,
                                          isStreaming: isStreaming),
            accessibilityIdentifier: "thinking-block",
            isStreaming: isStreaming
        ) {
            HStack(alignment: .top, spacing: 9) {
                RoundedRectangle(cornerRadius: 1)
                    .fill(DroverColor.accentTint)
                    .frame(width: 2)
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(run) { message in
                        Text(message.text)
                            .droverText(.nested)
                            .italic()
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
}
