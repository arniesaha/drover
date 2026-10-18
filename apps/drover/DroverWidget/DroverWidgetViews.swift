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
        HStack(alignment: .top, spacing: 14) {
            // LEFT COLUMN: Accounts Quota
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 4) {
                    Text("QUOTA")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundStyle(WidgetColor.faint)
                    Spacer()
                    if snapshot.accounts.isEmpty {
                        Text("No data")
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(WidgetColor.muted)
                    }
                }
                .padding(.bottom, 1)

                if quotaDisplay.visibleAccounts.isEmpty {
                    Text("No accounts reported")
                        .font(.system(size: 11))
                        .foregroundStyle(WidgetColor.muted)
                        .frame(maxHeight: .infinity, alignment: .center)
                } else {
                    VStack(alignment: .leading, spacing: 4.5) {
                        ForEach(quotaDisplay.visibleAccounts) { account in
                            accountQuotaRow(account)
                        }
                    }

                    if quotaDisplay.hasTruncatedAccounts {
                        Text("+\(quotaDisplay.truncatedCount) more")
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(WidgetColor.faint)
                            .padding(.top, 1)
                    }
                }

                Spacer(minLength: 0)

                // Freshness footer
                freshnessFooter
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            // VERTICAL DIVIDER
            Rectangle()
                .fill(WidgetColor.line)
                .frame(width: 1)
                .padding(.vertical, 2)

            // RIGHT COLUMN: Hosts & Needs You
            VStack(alignment: .leading, spacing: 5) {
                // Headline: "N need you"
                headlineSection

                // Hosts List
                if snapshot.hosts.isEmpty {
                    Text("No hosts configured")
                        .font(.system(size: 11))
                        .foregroundStyle(WidgetColor.muted)
                        .frame(maxHeight: .infinity, alignment: .center)
                } else {
                    VStack(alignment: .leading, spacing: 4.5) {
                        ForEach(Array(snapshot.hosts.prefix(4))) { host in
                            Link(destination: URL(string: "drover://host/\(host.id)")!) {
                                hostRow(host)
                            }
                        }
                    }
                }

                Spacer(minLength: 0)

                // Stale indicator if applicable
                staleFooter
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(13)
        .background(WidgetColor.bg)
        .widgetURL(URL(string: "drover://home"))
    }

    // MARK: Account Row
    private func accountQuotaRow(_ account: WidgetAccountQuota) -> some View {
        HStack(spacing: 5) {
            // Provider initial badge
            Text(account.providerInitial)
                .font(.system(size: 8, weight: .bold, design: .monospaced))
                .foregroundStyle(WidgetColor.accentHi)
                .frame(width: 13, height: 13)
                .background(WidgetColor.card)
                .clipShape(RoundedRectangle(cornerRadius: 3))

            // Short account label
            Text(account.accountLabel)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(WidgetColor.text)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: 55, alignment: .leading)

            // Compact progress bar
            WidgetCapacityBar(
                fraction: account.remainingFraction,
                isCritical: (account.remainingFraction ?? 1.0) <= 0.15,
                isStale: account.isStale
            )
            .frame(maxWidth: .infinity)

            // Percentage remaining
            Text(account.remainingPercent.map { "\($0)%" } ?? "—")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(account.isStale ? AnyShapeStyle(WidgetColor.muted) : AnyShapeStyle(WidgetColor.accentHi))
                .monospacedDigit()
                .frame(width: 28, alignment: .trailing)

            // Stale clock indicator
            if account.isStale {
                Image(systemName: "clock")
                    .font(.system(size: 8.5))
                    .foregroundStyle(WidgetColor.warn)
            }
        }
    }

    // MARK: Headline
    @ViewBuilder
    private var headlineSection: some View {
        if snapshot.needsYouCount > 0 {
            HStack(spacing: 5) {
                Circle()
                    .fill(WidgetColor.accent)
                    .frame(width: 6, height: 6)
                Text("\(snapshot.needsYouCount) need\(snapshot.needsYouCount == 1 ? "s" : "") you")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(WidgetColor.accentHi)
                    .lineLimit(1)
            }
        } else {
            HStack(spacing: 5) {
                Circle()
                    .strokeBorder(WidgetColor.faint, lineWidth: 1)
                    .frame(width: 6, height: 6)
                Text("All clear")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(WidgetColor.muted)
                    .lineLimit(1)
            }
        }
    }

    // MARK: Host Row
    private func hostRow(_ host: WidgetHostStatus) -> some View {
        HStack(spacing: 6) {
            // Status dot: active / idle / offline
            switch host.status {
            case .active:
                if host.hasWaiting {
                    Circle()
                        .fill(WidgetColor.accent)
                        .frame(width: 7, height: 7)
                } else {
                    Circle()
                        .strokeBorder(WidgetColor.accentHi, lineWidth: 1.5)
                        .frame(width: 7, height: 7)
                }
            case .idle:
                Circle()
                    .strokeBorder(WidgetColor.faint, lineWidth: 1.2)
                    .frame(width: 7, height: 7)
            case .offline:
                Circle()
                    .strokeBorder(WidgetColor.line, lineWidth: 1)
                    .frame(width: 7, height: 7)
            }

            // Host short name
            Text(host.name)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(host.status == .offline ? WidgetColor.faint : WidgetColor.text)
                .lineLimit(1)

            Spacer(minLength: 4)

            // Active session count
            Text(host.status == .offline ? "·" : "\(host.activeSessionCount)")
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundStyle(host.activeSessionCount > 0 ? AnyShapeStyle(WidgetColor.accentHi) : AnyShapeStyle(WidgetColor.faint))
        }
        .contentShape(Rectangle())
    }

    // MARK: Footers
    private var freshnessFooter: some View {
        Text("Updated \(timeAgoText(from: entry.date))")
            .font(.system(size: 8.5, design: .monospaced))
            .foregroundStyle(WidgetColor.faint)
            .lineLimit(1)
    }

    @ViewBuilder
    private var staleFooter: some View {
        if entry.isStale {
            HStack(spacing: 3) {
                Image(systemName: "clock.badge.exclamationmark")
                    .font(.system(size: 8.5))
                Text("Stale")
                    .font(.system(size: 8.5, weight: .medium, design: .monospaced))
            }
            .foregroundStyle(WidgetColor.warn)
        } else {
            HStack(spacing: 4) {
                Text("\(snapshot.liveHostsCount) live")
                    .font(.system(size: 8.5, design: .monospaced))
                    .foregroundStyle(WidgetColor.faint)
            }
        }
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
                        Text(lowest.providerInitial)
                            .font(.system(size: 8, weight: .bold, design: .monospaced))
                            .foregroundStyle(WidgetColor.accentHi)
                        Text(lowest.accountLabel)
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
                        isCritical: (lowest.remainingFraction ?? 1.0) <= 0.15,
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
                    Text("\(lowest.accountLabel): \(lowest.remainingPercent.map { "\($0)%" } ?? "—") left")
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
        case .systemSmall:
            SmallWidgetView(entry: entry)
        case .accessoryRectangular:
            AccessoryRectangularWidgetView(entry: entry)
        default:
            MediumWidgetView(entry: entry)
        }
    }
}
