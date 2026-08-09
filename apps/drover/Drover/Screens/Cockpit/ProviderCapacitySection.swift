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
                            ProviderAccountCard(subscription: subscription, section: section)
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

private struct ProviderAccountCard: View {
    let subscription: ProviderSubscriptionPresentation
    let section: ProviderSectionPresentation

    private var account: ProviderAccount { subscription.representative }

    var body: some View {
        CockpitCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(subscription.accountLabel).droverText(.h2)
                        Text([subscription.provider.capitalized, subscription.planLabel]
                            .compactMap { $0 }.joined(separator: " · "))
                            .droverText(.subtitle)
                    }
                    Spacer(minLength: 8)
                    Text(statusTitle)
                        .droverText(.marker)
                }

                if subscription.windows.isEmpty {
                    Text("Usage unavailable")
                        .droverText(.body)
                } else {
                    ForEach(Array(subscription.windows.enumerated()), id: \.offset) { _, window in
                        let value = ProviderCapacityPresentation(
                            account: account, window: window, now: .now
                        )
                        VStack(alignment: .leading, spacing: 3) {
                            Text(window.kind.replacingOccurrences(of: "_", with: " ").capitalized)
                                .droverText(.h3)
                            Text(value.remainingText).droverText(.body)
                            Text("\(value.usedText) · \(value.resetText)")
                                .droverText(.subtitle)
                            if let freshness = value.freshnessText {
                                Text(freshness).droverText(.subtitle)
                            }
                        }
                    }
                }

                // The hosts this one subscription covers, and — when a probe
                // failed on one of them — which host and why. A single broken
                // probe belongs on its own card, not in a banner over the
                // whole section.
                Text(subscription.hostsText)
                    .droverText(.subtitle)
                    .foregroundStyle(DroverColor.faint)

                if let reason = subscription.reasonText {
                    Label(reason, systemImage: "exclamationmark.triangle")
                        .droverText(.subtitle)
                        .foregroundStyle(DroverColor.accentHi)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("provider-account-reason")
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityIdentifier("provider-account-\(subscription.id)")
    }

    private var statusTitle: String {
        section.accountStatusText(accountStatus: subscription.status)
    }

    private var accessibilityLabel: String {
        let windows = subscription.windows.map {
            let value = ProviderCapacityPresentation(account: account, window: $0, now: .now)
            return [
                $0.kind,
                value.usedText,
                value.remainingText,
                value.resetText,
                value.freshnessText,
            ].compactMap { $0 }.joined(separator: ", ")
        }.joined(separator: ". ")
        return [
            "\(subscription.provider), \(subscription.accountLabel), Provider reported, \(statusTitle)",
            subscription.hostsText,
            subscription.reasonText,
            windows,
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
