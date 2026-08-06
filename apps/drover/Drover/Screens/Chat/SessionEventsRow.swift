import SwiftUI
import DroverKit

/// A status run as a fold: `3 status updates · last: indexed 1,204 files`.
/// Status messages are ~48% of a real transcript and individually meaningless,
/// so they collapse hardest — but the last one usually says something worth
/// seeing, which is why it rides the summary line.
struct SessionEventsRow: View {
    let run: [HarnessMessage]

    var body: some View {
        FoldRow(
            systemImage: "waveform",
            summary: FoldSummary.status(run: run),
            accessibilityIdentifier: "session-events-row"
        ) {
            ForEach(run) { message in
                Text(SessionEventSummary.detail(for: message))
                    .droverText(.nested)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}
