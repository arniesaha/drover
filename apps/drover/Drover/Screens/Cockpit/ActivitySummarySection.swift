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
        let totals = ActivityTotalsPresentation(
            totals: activity.totals, coverage: activity.coverage
        )
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
                        // the token coverage printed below. The wording, and
                        // the zero-versus-unmeasured rule behind it, live in
                        // DroverKit so the analytics screen cannot drift from
                        // this card again. See #150.
                        ActivityMetric(
                            value: totals.costText,
                            label: ActivityTotalsPresentation.costLabel,
                            accessibilityText: totals.costAccessibilityText
                        )
                    }
                    Text(totals.coverageText)
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
}

private struct ActivityMetric: View {
    let value: String
    let label: String
    /// What VoiceOver reads when the shown value is abbreviated. "63.1M" is a
    /// glance aid for sighted scanning; it should not be the only form of the
    /// number available.
    var spokenValue: String?
    /// Replaces the whole spoken phrase, for values that are not a quantity of
    /// the label: "Not reported API-billed" is not a sentence, and reading the
    /// cost slot as a number at all is exactly the claim #150 is about.
    var accessibilityText: String?

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
        .accessibilityLabel(accessibilityText ?? "\(spokenValue ?? value) \(label)")
    }
}

func format(_ value: Int) -> String {
    value.formatted(.number.grouping(.automatic))
}
