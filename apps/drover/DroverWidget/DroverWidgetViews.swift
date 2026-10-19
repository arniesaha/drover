import SwiftUI
import WidgetKit
import DroverKit

// MARK: - Medium Widget (Wide)

public struct MediumWidgetView: View {
    public let entry: DroverWidgetEntry

    public init(entry: DroverWidgetEntry) {
        self.entry = entry
    }

    private var snapshot: WidgetFleetSnapshot {
        entry.snapshot
    }

    private var quotaDisplay: WidgetQuotaDisplay {
        snapshot.quotaDisplay
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("QUOTA")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
                Text("\(snapshot.accounts.count) accounts")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(WidgetColor.muted)
                Spacer(minLength: 4)
                needsYouHeadline
            }

            if quotaDisplay.visibleAccounts.isEmpty {
                Text("No accounts reported")
                    .font(.system(size: 11))
                    .foregroundStyle(WidgetColor.muted)
                    .frame(maxHeight: .infinity, alignment: .center)
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(quotaDisplay.visibleAccounts) { account in
                        WidgetQuotaRow(account: account, fallbackUpdatedText: timeAgoText(from: entry.date))
                    }
                }

                if quotaDisplay.hasTruncatedAccounts {
                    Text("+\(quotaDisplay.truncatedCount) more")
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundStyle(WidgetColor.faint)
                }
            }

            Spacer(minLength: 0)

            Rectangle()
                .fill(WidgetColor.line)
                .frame(height: 1)

            hostFooter
        }
        .padding(13)
        .background(WidgetColor.bg)
        .widgetURL(URL(string: "drover://home"))
    }

    @ViewBuilder
    private var needsYouHeadline: some View {
        if snapshot.needsYouCount > 0 {
            HStack(spacing: 5) {
                Circle()
                    .fill(WidgetColor.accent)
                    .frame(width: 6, height: 6)
                Text("\(snapshot.needsYouCount) need\(snapshot.needsYouCount == 1 ? "s" : "") you")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(WidgetColor.accentHi)
                    .lineLimit(1)
            }
        } else {
            HStack(spacing: 5) {
                Circle()
                    .strokeBorder(WidgetColor.faint, lineWidth: 1)
                    .frame(width: 6, height: 6)
                Text("All clear")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(WidgetColor.muted)
                    .lineLimit(1)
            }
        }
    }

    private var hostFooter: some View {
        HStack(spacing: 7) {
            Text("HOSTS")
                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                .foregroundStyle(WidgetColor.faint)
            Text(hostSummary)
                .font(.system(size: 9.5, weight: .medium))
                .foregroundStyle(WidgetColor.muted)
                .lineLimit(1)
            Spacer(minLength: 2)
            Text("updated \(timeAgoText(from: entry.date))")
                .font(.system(size: 8.5, design: .monospaced))
                .foregroundStyle(entry.isStale ? WidgetColor.muted : WidgetColor.faint)
                .lineLimit(1)
        }
    }

    private var hostSummary: String {
        guard !snapshot.hosts.isEmpty else { return "No hosts configured" }
        return snapshot.hosts.prefix(3).map { host in
            host.status == .offline ? "\(host.name) off" : "\(host.name) \(host.activeSessionCount)"
        }.joined(separator: " · ")
    }

    private func timeAgoText(from date: Date) -> String {
        let elapsed = Int(Date().timeIntervalSince(date))
        if elapsed < 60 {
            return "just now"
        } else if elapsed < 3600 {
            return "\(elapsed / 60)m ago"
        } else {
            return "\(elapsed / 3600)h ago"
        }
    }
}

private struct WidgetQuotaRow: View {
    let account: WidgetAccountQuota
    let fallbackUpdatedText: String

