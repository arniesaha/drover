import DroverKit
import SwiftUI

struct ActivitySummarySection: View {
    let activity: ActivitySummary
    let statusMessage: String?
    let onOpenAnalytics: () -> Void
    /// `GridItem.adaptive` measures against a fixed point width, so it has no
    /// idea the text grew — at accessibility sizes it still packed three
    /// columns and crushed every label into a stack of syllables. Scaling the
    /// minimum with the type size is what makes the grid actually reflow.
    @ScaledMetric(relativeTo: .title3) private var metricMinimum: CGFloat = 104

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CockpitSectionHeading(
                title: "Recent activity", source: "Drover observed", action: onOpenAnalytics
            )
            CockpitCard {
                VStack(alignment: .leading, spacing: 9) {
                    // A grid rather than a fixed three-column row: at
                    // accessibility sizes the labels wrapped into stacks of
                    // single words. This reflows to two-up, then one.
                    LazyVGrid(
                        columns: [GridItem(.adaptive(minimum: metricMinimum), alignment: .leading)],
                        alignment: .leading,
                        spacing: 12
                    ) {
                        ActivityMetric(
                            value: format(activity.totals.sessionCount),
                            label: "Observed sessions"
                        )
                        // Abbreviated: the exact figure is nine digits, a
                        // shape you count rather than see, and it crowded the
                        // third metric off the row. Analytics still prints it
                        // in full, where the number *is* the content.
                        ActivityMetric(
                            value: CompactNumber.abbreviated(activity.totals.totalTokens),
                            label: "Tokens",
                            spokenValue: "\(format(activity.totals.totalTokens)) tokens"
                        )
                        // "API cost" next to a token count reads as "this is
                        // what those tokens cost", which is false: subscription
                        // usage reports no per-token cost, so this is only the
                        // API-billed slice — and its coverage is far lower than
                        // the token coverage printed below. See #150.
                        ActivityMetric(
                            value: currency(activity.totals.costUSD), label: "API-billed"
                        )
                    }
                    Text(coverageText)
                        .droverText(.subtitle)
                        .fixedSize(horizontal: false, vertical: true)
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

    /// Coverage for both figures, not just tokens.
    ///
    /// Cost coverage is routinely far lower than token coverage — 5% against
    /// 5.8% in the case that prompted this — and printing only the token
    /// figure left the *less* covered number looking like a total. Only says
    /// it twice when the two actually differ.
    private var coverageText: String {
        guard let tokens = activity.coverage.tokenPercent else {
            return "Token coverage unavailable"
        }
        let tokenText = "\(formatPercent(tokens))% token coverage"
        guard let cost = activity.coverage.costPercent,
              formatPercent(cost) != formatPercent(tokens)
        else { return tokenText }
        return "\(tokenText) · \(formatPercent(cost))% cost coverage"
    }
}

private struct ActivityMetric: View {
    let value: String
    let label: String
    /// What VoiceOver reads when the shown value is abbreviated. "63.1M" is a
    /// glance aid for sighted scanning; it should not be the only form of the
    /// number available.
    var spokenValue: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            // One line, always: "63.1M" wrapping into "63.1" / "M" is not a
            // smaller number, it is a broken one.
            Text(value).droverText(.h2).monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text(label).droverText(.subtitle)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(spokenValue ?? value) \(label)")
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
