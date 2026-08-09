import Foundation
import Testing
@testable import DroverKit

@Test func homeSectionsFollowApprovedHierarchy() throws {
    let overview = try decodeOverview(
        providerStatus: "ok",
        providerData: #"[{"snapshot_id":"snapshot-1","dedup_key":"codex:personal","provider":"openai","account_label":"Personal","host_id":"mac-mini","status":"ok","observed_at":"2026-08-08T18:00:00Z","source":"codex_app_server","windows":[]}]"#,
        activityData: populatedActivity,
        projects: populatedProjects,
        insightCounts: #"{"critical":0,"high":1,"medium":0,"low":0}"#
    )

    #expect(HomeSection.visible(for: overview) == [
        .attention, .providerCapacity, .activity, .popularProjects, .insights, .sessions,
    ])
}

@Test func emptyHealthyHomeSectionsStayCompact() throws {
    let overview = try decodeOverview(
        providerStatus: "ok",
        providerData: "[]",
        activityData: "null",
        projects: "[]",
        insightCounts: #"{"critical":0,"high":0,"medium":0,"low":0}"#
    )

    #expect(HomeSection.visible(for: overview) == [.attention, .sessions])
}

@Test func unavailableProviderSectionRemainsVisibleWithoutAccounts() throws {
    let overview = try decodeOverview(
        providerStatus: "unavailable",
        providerData: "[]",
        activityData: "null",
        projects: "[]",
        insightCounts: "null"
    )

    #expect(HomeSection.visible(for: overview) == [.attention, .providerCapacity, .sessions])
}

@Test func expiredResetNeverShowsNegativeCountdown() throws {
    let now = Date(timeIntervalSince1970: 2_000)
    let window = try decodeProviderWindow("""
    {"kind":"primary","used_percent":25,"limit_value":1000,
     "remaining_value":750,"unit":"tokens",
     "resets_at":"1970-01-01T00:16:40Z"}
    """)

    let value = ProviderCapacityPresentation(window: window, now: now)

    #expect(value.resetText == "Stale")
    #expect(value.isStale)
    #expect(!value.resetText.contains("-"))
}

@Test func providerPresentationUsesReportedUnitsForUsedAndRemaining() throws {
    let window = try decodeProviderWindow("""
    {"kind":"primary","used_percent":25,"limit_value":1000,
     "remaining_value":750,"unit":"tokens",
     "resets_at":"1970-01-01T01:33:20Z"}
    """)

    let value = ProviderCapacityPresentation(
        window: window,
        now: Date(timeIntervalSince1970: 2_000)
    )

    #expect(value.usedText == "250 tokens used")
    #expect(value.remainingText == "750 tokens remaining")
    #expect(value.resetText == "Resets in 1h")
}

@Test func providerPresentationFallsBackToReportedPercentage() throws {
    let window = try decodeProviderWindow("""
    {"kind":"secondary","used_percent":42.5,"window_minutes":10080}
    """)

    let value = ProviderCapacityPresentation(
        window: window,
        now: Date(timeIntervalSince1970: 2_000)
    )

    #expect(value.usedText == "42.5% used")
    #expect(value.remainingText == "57.5% remaining")
    #expect(value.resetText == "Reset unavailable")
}

@Test func projectFallbackNamesSessionMetricAndCoverage() throws {
    let project = try decodePopularProject(metric: "sessions")

    let value = ProjectActivityPresentation(project: project, tokenCoveragePercent: 42)

    #expect(value.valueText == "12 sessions")
    #expect(value.metricText == "Ranked by sessions")
    #expect(value.coverageText == "42% token coverage")
    #expect(value.contributorsText == "codex, gemini · mac-mini, nas")
}

@Test func tokenRankedProjectNamesTokenMetric() throws {
    let project = try decodePopularProject(metric: "tokens")

    let value = ProjectActivityPresentation(project: project, tokenCoveragePercent: 86.4)

    #expect(value.valueText == "1,234 tokens")
    #expect(value.metricText == "Ranked by tokens")
    #expect(value.coverageText == "86.4% token coverage")
}

@Test func insightPresentationDistinguishesModelJudgmentFromDeterministicEvidence() throws {
    let model = try decodeInsightSummary(analyzerClass: "model", confidence: "likely")
    let deterministic = try decodeInsightSummary(
        analyzerClass: "deterministic", confidence: "confirmed"
    )

    let modelValue = InsightPresentation(insight: model)
    let deterministicValue = InsightPresentation(insight: deterministic)

    #expect(modelValue.sourceText == "Model judgment")
    #expect(modelValue.confidenceText == "Likely")
    #expect(modelValue.uncertaintyText == "Review the evidence before making changes.")
    #expect(deterministicValue.sourceText == "Deterministic check")
    #expect(deterministicValue.confidenceText == "Confirmed")
    #expect(deterministicValue.uncertaintyText == nil)
}

