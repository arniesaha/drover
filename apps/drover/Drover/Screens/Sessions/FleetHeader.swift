import DroverKit
import SwiftUI

/// The top of the inbox: the count that matters, one quiet line about the
/// rest of the fleet, and the interactive host filter strip.
///
/// This is also the entire degraded-state surface. When the hub is
/// unreachable the fleet line carries the error and offers the retry, and
/// every host dot falls to its offline form — replacing both the old
/// `UnreachableBanner` and the whole-screen dimming. Nothing here is a banner:
/// the status lives in the space that was already describing status.
struct FleetHeader: View {
    let summary: FleetSummaryPresentation
    let hostGroups: [HostGroup]
    var selectedHostID: String? = nil
    var onSelectHost: ((String?) -> Void)? = nil
    var isOverviewExpanded: Bool = false
    var onToggleOverview: (() -> Void)? = nil
    let onRetry: () -> Void

    private var totalActiveSessions: Int {
        hostGroups.reduce(0) { $0 + $1.sessions.count }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                if let headline = summary.headline {
                    HStack(alignment: .firstTextBaseline) {
                        Text(headline)
                            .droverText(.h1)
                            .accessibilityIdentifier("fleet-headline")

                        Spacer()

                        if onToggleOverview != nil {
                            overviewToggleButton
                        }
                    }
                } else if onToggleOverview != nil {
                    HStack {
                        Spacer()
                        overviewToggleButton
                    }
                }

                HStack(spacing: 8) {
                    Text(summary.fleetLine)
                        .droverText(.subtitle)
                        .foregroundStyle(summary.isStale ? DroverColor.accentHi : DroverColor.muted)
                        .accessibilityIdentifier("fleet-line")

                    if summary.isStale {
                        Button("Retry", action: onRetry)
                            .font(.system(.caption, design: .default, weight: .medium))
                            .foregroundStyle(DroverColor.accentHi)
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("fleet-retry")
                    }
                }
            }

            if !hostGroups.isEmpty {
                hostStrip
            }
        }
    }

    private var overviewToggleButton: some View {
        Button {
            onToggleOverview?()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: isOverviewExpanded ? "square.grid.3x3.fill" : "square.grid.3x3")
                    .font(.system(size: 11, weight: .semibold))
                Text("Overview")
                    .font(.system(.caption2, design: .monospaced, weight: .medium))
                Image(systemName: isOverviewExpanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 9, weight: .bold))
            }
            .foregroundStyle(isOverviewExpanded ? DroverColor.accentHi : DroverColor.muted)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(isOverviewExpanded ? DroverColor.accentTint : DroverColor.surface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(isOverviewExpanded ? DroverColor.accent : DroverColor.line, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("overview-toggle")
        .accessibilityLabel(isOverviewExpanded ? "Hide fleet bird's-eye overview" : "Show fleet bird's-eye overview")
    }

    private var hostStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                AllHostsChip(
                    totalCount: totalActiveSessions,
                    isSelected: selectedHostID == nil,
                    isStale: summary.isStale,
                    hasWaiting: hostGroups.contains { $0.sessions.contains { $0.attention == .needsApproval || $0.attention == .needsInput } },
                    hasRunning: hostGroups.contains { $0.sessions.contains { $0.attention == .working } },
                    onSelect: { onSelectHost?(nil) }
                )

                ForEach(hostGroups) { group in
                    HostChip(
                        group: group,
                        isSelected: selectedHostID == group.host.id,
                        isStale: summary.isStale,
                        onSelect: {
                            if selectedHostID == group.host.id {
                                onSelectHost?(nil)
                            } else {
                                onSelectHost?(group.host.id)
                            }
                        }
                    )
                }
            }
        }
        // The strip is one row of chips; letting it scroll horizontally keeps
        // a five-host fleet from wrapping into a block that pushes the actual
        // work off screen.
        .scrollClipDisabled()
    }
}

