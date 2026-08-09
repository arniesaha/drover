import DroverKit
import SwiftUI

struct ProviderCapacitySection: View {
    let accounts: [ProviderAccount]
    let status: DataStatus
    let statusMessage: String?
    let onOpenAnalytics: () -> Void

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
                        ForEach(accounts, id: \.snapshotID) { account in
                            ProviderAccountCard(account: account, section: section)
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
    let account: ProviderAccount
    let section: ProviderSectionPresentation

    var body: some View {
        CockpitCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(account.accountLabel).droverText(.h2)
                        Text([account.provider.capitalized, account.planLabel]
                            .compactMap { $0 }.joined(separator: " · "))
                            .droverText(.subtitle)
                    }
                    Spacer(minLength: 8)
                    Text(statusTitle)
                        .droverText(.marker)
                }

                if account.windows.isEmpty {
                    Text("Usage unavailable")
                        .droverText(.body)
                } else {
                    ForEach(Array(account.windows.enumerated()), id: \.offset) { _, window in
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

            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityIdentifier("provider-account-\(account.snapshotID)")
    }

    private var statusTitle: String {
        section.accountStatusText(accountStatus: account.status)
    }

    private var accessibilityLabel: String {
        let windows = account.windows.map {
            let value = ProviderCapacityPresentation(account: account, window: $0, now: .now)
            return [
                $0.kind,
                value.usedText,
                value.remainingText,
                value.resetText,
                value.freshnessText,
            ].compactMap { $0 }.joined(separator: ", ")
        }.joined(separator: ". ")
        return "\(account.provider), \(account.accountLabel), Provider reported, \(statusTitle). \(windows)"
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
