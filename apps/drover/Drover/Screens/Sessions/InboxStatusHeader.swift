import DroverKit
import SwiftUI

/// The part of the inbox that does not scroll: the fleet line and the provider
/// capacity strip, held above the session list.
///
/// Capacity used to be the third thing in the scroll, sitting *between* two
/// groups of sessions. That cost it twice: it scrolled away the moment you
/// reached for a session, and it broke the list in half on the way (#80).
/// Pinned, it answers "what have I got left to spend" without being scrolled
/// past, and the list below it is one continuous run.
///
/// It stays deliberately short — fleet line, host strip, one row of capacity
/// cards — because every point it takes is a point the list does not get.
/// `InboxStatusHeaderLayoutTests` holds that line.
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
        .accessibilityIdentifier("inbox-status-header")
    }
}
