import Foundation

/// Membership and count rules for the actionable Inbox segment.
///
/// This intentionally does not reuse the Home fleet headline: that headline
/// includes approval states, while Inbox only counts sessions waiting for an
/// answer plus work decisions and unresolved priority findings.
public enum InboxAttentionPresentation {
    public static func sessionsAwaitingAnswer(_ sessions: [SessionSummary]) -> [SessionSummary] {
        sessions
            .filter { $0.attention == .needsInput }
            .sorted { ($0.activityDate ?? .distantPast) > ($1.activityDate ?? .distantPast) }
    }

    public static func openPriorityInsights(_ insights: [InsightSummary]) -> [InsightSummary] {
        insights
            .filter { $0.state == .open && ($0.severity == .critical || $0.severity == .high) }
            .sorted { $0.lastSeenAt > $1.lastSeenAt }
    }

    public static func count(
        workItems: [WorkAttentionItem], sessions: [SessionSummary], insights: [InsightSummary]
    ) -> Int {
        workItems.count + sessionsAwaitingAnswer(sessions).count + openPriorityInsights(insights).count
    }

    /// Tab bar badges are absent at zero instead of displaying a meaningless
    /// zero badge.
    public static func badgeValue(for count: Int) -> String? {
        count > 0 ? String(count) : nil
    }
}
