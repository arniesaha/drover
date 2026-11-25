import Foundation
import Testing
@testable import DroverKit

@Suite struct InboxAttentionPresentationTests {
    @Test func countExcludesResolvedInsightsAndZeroHasNoBadge() {
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        let answered = SessionSummary(id: "answered", hostID: "studio", harness: "codex", mode: "structured", status: "working", awaiting: nil, cwd: nil, lastActivity: base)
        let waiting = SessionSummary(id: "waiting", hostID: "studio", harness: "codex", mode: "structured", status: "working", awaiting: "input", cwd: nil, lastActivity: base.addingTimeInterval(60))
        let approval = SessionSummary(id: "approval", hostID: "studio", harness: "codex", mode: "structured", status: "working", awaiting: "approval", cwd: nil, lastActivity: base.addingTimeInterval(90))
        let resolvedHigh = Self.insight(id: "resolved", state: .resolved, severity: .high, date: base)
        let openHigh = Self.insight(id: "open", state: .open, severity: .high, date: base.addingTimeInterval(120))

        let zero = InboxAttentionPresentation.count(workItems: [], sessions: [answered], insights: [resolvedHigh])
        #expect(zero == 0)
        #expect(InboxAttentionPresentation.badgeValue(for: zero) == nil)
        #expect(InboxAttentionPresentation.sessionsAwaitingAnswer([waiting, approval]).map(\.id) == ["approval", "waiting"])
        #expect(InboxAttentionPresentation.count(workItems: [], sessions: [answered, waiting, approval], insights: [resolvedHigh, openHigh]) == 3)
        #expect(InboxAttentionPresentation.openPriorityInsights([resolvedHigh, openHigh]).map(\.findingID) == ["open"])
    }

    @Test func retiredOpenHighEvidenceIsNotAnActiveWarning() {
        let date = Date(timeIntervalSince1970: 1_700_000_000)
        let retired = Self.insight(id: "retired", state: .open, severity: .high, date: date, historical: true)
        let current = Self.insight(id: "current", state: .open, severity: .high, date: date)
        #expect(InboxAttentionPresentation.openPriorityInsights([retired]).isEmpty)
        #expect(InboxAttentionPresentation.count(workItems: [], sessions: [], insights: [retired]) == 0)
        #expect(InboxAttentionPresentation.openPriorityInsights([retired, current]).map(\.findingID) == ["current"])
        #expect(retired.state == .open && retired.severity == .high)
    }

    private static func insight(id: String, state: InsightState, severity: InsightSeverity, date: Date, historical: Bool = false) -> InsightSummary {
        let data = try! JSONSerialization.data(withJSONObject: [
            "finding_id": id, "analyzer_id": "analyzer", "rule_id": "rule", "target_type": "host",
            "target_id": "studio", "analyzer_class": "deterministic", "severity": severity.rawValue,
            "confidence": "confirmed", "title": id, "state": state.rawValue, "historical": historical,
            "first_seen_at": ISO8601DateFormatter().string(from: date),
            "last_seen_at": ISO8601DateFormatter().string(from: date),
        ])
        return try! JSONDecoder().decode(InsightSummary.self, from: data)
    }
}
