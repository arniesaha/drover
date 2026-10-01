import DroverKit
import SwiftUI

/// A bounded preview in the pinned inbox header. All detail lives on Accounts.
struct ProviderCapacitySection: View {
    let accounts: [ProviderAccount]
    let status: DataStatus
    let statusMessage: String?
    var hostTitles: [String: String] = [:]
    let onOpenAccounts: () -> Void
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let preview = ProviderCapacityPreview(
            subscriptions: ProviderSubscriptionGrouping.group(accounts, hostTitles: hostTitles)
        )
        let degraded = status != .ok || statusMessage != nil
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(typeSize.isAccessibilitySize ? "\(preview.accountCount) accounts" : "Accounts · \(preview.accountCount)").droverText(.body)
                if preview.hasWarnings || degraded {
                    Image(systemName: "clock.badge.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(DroverColor.muted)
                        .accessibilityHidden(true)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundStyle(DroverColor.muted)
                    .accessibilityHidden(true)
            }
            .accessibilityHidden(true)
            .overlay {
                // Enlarge the navigation target without growing the pinned pane.
                Button(action: onOpenAccounts) {
                    Color.clear.frame(height: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(accessibilityLabel(preview, degraded: degraded))
                .accessibilityHint("Open all accounts, quota windows and host reporting details")
                .accessibilityIdentifier("provider-capacity-navigation")
            }

            if typeSize.isAccessibilitySize {
                Button(action: onOpenAccounts) {
                    Text(preview.meters.first.map { "Lowest \($0.remainingText)" } ?? "Capacity unavailable")
                        .droverText(.subtitle)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(preview.meters.first.map { meterLabel($0, degraded: degraded) } ?? "Capacity unavailable")
                .accessibilityIdentifier("lowest-account-meter")
            } else if preview.meters.isEmpty {
                Text(degraded ? "Capacity unavailable · View details" : "No reported accounts")
                    .droverText(.subtitle)
            } else {
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 16) {
                        ForEach(preview.meters) { meter in
                            Button(action: onOpenAccounts) {
                                meterView(meter, degraded: degraded)
                                    .frame(minHeight: 44)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(meterLabel(meter, degraded: degraded))
                            .accessibilityHint("Open all account details")
                            .accessibilityIdentifier("account-meter-\(meter.id)")
                        }
                    }
                }
                .scrollIndicators(.hidden)
                .accessibilityIdentifier("account-meter-strip")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10))
        .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(DroverColor.line, lineWidth: 1) }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("provider-capacity-summary")
    }

    private func meterLabel(_ meter: ProviderCapacityPreview.Meter, degraded: Bool) -> String {
        var label = "\(meter.accountLabel), \(meter.providerTitle), \(meter.remainingText)"
        if meter.isStale || degraded { label += ", stale" }
        return label
    }

    private func accessibilityLabel(_ preview: ProviderCapacityPreview, degraded: Bool) -> String {
        var parts = ["Accounts, \(preview.accountCount) accounts"]
        for meter in preview.meters.prefix(3) {
            parts.append(meterLabel(meter, degraded: degraded))
        }
        if preview.accountCount > 3 {
            parts.append("\(preview.accountCount - 3) additional accounts")
        }
        if preview.hasWarnings || degraded { parts.append("Some usage readings need refresh") }
        return parts.joined(separator: ". ")
    }

    private func meterView(_ meter: ProviderCapacityPreview.Meter, degraded: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 3) {
                Text(meter.accountLabel).droverText(.subtitle).lineLimit(1).truncationMode(.middle)
                if meter.isStale || degraded {
                    Image(systemName: "clock").font(.caption2).foregroundStyle(DroverColor.muted)
                }
            }
            Text("\(meter.providerTitle) · \(meter.remainingText)").droverText(.subtitle).monospacedDigit().lineLimit(1)
            CapacityBar(fraction: meter.remainingFraction, height: 4)
                .opacity(meter.isStale || degraded ? 0.45 : 1)
        }
        .frame(width: 180, alignment: .leading)
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

                if !subscription.staleHosts.isEmpty {
                    DisclosureGroup {
                        ForEach(subscription.staleHosts) { host in
                            Label(host.title + " · stale", systemImage: "clock")
                                .droverText(.subtitle)
                                .accessibilityLabel(host.title + ", stale")
                        }
                    } label: {
                        // Several accounts can each have one; name the account
                        // so VoiceOver (and UI tests) can tell them apart.
                        Text("Stale hosts · \(subscription.staleHosts.count)")
                            .accessibilityLabel("Stale hosts · \(subscription.staleHosts.count), \(subscription.accountLabel)")
                    }
                    .accessibilityIdentifier("stale-hosts-\(subscription.id)")
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
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("provider-account-\(subscription.id)")
    }

}

struct CockpitSectionHeading: View {
    let title: String
    let source: String?
    let action: (() -> Void)?

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