@Test func insightDetailUsesTheSameSeverityConfidenceAndSourceFormatting() throws {
    let finding = try JSONDecoder().decode(InsightFinding.self, from: Data(#"""
    {"finding_id":"finding-1","analyzer_id":"prompt-review",
     "rule_id":"duplicate-context","target_type":"system_prompt","target_id":"codex",
     "analyzer_class":"model","severity":"high","confidence":"speculative",
     "title":"Repeated context","impact":"More tokens","remediation":["Remove repetition"],
     "state":"open","first_seen_at":"2026-08-08T18:00:00Z",
     "last_seen_at":"2026-08-08T18:01:00Z"}
    """#.utf8))

    let value = InsightPresentation(insight: finding)

    #expect(value.severityText == "High")
    #expect(value.confidenceText == "Speculative")
    #expect(value.sourceText == "Model judgment")
    #expect(value.uncertaintyText != nil)
}

@Test func staleProviderAccountNamesFreshnessAndSourceClass() throws {
    let account = try JSONDecoder().decode(ProviderAccount.self, from: Data(#"""
    {"snapshot_id":"snapshot-1","dedup_key":"codex:personal","provider":"openai",
     "account_label":"Personal","host_id":"mac-mini","status":"stale",
     "observed_at":"1970-01-01T00:16:40Z","source":"codex_app_server",
     "windows":[{"kind":"primary","used_percent":20}]}
    """#.utf8))

    let value = ProviderCapacityPresentation(
        account: account,
        window: account.windows[0],
        now: Date(timeIntervalSince1970: 8_200)
    )

    #expect(value.isStale)
    #expect(value.freshnessText == "Updated 2h ago")
    #expect(value.sourceText == "Provider reported")
}

private func decodeProviderWindow(_ json: String) throws -> ProviderWindow {
    try JSONDecoder().decode(ProviderWindow.self, from: Data(json.utf8))
}

private func decodePopularProject(metric: String) throws -> PopularProject {
    try JSONDecoder().decode(PopularProject.self, from: Data("""
    {"project_key":"arniesaha/drover","session_count":12,"total_tokens":1234,
     "cost_usd":1.25,"cache_read_tokens":100,"cache_write_tokens":20,
     "total_latency_ms":500,"average_latency_ms":41.7,
     "harnesses":["codex","gemini"],"hosts":["mac-mini","nas"],
     "metric":"\(metric)"}
    """.utf8))
}

private func decodeInsightSummary(
    analyzerClass: String,
    confidence: String
) throws -> InsightSummary {
    try JSONDecoder().decode(InsightSummary.self, from: Data("""
    {"finding_id":"finding-1","analyzer_id":"prompt-review",
     "rule_id":"duplicate-context","target_type":"system_prompt",
     "target_id":"codex","analyzer_class":"\(analyzerClass)",
     "severity":"medium","confidence":"\(confidence)",
     "title":"Repeated context","state":"open",
     "first_seen_at":"2026-08-08T18:00:00Z",
     "last_seen_at":"2026-08-08T18:01:00Z"}
    """.utf8))
}

private let populatedActivity = #"{"totals":{"session_count":12,"total_tokens":1234,"cost_usd":1.25,"cache_read_tokens":100,"cache_write_tokens":20,"total_latency_ms":500},"projects":[],"harnesses":[],"hosts":[],"models":[],"project_metric":"tokens","coverage":{"token_percent":86.4}}"#

private let populatedProjects = #"[{"project_key":"arniesaha/drover","session_count":12,"total_tokens":1234,"cost_usd":1.25,"cache_read_tokens":100,"cache_write_tokens":20,"total_latency_ms":500,"harnesses":["codex"],"hosts":["mac-mini"],"metric":"tokens"}]"#

private func decodeOverview(
    providerStatus: String,
    providerData: String,
    activityData: String,
    projects: String,
    insightCounts: String
) throws -> CockpitOverview {
    try JSONDecoder().decode(CockpitOverview.self, from: Data(#"{"cockpit_api_version":1,"provider_capacity":{"status":"\#(providerStatus)","data":\#(providerData)},"activity":{"status":"ok","data":\#(activityData)},"popular_projects":\#(projects),"insight_counts":\#(insightCounts)}"#.utf8))
}
