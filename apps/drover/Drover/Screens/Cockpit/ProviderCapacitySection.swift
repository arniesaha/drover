import DroverKit
import SwiftUI

struct ProviderCapacitySection: View {
    let accounts: [ProviderAccount]
    let status: DataStatus
    let statusMessage: String?
    /// Host id → display title, so a merged card can name the machines it
    /// covers. Falls back to the raw id when the fleet snapshot is unavailable.
    var hostTitles: [String: String] = [:]
    let onOpenAnalytics: () -> Void

    /// Collapsed by default, and remembered across launches.
    ///
    /// The strip is pinned above the inbox (#80), so its height is taken from
    /// the session list on every screen, forever. Expanded it ran to roughly a
    /// quarter of the viewport — worth it while you are deciding where to send
    /// work, dead weight the rest of the time. Collapsed it keeps the one line
    /// that answers "what have I got left"; the cards are one tap away.
    @AppStorage("inbox.providerCapacityExpanded") private var isExpanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var subscriptions: [ProviderSubscriptionPresentation] {
        ProviderSubscriptionGrouping.group(accounts, hostTitles: hostTitles)
    }

    var body: some View {
        let section = ProviderSectionPresentation(
            status: status,
            message: statusMessage,
            hasRetainedValues: !accounts.isEmpty
        )
        VStack(alignment: .leading, spacing: 10) {
            CockpitSectionHeading(
                title: "Provider capacity",
                source: "Provider reported",
                action: accounts.isEmpty ? nil : onOpenAnalytics,
                disclosure: accounts.isEmpty
                    ? nil
                    : .init(isExpanded: isExpanded) {
                        withAnimation(reduceMotion ? nil : .snappy(duration: 0.2)) { isExpanded.toggle() }
                    }
            )

            // A failed probe is not something to hide behind a chevron: it
            // explains numbers that are missing or stale, so it shows in both
            // states.
            if let warning = section.warningText {
                CockpitCard {
                    Label(warning, systemImage: "gauge.with.dots.needle.33percent")
                        .droverText(.nested)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityIdentifier("provider-capacity-warning")
            }

            if !accounts.isEmpty {
                if isExpanded {
                    VStack(spacing: 8) {
                        ForEach(subscriptions) { subscription in
                            ProviderAccountCard(subscription: subscription, section: section)
                        }
                    }
                } else {
                    collapsedSummary
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("provider-capacity-section")
    }

    private var collapsedSummary: some View {
        let summary = ProviderCapacitySummary(subscriptions: subscriptions)
        return Button {
            withAnimation(reduceMotion ? nil : .snappy(duration: 0.2)) { isExpanded = true }
        } label: {
            Text(summary.text)
                .droverText(.subtitle, accented: summary.isCritical)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
        }
            .buttonStyle(.plain)
            .accessibilityIdentifier("provider-capacity-summary")
    }
}

/// One subscription, with host probe states and quota detail on demand.
struct ProviderAccountCard: View {
    let subscription: ProviderSubscriptionPresentation
    let section: ProviderSectionPresentation
    @State private var isExpanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        CockpitCard {
            VStack(alignment: .leading, spacing: 8) {
                Button {
                    withAnimation(reduceMotion ? nil : .snappy(duration: 0.2)) {
                        isExpanded.toggle()
                    }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(subscription.accountLabel)
                                .droverText(.body)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            Text([subscription.provider.capitalized, subscription.planLabel]
                                .compactMap { $0 }.joined(separator: " · "))
                                .droverText(.subtitle)
                        }
                        Spacer(minLength: 4)
                        if subscription.isDegraded || section.warningText != nil {
                            Image(systemName: "clock.badge.exclamationmark")
                                .foregroundStyle(DroverColor.muted)
                        }
                        Image(systemName: "chevron.down")
                            .font(.caption)
                            .rotationEffect(.degrees(isExpanded ? 180 : 0))
                            .foregroundStyle(DroverColor.accentHi)
                    }
                    .frame(minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(subscription.title), \(section.accountStatusText(accountStatus: subscription.status))")
                .accessibilityHint(isExpanded ? "Collapse quota details" : "Expand quota details")

                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(subscription.hosts) { host in
                        Label(host.title, systemImage: host.symbol)
                            .droverText(.subtitle)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 4)
                            .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 8))
                            .accessibilityLabel(host.accessibilityLabel)
                    }
                }

                if isExpanded {
                    Text(subscription.freshnessText).droverText(.subtitle)
                    ForEach(Array(subscription.windows.enumerated()), id: \.offset) { _, window in
                        ProviderWindowRow(account: subscription.representative, window: window)
                    }
                    if subscription.windows.isEmpty {
                        Text(subscription.headline.usedText).droverText(.subtitle)
                    }
                    if let reason = subscription.reasonText {
                        Label(reason, systemImage: "exclamationmark.triangle")
                            .droverText(.subtitle)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    let headline = subscription.headline
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(headline.windowTitle).droverText(.subtitle)
                        Spacer(minLength: 4)
                        Text(headline.remainingText == "Remaining unavailable" ? headline.usedText : headline.remainingText)
                            .droverText(.subtitle, accented: headline.isCritical)
                            .monospacedDigit()
                    }
                    CapacityBar(fraction: headline.fraction.map { 1 - $0 }, isCritical: headline.isCritical)
                }
            }
        }
        .accessibilityIdentifier("provider-account-\(subscription.id)")
    }

}

struct CockpitSectionHeading: View {
    /// An optional expand/collapse affordance. Optional because every other
    /// cockpit section using this heading has nothing to collapse — passing
    /// nil keeps their headings byte-identical to before.
    struct Disclosure {
        let isExpanded: Bool
        let toggle: () -> Void
    }

    let title: String
    let source: String?
    let action: (() -> Void)?
    var disclosure: Disclosure? = nil

    var body: some View {
        // Wraps rather than sharing one line. Three items squeezed side by
        // side hyphenated every heading at accessibility sizes — "BUSIEST
        // PROJECT-S", "RECENT / ACTIVITY", "Drover ob-served" — and a heading
        // broken mid-word is harder to read than one on two lines.
        FlowLayout(spacing: 8, lineSpacing: 4) {
            Text(title).droverText(.h3)
            if let source {
                Text(source).droverText(.subtitle)
            }
            if let action {
                Button("See all", action: action)
                    .font(.system(.caption, design: .default, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .buttonStyle(.plain)
            }
            if let disclosure {
                Button(action: disclosure.toggle) {
                    Image(systemName: "chevron.down")
                        .font(.system(.caption, design: .default, weight: .semibold))
                        .rotationEffect(.degrees(disclosure.isExpanded ? 180 : 0))
                        .foregroundStyle(DroverColor.accentHi)
                        // The chevron alone is well under the 44pt minimum, so
                        // the tap target is padded out rather than left at the
                        // glyph's size.
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(disclosure.isExpanded
                    ? "Collapse provider capacity"
                    : "Expand provider capacity")
                .accessibilityIdentifier("provider-capacity-disclosure")
            }
        }
    }
}

struct CockpitCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
    }
}
