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
                action: accounts.isEmpty ? nil : onOpenAnalytics
            )

            if let warning = section.warningText {
                CockpitCard {
                    Label(warning, systemImage: "gauge.with.dots.needle.33percent")
                        .droverText(.nested)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityIdentifier("provider-capacity-warning")
            }

            if !accounts.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 10) {
                        ForEach(subscriptions) { subscription in
                            // Every card in the strip is the same card. The row
                            // sizes itself to the tallest and the rest fill it,
                            // which beats a hardcoded height: a two-line
                            // account label or a Dynamic Type bump moves the
                            // tallest card and everyone else follows it.
                            ProviderAccountCard(
                                subscription: subscription,
                                section: section,
                                fillsHeight: true
                            )
                            .frame(width: 250)
                        }
                    }
                }
                .scrollClipDisabled()
            }
        }
        .accessibilityIdentifier("provider-capacity-section")
    }
}

struct ProviderAccountCard: View {
    let subscription: ProviderSubscriptionPresentation
    let section: ProviderSectionPresentation
    /// Set by the strip so every card matches the tallest one. Off by default,
    /// which is what lets the layout tests measure a card's own content.
    var fillsHeight = false

    private var account: ProviderAccount { subscription.representative }

    private var headline: ProviderHeadline { subscription.headline }

    var body: some View {
        CockpitCard(fillsHeight: fillsHeight) {
            VStack(alignment: .leading, spacing: 10) {
                identity
                capacity
                // Pins the footer to the bottom edge once the row has
                // stretched this card to its neighbours' height.
                Spacer(minLength: 0)
                footer
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityIdentifier("provider-account-\(subscription.id)")
    }

    /// Who this subscription is and where it is signed in. The host used to sit
    /// last in the faintest style, which is backwards — it is the field you
    /// read when deciding where to send work.
    private var identity: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(subscription.accountLabel)
                    .droverText(.h2)
                    // Reserved, not merely capped: without it a one-line
                    // address makes a shorter card than a wrapped one, and
                    // the strip goes ragged again.
                    .lineLimit(2, reservesSpace: true)
                Spacer(minLength: 0)
                Text(statusTitle)
                    .droverText(.marker)
                    .lineLimit(1)
            }
            Text([subscription.provider.capitalized, subscription.planLabel]
                .compactMap { $0 }.joined(separator: " · "))
                .droverText(.subtitle)
                .lineLimit(1)
            // The hosts this one subscription covers. Reads at the same weight
            // as the plan above it: it used to be the quietest thing on the
            // card, which is backwards for the field you scan when deciding
            // where to send work.
            Text(subscription.hostsText)
                .droverText(.subtitle)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    /// One window — the one closest to exhaustion — and its bar. The rest of
    /// the windows are on the analytics screen; rendering all of them here is
    /// what made a four-window card stand four times taller than a card with
    /// none.
    private var capacity: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(headline.windowTitle)
                    .droverText(.h3)
                    .lineLimit(1)
                Spacer(minLength: 0)
                Text(headline.usedText)
                    .droverText(.subtitle, accented: headline.isCritical)
                    .lineLimit(1)
            }
            CapacityBar(fraction: headline.fraction, isCritical: headline.isCritical)
            // A space rather than the empty string: an empty `Text` reserves a
            // line a third of a point shorter than a laid-out one, which is
            // enough to make the no-window card measurably shorter than its
            // neighbours.
            Text(headline.detailText ?? " ")
                .droverText(.subtitle)
                .lineLimit(1, reservesSpace: true)
        }
    }

    /// When a probe failed on one host, which host and why. A single broken
    /// probe belongs on its own card, not in a banner over the whole section.
    private var footer: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let reason = subscription.reasonText {
                Label(reason, systemImage: "exclamationmark.triangle")
                    .droverText(.subtitle)
                    .foregroundStyle(DroverColor.accentHi)
                    .lineLimit(1)
                    .accessibilityIdentifier("provider-account-reason")
            }
            Text(subscription.freshnessText)
                .droverText(.subtitle)
                .lineLimit(1)
        }
    }

    private var statusTitle: String {
        section.accountStatusText(accountStatus: subscription.status)
    }

    /// VoiceOver still hears every window. The card drops the other windows
    /// for space; a screen reader has none of that pressure.
    private var accessibilityLabel: String {
        let windows = subscription.windows.map {
            let value = ProviderCapacityPresentation(account: account, window: $0, now: .now)
            return [
                ProviderWindowTitle.display($0.kind),
                value.usedText,
                value.remainingText,
                value.resetText,
            ].joined(separator: ", ")
        }.joined(separator: ". ")
        return [
            "\(subscription.provider), \(subscription.accountLabel), Provider reported, \(statusTitle)",
            subscription.hostsText,
            subscription.reasonText,
            windows.isEmpty ? headline.usedText : windows,
            subscription.freshnessText,
        ].compactMap { $0 }.joined(separator: ". ")
    }
}

struct CockpitSectionHeading: View {
    let title: String
    let source: String?
    let action: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).droverText(.h3)
            if let source {
                Text(source).droverText(.subtitle)
            }
            Spacer(minLength: 8)
            if let action {
                Button("See all", action: action)
                    .font(.system(.caption, design: .default, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .buttonStyle(.plain)
            }
        }
    }
}

struct CockpitCard<Content: View>: View {
    /// Grow to whatever height is offered rather than to the content's own.
    /// Has to happen inside the card, ahead of the background, or the card
    /// paints itself at its natural height inside a taller frame.
    var fillsHeight = false
    @ViewBuilder let content: Content

    var body: some View {
        content
            .frame(
                maxWidth: .infinity,
                maxHeight: fillsHeight ? .infinity : nil,
                alignment: .leading
            )
            .padding(12)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
    }
}