    var body: some View {
        HStack(spacing: 6) {
            Text("\(account.providerDisplayName) · \(account.shortLabel)")
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(account.isStale ? WidgetColor.muted : WidgetColor.text)
                .lineLimit(1)
                .frame(width: 94, alignment: .leading)

            WidgetCapacityBar(
                fraction: account.remainingFraction,
                isCritical: account.isLowQuota,
                isStale: account.isStale,
                height: 4
            )
            .frame(maxWidth: .infinity)
            .layoutPriority(-1)

            Text(account.remainingPercent.map { "\($0)%" } ?? "—")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(quotaForeground)
                .monospacedDigit()
                .frame(width: 28, alignment: .trailing)

            Text(account.isStale ? "updated \(account.updatedText() ?? fallbackUpdatedText)" : (account.resetText() ?? ""))
                .font(.system(size: 8.5, design: .monospaced))
                .foregroundStyle(account.isStale ? WidgetColor.muted : WidgetColor.faint)
                .lineLimit(1)
                .frame(width: 82, alignment: .leading)
                .layoutPriority(1)
        }
        .padding(.horizontal, 5)
        .padding(.vertical, 3)
        .background {
            if account.isLowQuota {
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(WidgetColor.lowQuotaTint)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
    }

    private var quotaForeground: AnyShapeStyle {
        if account.isStale { return AnyShapeStyle(WidgetColor.muted) }
        if account.isLowQuota { return AnyShapeStyle(WidgetColor.warn) }
        return AnyShapeStyle(WidgetColor.accentHi)
    }
}

// MARK: - Large Widget

public struct LargeWidgetView: View {
    public let entry: DroverWidgetEntry

    public init(entry: DroverWidgetEntry) {
        self.entry = entry
    }

    private var snapshot: WidgetFleetSnapshot { entry.snapshot }

    public var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text("QUOTA")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
                Text("lowest remaining first")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(WidgetColor.muted)
                Spacer()
                Text("updated \(timeAgoText(from: entry.date))")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
            }

            if snapshot.accounts.isEmpty {
                Text("No accounts reported")
                    .font(.system(size: 12))
                    .foregroundStyle(WidgetColor.muted)
            } else {
                VStack(spacing: 4) {
                    // The large family deliberately receives the whole list;
                    // medium uses four rows to reserve room for host context.
                    ForEach(snapshot.accounts) { account in
                        WidgetQuotaRow(account: account, fallbackUpdatedText: timeAgoText(from: entry.date))
                    }
                }
            }

            Spacer(minLength: 0)

            Rectangle()
                .fill(WidgetColor.line)
                .frame(height: 1)

            HStack {
                if snapshot.needsYouCount > 0 {
                    Text("\(snapshot.needsYouCount) need\(snapshot.needsYouCount == 1 ? "s" : "") you")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(WidgetColor.accentHi)
                } else {
                    Text("All clear")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(WidgetColor.muted)
                }
                Spacer()
                Text("HOSTS")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
            }

            if snapshot.hosts.isEmpty {
                Text("No hosts configured")
                    .font(.system(size: 11))
                    .foregroundStyle(WidgetColor.muted)
            } else {
                VStack(spacing: 5) {
                    ForEach(snapshot.hosts) { host in
                        Link(destination: URL(string: "drover://host/\(host.id)")!) {
                            HStack {
                                Circle()
                                    .fill(host.status == .offline ? WidgetColor.line : (host.hasWaiting ? WidgetColor.accent : WidgetColor.faint))
                                    .frame(width: 7, height: 7)
                                Text(host.name)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundStyle(host.status == .offline ? WidgetColor.faint : WidgetColor.text)
                                Spacer()
                                Text(host.status == .offline ? "off" : "\(host.activeSessionCount)")
                                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                                    .foregroundStyle(host.status == .offline ? WidgetColor.faint : WidgetColor.accentHi)
                            }
                        }
                    }
                }
            }
        }
        .padding(16)
        .background(WidgetColor.bg)
        .widgetURL(URL(string: "drover://home"))
    }

    private func timeAgoText(from date: Date) -> String {
        let elapsed = max(0, Int(Date().timeIntervalSince(date)))
        if elapsed < 60 { return "just now" }
        if elapsed < 3_600 { return "\(elapsed / 60)m ago" }
        return "\(elapsed / 3_600)h ago"
    }
}

// MARK: - Small Widget (Square)

public struct SmallWidgetView: View {
    public let entry: DroverWidgetEntry

