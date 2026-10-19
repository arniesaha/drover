import DroverKit
import SwiftUI

/// Pinned fleet status. Account diagnostics live exclusively on Accounts so
/// Home keeps its full vertical space for sessions.
struct InboxStatusHeader: View {
    let summary: FleetSummaryPresentation
    let hostGroups: [HostGroup]
    var selectedHostID: String? = nil
    var onSelectHost: ((String?) -> Void)? = nil
    var isOverviewExpanded: Bool = false
    var onToggleOverview: (() -> Void)? = nil
    let onRetry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            FleetHeader(
                summary: summary,
                hostGroups: hostGroups,
                selectedHostID: selectedHostID,
                onSelectHost: onSelectHost,
                isOverviewExpanded: isOverviewExpanded,
                onToggleOverview: onToggleOverview,
                onRetry: onRetry
            )
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
