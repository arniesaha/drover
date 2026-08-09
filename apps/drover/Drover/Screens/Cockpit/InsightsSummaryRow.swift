import DroverKit
import SwiftUI

struct InsightsSummaryRow: View {
    let counts: InsightCounts?
    let onOpenInsights: () -> Void

    var body: some View {
        Button(action: onOpenInsights) {
            CockpitCard {
                HStack(spacing: 12) {
                    Image(systemName: "lightbulb.max")
                        .foregroundStyle(DroverColor.accentHi)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Configuration insights").droverText(.h2)
                        Text(summary).droverText(.subtitle)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right")
                        .font(.caption)
                        .foregroundStyle(DroverColor.faint)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Configuration insights. \(summary)")
        .accessibilityIdentifier("insights-summary")
    }

    private var summary: String {
        guard let counts else { return "Counts temporarily unavailable" }
        let severe = counts.critical + counts.high
        let total = severe + counts.medium + counts.low
        return severe > 0 ? "\(severe) high priority · \(total) open" : "\(total) open"
    }
}