    public init(entry: DroverWidgetEntry) {
        self.entry = entry
    }

    private var snapshot: WidgetFleetSnapshot {
        entry.snapshot
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            // Top: "N need you"
            VStack(alignment: .leading, spacing: 2) {
                if snapshot.needsYouCount > 0 {
                    Text("\(snapshot.needsYouCount)")
                        .font(.system(size: 26, weight: .bold, design: .monospaced))
                        .foregroundStyle(WidgetColor.accentHi)
                    Text("need you")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(WidgetColor.text)
                } else {
                    Text("0")
                        .font(.system(size: 24, weight: .bold, design: .monospaced))
                        .foregroundStyle(WidgetColor.muted)
                    Text("need you · all clear")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(WidgetColor.muted)
                }
            }

            Spacer(minLength: 0)

            // Middle: Lowest quota bar
            if let lowest = snapshot.lowestQuotaAccount {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 4) {
                        Text("\(lowest.providerDisplayName) · \(lowest.shortLabel)")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(WidgetColor.text)
                            .lineLimit(1)
                        Spacer()
                        Text(lowest.remainingPercent.map { "\($0)%" } ?? "—")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(lowest.isStale ? AnyShapeStyle(WidgetColor.muted) : AnyShapeStyle(WidgetColor.accentHi))
                    }

                    WidgetCapacityBar(
                        fraction: lowest.remainingFraction,
                        isCritical: lowest.isLowQuota,
                        isStale: lowest.isStale
                    )
                }
                .padding(6)
                .background(WidgetColor.surface)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            } else {
                Text("Quota unavailable")
                    .font(.system(size: 10))
                    .foregroundStyle(WidgetColor.muted)
            }

            Spacer(minLength: 0)

            // Bottom: Live hosts count & timestamp
            HStack(spacing: 4) {
                Circle()
                    .fill(snapshot.liveHostsCount > 0 ? WidgetColor.accent : WidgetColor.faint)
                    .frame(width: 5, height: 5)
                Text("\(snapshot.liveHostsCount) host\(snapshot.liveHostsCount == 1 ? "" : "s") live")
                    .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
            }
        }
        .padding(12)
        .background(WidgetColor.bg)
        .widgetURL(URL(string: "drover://home"))
    }
}

// MARK: - Lock Screen Widget (Rectangular)

public struct AccessoryRectangularWidgetView: View {
    public let entry: DroverWidgetEntry

    public init(entry: DroverWidgetEntry) {
        self.entry = entry
    }

    private var snapshot: WidgetFleetSnapshot {
        entry.snapshot
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: "bolt.fill")
                    .font(.system(size: 9))
                Text("\(snapshot.needsYouCount) need you · \(snapshot.liveHostsCount) live")
                    .font(.system(size: 11, weight: .bold))
            }

            if let lowest = snapshot.lowestQuotaAccount {
                HStack(spacing: 4) {
                    Text("\(lowest.providerDisplayName) · \(lowest.shortLabel): \(lowest.remainingPercent.map { "\($0)%" } ?? "—") left")
                        .font(.system(size: 9.5, weight: .medium))
                        .lineLimit(1)
                }
                ProgressView(value: lowest.remainingFraction ?? 0, total: 1.0)
                    .progressViewStyle(.linear)
            } else {
                Text("Quota unavailable")
                    .font(.system(size: 9))
            }
        }
        .widgetURL(URL(string: "drover://home"))
    }
}

// MARK: - Entry View Switcher

public struct DroverWidgetEntryView: View {
    @Environment(\.widgetFamily) var family
    public let entry: DroverWidgetEntry

    public init(entry: DroverWidgetEntry) {
        self.entry = entry
    }

    public var body: some View {
        switch family {
        case .systemMedium:
            MediumWidgetView(entry: entry)
        case .systemLarge:
            LargeWidgetView(entry: entry)
        case .systemSmall:
            SmallWidgetView(entry: entry)
        case .accessoryRectangular:
            AccessoryRectangularWidgetView(entry: entry)
        default:
            MediumWidgetView(entry: entry)
        }
    }
}
