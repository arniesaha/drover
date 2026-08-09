import DroverKit
import SwiftUI

struct ActivitySummarySection: View {
    let activity: ActivitySummary
    let statusMessage: String?
    let onOpenAnalytics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CockpitSectionHeading(
                title: "Recent activity", source: "Drover observed", action: onOpenAnalytics
            )
            CockpitCard {
                VStack(alignment: .leading, spacing: 9) {
                    HStack(alignment: .top, spacing: 16) {
                        ActivityMetric(value: format(activity.totals.sessionCount), label: "Sessions")
                        ActivityMetric(value: format(activity.totals.totalTokens), label: "Tokens")
                        ActivityMetric(value: currency(activity.totals.costUSD), label: "API cost")
                    }
                    Text(coverageText)
                        .droverText(.subtitle)
                    if let statusMessage {
                        Text(statusMessage)
                            .droverText(.subtitle)
                            .foregroundStyle(DroverColor.accentHi)
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("activity-summary-section")
    }

    private var coverageText: String {
        guard let coverage = activity.coverage.tokenPercent else {
            return "Token coverage unavailable"
        }
        return "\(formatPercent(coverage))% token coverage"
    }
}

private struct ActivityMetric: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).droverText(.h2).monospacedDigit()
            Text(label).droverText(.subtitle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

func format(_ value: Int) -> String {
    value.formatted(.number.grouping(.automatic))
}

func formatPercent(_ value: Double) -> String {
    value.formatted(.number.precision(.fractionLength(0...1)))
}

func currency(_ value: Double) -> String {
    value.formatted(.currency(code: "USD").precision(.fractionLength(2)))
}
