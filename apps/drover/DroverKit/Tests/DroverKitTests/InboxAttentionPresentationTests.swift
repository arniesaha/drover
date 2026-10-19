import Foundation
import Testing
@testable import DroverKit

@Suite struct InboxAttentionPresentationTests {
    @Test func countExcludesResolvedInsightsAndZeroHasNoBadge() {
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        let answered = SessionSummary(id: "answered", hostID: "studio", harness: "codex", mode: "structured", status: "working", awaiting: nil, cwd: nil, lastActivity: base)
        let waiting = SessionSummary(id: "waiting", hostID: "studio", harness: "codex", mode: "structured", status: "working", awaiting: "input", cwd: nil, lastActivity: base.addingTimeInterval(60))
        let resolvedHigh = Self.insight(id: "resolved", state: .resolved, severity: .high, date: base)
        let openHigh = Self.insight(id: "open", state: .open, severity: .high, date: base.addingTimeInterval(120))

        let zero = InboxAttentionPresentation.count(workItems: [], sessions: [answered], insights: [resolvedHigh])
        #expect(zero == 0)
        #expect(InboxAttentionPresentation.badgeValue(for: zero) == nil)
        #expect(InboxAttentionPresentation.count(workItems: [], sessions: [answered, waiting], insights: [resolvedHigh, openHigh]) == 2)
        #expect(InboxAttentionPresentation.openPriorityInsights([resolvedHigh, openHigh]).map(\.findingID) == ["open"])
    }

    private static func insight(id: String, state: InsightState, severity: InsightSeverity, date: Date) -> InsightSummary {
        let data = try! JSONSerialization.data(withJSONObject: [
            "finding_id": id, "analyzer_id": "analyzer", "rule_id": "rule", "target_type": "host",
            "target_id": "studio", "analyzer_class": "deterministic", "severity": severity.rawValue,
            "confidence": "confirmed", "title": id, "state": state.rawValue,
            "first_seen_at": ISO8601DateFormatter().string(from: date),
            "last_seen_at": ISO8601DateFormatter().string(from: date),
        ])
        return try! JSONDecoder().decode(InsightSummary.self, from: data)
    }
}
