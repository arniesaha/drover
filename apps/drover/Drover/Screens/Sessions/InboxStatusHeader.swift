import DroverKit
import SwiftUI

/// Pinned fleet status and a bounded Accounts preview. Provider details are
/// pushed onto their own scrolling page so the inbox keeps room for sessions.
struct InboxStatusHeader<Capacity: View>: View {
    let summary: FleetSummaryPresentation
    let hostGroups: [HostGroup]
    let onRetry: () -> Void
    @ViewBuilder let capacity: Capacity

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            FleetHeader(summary: summary, hostGroups: hostGroups, onRetry: onRetry)
            capacity
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Opaque, and drawn over the list: the rows pass under this edge, and
        // a translucent header would show them doing it.
        .background(DroverColor.bg)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(DroverColor.line)
                .frame(height: 1)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("inbox-status-header")
    }
}