/// "All" hosts filter chip at the start of the host strip.
private struct AllHostsChip: View {
    let totalCount: Int
    let isSelected: Bool
    let isStale: Bool
    let hasWaiting: Bool
    let hasRunning: Bool
    let onSelect: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 6) {
                dot
                Text("All")
                    .font(.system(.caption, design: .default, weight: isSelected ? .medium : .regular))
                    .foregroundStyle(isStale ? DroverColor.faint : (isSelected ? DroverColor.accentHi : DroverColor.text))
                Text(isStale ? "·" : "\(totalCount) active")
                    .droverText(.marker)
                    .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.muted))
            }
            .padding(.horizontal, 10)
            .background {
                if isSelected {
                    Capsule().fill(DroverColor.accentTint)
                }
            }
            .overlay {
                Capsule().strokeBorder(
                    isSelected ? AnyShapeStyle(DroverColor.accent) : AnyShapeStyle(DroverColor.line),
                    lineWidth: isSelected ? 1.5 : 1
                )
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("All hosts, \(totalCount) active sessions\(isSelected ? ", selected" : "")")
        .accessibilityIdentifier("host-chip-all")
    }

    @ViewBuilder
    private var dot: some View {
        if isStale {
            Circle().strokeBorder(
                DroverColor.muted,
                style: StrokeStyle(lineWidth: 1.5, dash: [2, 2])
            )
            .frame(width: 7, height: 7)
        } else if hasWaiting {
            Circle().fill(DroverColor.warn).frame(width: 7, height: 7)
        } else if hasRunning {
            Circle().fill(DroverColor.accentHi)
                .frame(width: 7, height: 7)
                .phaseAnimator(reduceMotion ? [false] : [false, true]) { dot, faded in
                    dot.opacity(faded ? 0.45 : 1)
                } animation: { _ in
                    .easeInOut(duration: 1.2)
                }
        } else {
            Circle().fill(DroverColor.muted).frame(width: 7, height: 7)
        }
    }
}

/// One host in the strip: presence as form, plus how many of its sessions are
/// active (running + needs-you). Tapping a host filters the inbox list to that machine;
/// tapping again clears. Offline hosts stay visible but dimmed.
private struct HostChip: View {
    let group: HostGroup
    let isSelected: Bool
    let isStale: Bool
    let onSelect: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 6) {
                presenceDot
                Text(group.host.title)
                    .font(.system(.caption, design: .default, weight: isSelected ? .medium : .regular))
                    .foregroundStyle(isStale ? DroverColor.faint : (isSelected ? DroverColor.accentHi : DroverColor.text))
                    .lineLimit(1)
                Text(isStale ? "·" : "\(group.sessions.count) active")
                    .droverText(.marker)
                    .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.accentHi) : countStyle)
            }
            .padding(.horizontal, 10)
            .background {
                if isSelected {
                    Capsule().fill(DroverColor.accentTint)
                }
            }
            .overlay {
                Capsule().strokeBorder(
                    isSelected ? AnyShapeStyle(DroverColor.accent) : AnyShapeStyle(DroverColor.line),
                    lineWidth: isSelected ? 1.5 : 1
                )
            }
        }
        .buttonStyle(.plain)
        .opacity(group.host.presence == .offline ? 0.45 : 1.0)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(group.host.title), \(accessibilityState)\(isSelected ? ", selected" : "")")
        .accessibilityHint(isSelected ? "Tap to clear host filter" : "Tap to filter sessions to this host")
        .accessibilityIdentifier("host-chip-\(group.host.id)")
    }

    /// Status icon semantics consistent with StateDot:
    /// - Stale: dashed line ring
    /// - Offline: dimmed hollow line ring
    /// - Needs You (hasWaiting): solid amber disc
    /// - Running (hasRunning): pulsing filled teal disc
    /// - Idle: solid neutral disc
    @ViewBuilder
    private var presenceDot: some View {
        if isStale {
            Circle().strokeBorder(
                DroverColor.muted,
                style: StrokeStyle(lineWidth: 1.5, dash: [2, 2])
            )
            .frame(width: 7, height: 7)
        } else if group.host.presence == .offline {
            Circle().strokeBorder(DroverColor.line, lineWidth: 1).frame(width: 7, height: 7)
        } else if hasWaiting {
            Circle().fill(DroverColor.warn).frame(width: 7, height: 7)
        } else if hasRunning {
            Circle().fill(DroverColor.accentHi)
                .frame(width: 7, height: 7)
                .phaseAnimator(reduceMotion ? [false] : [false, true]) { dot, faded in
                    dot.opacity(faded ? 0.45 : 1)
                } animation: { _ in
                    .easeInOut(duration: 1.2)
                }
        } else {
            Circle().fill(DroverColor.muted).frame(width: 7, height: 7)
        }
    }

    private var countStyle: AnyShapeStyle {
        if isStale || group.sessions.isEmpty {
            return AnyShapeStyle(DroverColor.faint)
        }
        return AnyShapeStyle(DroverColor.accentHi)
    }

    private var hasWaiting: Bool {
        group.sessions.contains {
            $0.attention == .needsApproval || $0.attention == .needsInput
        }
    }

    private var hasRunning: Bool {
        group.sessions.contains {
            $0.attention == .working
        }
    }

    private var accessibilityState: String {
        if isStale { return "unreachable" }
        let presence = switch group.host.presence {
        case .online: "live"
        case .stale: "stale"
        case .offline: "offline"
        }
        return "\(presence), \(group.sessions.count) active"
    }
}
