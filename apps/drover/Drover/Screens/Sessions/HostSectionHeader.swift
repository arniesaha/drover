import NexusKit
import SwiftUI

/// Fleet section header: presence dot, host name, relay badge, last-seen.
/// Green = live, orange = stale (missed heartbeats, may recover),
/// gray = offline (relay socket down, or host unknown to the hub).
struct HostSectionHeader: View {
    let host: HostSummary

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(dotColor)
                .frame(width: 8, height: 8)
            Text(host.title)
            if host.isRelay {
                Text("relay")
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(.secondary.opacity(0.2), in: Capsule())
            }
            Spacer()
            if host.presence != .online {
                Text(lastSeenText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .textCase(nil)
        .accessibilityIdentifier("host-header-\(host.id)")
    }

    private var dotColor: Color {
        switch host.presence {
        case .online: return .green
        case .stale: return .orange
        case .offline: return .gray
        }
    }

    private var lastSeenText: String {
        guard let lastSeenAt = host.lastSeenAt else {
            return host.presence == .stale ? "last seen unknown" : "offline"
        }
        let relative = lastSeenAt.formatted(.relative(presentation: .named))
        return "last seen \(relative)"
    }
}
