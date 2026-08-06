import DroverKit
import SwiftUI

/// The top of the inbox: the count that matters, one quiet line about the
/// rest of the fleet, and the host strip.
///
/// This is also the entire degraded-state surface. When the hub is
/// unreachable the fleet line carries the error and offers the retry, and
/// every host dot falls to its offline form — replacing both the old
/// `UnreachableBanner` and the whole-screen dimming. Nothing here is a banner:
/// the status lives in the space that was already describing status.
struct FleetHeader: View {
    let summary: FleetSummaryPresentation
    let hostGroups: [HostGroup]
    let onRetry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                if let headline = summary.headline {
                    Text(headline)
                        .droverText(.h1)
                        .accessibilityIdentifier("fleet-headline")
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

    private var hostStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(hostGroups) { group in
                    HostChip(group: group, isStale: summary.isStale)
                }
            }
        }
        // The strip is one row of chips; letting it scroll horizontally keeps
        // a five-host fleet from wrapping into a block that pushes the actual
        // work off screen.
        .scrollClipDisabled()
    }
}

/// One host in the strip: presence as form, plus how many of its sessions are
/// active. A stale hub blanks the count rather than showing a number it can no
/// longer vouch for.
private struct HostChip: View {
    let group: HostGroup
    let isStale: Bool

    var body: some View {
        HStack(spacing: 6) {
            presenceDot
            Text(group.host.title)
                .font(.system(.caption, design: .default))
                .foregroundStyle(isStale ? DroverColor.faint : DroverColor.text)
                .lineLimit(1)
            Text(isStale ? "—" : "\(group.sessions.count)")
                .droverText(.marker)
                .foregroundStyle(countStyle)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .overlay {
            Capsule().strokeBorder(DroverColor.line, lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(group.host.title), \(accessibilityState)")
        .accessibilityIdentifier("host-chip-\(group.host.id)")
    }

    /// Same three forms as a session dot, one level up: filled when this host
    /// has work waiting, hollow while it is merely online, faint otherwise.
    @ViewBuilder
    private var presenceDot: some View {
        if isStale || group.host.presence == .offline {
            Circle().strokeBorder(DroverColor.line, lineWidth: 1).frame(width: 7, height: 7)
        } else if hasWaiting {
            Circle().fill(DroverColor.accent).frame(width: 7, height: 7)
        } else {
            Circle()
                .strokeBorder(group.host.presence == .online ? AnyShapeStyle(DroverColor.accentHi)
                                                             : AnyShapeStyle(DroverColor.faint),
                              lineWidth: 1.5)
                .frame(width: 7, height: 7)
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
