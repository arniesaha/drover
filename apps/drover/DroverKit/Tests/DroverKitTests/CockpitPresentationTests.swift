import Foundation
import Testing
@testable import DroverKit

@Test func partialConsentPresentationNamesEveryAffectedHostAndState() throws {
    let status = try presentationConsentFixture("content-consent-partial")

    let value = ContentAnalysisPropagationPresentation(
        status: status, outcome: .partial
    )

    #expect(value.isWarning)
    #expect(value.title == "Fleet propagation incomplete")
    #expect(value.hostLines == ["offline-laptop · Disconnected"])
    #expect(value.accessibilityLabel.contains("offline-laptop, Disconnected"))
}

@Test func failedAndUnknownHostStatesNeverPresentAsComplete() throws {
    let status = try presentationConsentFixture("content-consent-failed")

    let value = ContentAnalysisPropagationPresentation(
        status: status, outcome: .failed
    )

    #expect(value.isWarning)
    #expect(value.title == "Fleet propagation failed")
    #expect(value.hostLines == [
        "workstation · Failed",
    ])
}

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

@Test func zeroInsightCountsKeepInsightsNavigationReachable() throws {
    let overview = try decodeOverview(
        providerStatus: "ok",
        providerData: "[]",
        activityData: "null",
        projects: "[]",
        insightCounts: #"{"critical":0,"high":0,"medium":0,"low":0}"#
    )

    #expect(HomeSection.visible(for: overview) == [.attention, .insights, .sessions])
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

@Test func staleProviderSectionQualifiesRetainedHealthyAccount() throws {
    let value = ProviderSectionPresentation(status: .stale)

    #expect(value.isDegraded)
    #expect(value.warningText == "Provider capacity is stale. Showing last reported values.")
    #expect(value.accountStatusText(accountStatus: .ok) == "Stale")
}

@Test func unavailableProviderSectionNeverCallsRetainedHealthyAccountLive() throws {
    let value = ProviderSectionPresentation(status: .unavailable)

    #expect(value.isDegraded)
    #expect(value.warningText == "Provider usage is unavailable. Showing last reported values.")
    #expect(value.accountStatusText(accountStatus: .ok) == "Unavailable")
}

@Test func providerSectionPreservesExplicitConnectorMessage() throws {
    let value = ProviderSectionPresentation(
        status: .stale, message: "Last successful refresh was two hours ago."
    )

    #expect(value.warningText == "Last successful refresh was two hours ago. Showing last reported values.")
}

@Test func unavailableProviderWithoutRetainedValuesDoesNotClaimToShowThem() throws {
    let value = ProviderSectionPresentation(
        status: .unavailable, hasRetainedValues: false
    )

    #expect(value.warningText == "Provider usage is unavailable.")
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
    #expect(value.contributorsText == "codex, agy · mac-mini, nas")
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

@Test func canonicalCodexSourceNamesProviderReported() throws {
    let account = try JSONDecoder().decode(ProviderAccount.self, from: Data(#"""
    {"snapshot_id":"snapshot-1","dedup_key":"codex:personal","provider":"openai",
     "account_label":"Personal","host_id":"mac-mini","status":"ok",
     "observed_at":"2026-08-08T18:00:00Z","source":"codex-app-server",
     "windows":[{"kind":"primary","used_percent":20}]}
    """#.utf8))

    let value = ProviderCapacityPresentation(
        account: account,
        window: account.windows[0],
        now: Date(timeIntervalSince1970: 1_786_212_100)
    )

    #expect(value.sourceText == "Provider reported")
}

@Test func analyticsPresentationNamesAllDistributionsAndObservedFreshness() throws {
    let activity = try JSONDecoder().decode(ActivitySummary.self, from: Data(#"""
    {"totals":{"session_count":4,"total_tokens":40,"cost_usd":0,
      "cache_read_tokens":0,"cache_write_tokens":0,"total_latency_ms":40},
     "projects":[],"harnesses":[],"hosts":[],"models":[],
     "project_metric":"tokens","coverage":{"token_percent":75},
     "metadata":{"source":"drover_observed","observed_at":"1970-01-01T00:16:40Z",
      "freshness":"stale","coverage":{"token_percent":75}}}
    """#.utf8))

    #expect(AnalyticsDistributionSection.allCases.map(\.title) == [
        "Projects", "Harnesses", "Hosts", "Models",
    ])
    let value = ObservedAggregatePresentation(
        metadata: activity.metadata, fallbackCoverage: activity.coverage,
        now: Date(timeIntervalSince1970: 8_200)
    )
    #expect(value.sourceText == "Drover observed")
    #expect(value.freshnessText == "Updated 2h ago · Stale")
    #expect(value.coverageText == "75% token coverage")
}

@Test func disabledSelectionWaitsForConfirmedRevocationAndCancelRestoresActualBackend() {
    var state = ContentAnalysisSelectionState()
    state.synchronize(enabled: true, backend: .cloud, disclosureAccepted: true)

    let requiresConfirmation = state.select(.disabled)

    #expect(requiresConfirmation)
    #expect(state.displayedMode == .cloud)
    #expect(state.disclosureAccepted)

    state.cancelRevocation()

    #expect(state.displayedMode == .cloud)
    #expect(!state.isRevocationPending)

    state.synchronize(enabled: false, backend: .cloud, disclosureAccepted: false)

    #expect(state.displayedMode == .disabled)
}

@Test func partialPropagationSelectionStillMirrorsCentralConsentTruth() throws {
    let enabled = try presentationConsentFixture("content-consent-partial")
    let disabled = try presentationConsentFixture("content-consent-failed")
    var state = ContentAnalysisSelectionState()

    state.synchronize(
        enabled: enabled.enabled, backend: enabled.backend,
        disclosureAccepted: enabled.externalDisclosureAccepted
    )
    #expect(state.displayedMode == .cloud)

    state.synchronize(
        enabled: disabled.enabled, backend: disabled.backend,
        disclosureAccepted: disabled.externalDisclosureAccepted
    )
    #expect(state.displayedMode == .disabled)
}

private func decodeProviderWindow(_ json: String) throws -> ProviderWindow {
    try JSONDecoder().decode(ProviderWindow.self, from: Data(json.utf8))
}

private func decodePopularProject(metric: String) throws -> PopularProject {
    try JSONDecoder().decode(PopularProject.self, from: Data("""
    {"project_key":"arniesaha/drover","session_count":12,"total_tokens":1234,
     "cost_usd":1.25,"cache_read_tokens":100,"cache_write_tokens":20,
     "total_latency_ms":500,"average_latency_ms":41.7,
     "harnesses":["codex","agy"],"hosts":["mac-mini","nas"],
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

private func presentationConsentFixture(_ name: String) throws -> ContentAnalysisStatus {
    let url = try #require(droverKitFixtureURL(name))
    return try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data(contentsOf: url))
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

// MARK: - Subscription grouping

private func providerAccount(
    snapshot: String,
    provider: String,
    label: String,
    plan: String? = nil,
    host: String,
    status: String = "ok",
    observedAt: String,
    errorCategory: String? = nil,
    windows: String = "[]"
) throws -> ProviderAccount {
    let plan = plan.map { "\"plan_label\":\"\($0)\"," } ?? ""
    let category = errorCategory.map { "\"error_category\":\"\($0)\"," } ?? ""
    let json = """
    {"snapshot_id":"\(snapshot)","dedup_key":"\(snapshot)-key","provider":"\(provider)",\
    "account_label":"\(label)",\(plan)"host_id":"\(host)","status":"\(status)",\
    "observed_at":"\(observedAt)",\(category)"source":"codex-app-server","windows":\(windows)}
    """
    return try JSONDecoder().decode(ProviderAccount.self, from: Data(json.utf8))
}

/// The reported bug: one Codex subscription signed in on three machines
/// rendered as three identical cards, with the host nowhere on them.
@Test func oneSubscriptionOnManyHostsCollapsesToASingleEntry() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "openai", label: "me@example.com",
                        plan: "prolite", host: "mac-mini", observedAt: "2026-08-09T18:00:00Z"),
        providerAccount(snapshot: "s2", provider: "openai", label: "me@example.com",
                        plan: "prolite", host: "nas", observedAt: "2026-08-09T17:00:00Z"),
    ]

    let groups = ProviderSubscriptionGrouping.group(
        accounts, hostTitles: ["mac-mini": "Mac Mini", "nas": "NAS"]
    )

    #expect(groups.count == 1)
    #expect(groups[0].hostIDs == ["mac-mini", "nas"])
    #expect(groups[0].hostsText.contains("Mac Mini"))
    #expect(groups[0].hostsText.contains("NAS"))
    // Freshest reading wins, so the card never shows an older host's numbers.
    #expect(groups[0].representative.snapshotID == "s1")
}

/// Different accounts with the same provider must stay apart, or a broken
/// probe would be merged into a healthy subscription's card.
@Test func distinctAccountsUnderOneProviderStaySeparate() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "openai", label: "me@example.com",
                        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z"),
        providerAccount(snapshot: "s2", provider: "openai", label: "Codex",
                        host: "work-laptop", status: "error",
                        observedAt: "2026-08-09T18:00:00Z", errorCategory: "unavailable"),
    ]

    let groups = ProviderSubscriptionGrouping.group(accounts)

    #expect(groups.count == 2)
    #expect(groups.first { $0.accountLabel == "Codex" }?.status == .error)
    #expect(groups.first { $0.accountLabel == "me@example.com" }?.status == .ok)
}

/// A probe failing on one host must not blank out numbers another host
/// reported successfully — it becomes a note on the card naming that host.
@Test func aFailingHostAnnotatesTheCardRatherThanReplacingIt() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "openai", label: "me@example.com",
                        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z"),
        providerAccount(snapshot: "s2", provider: "openai", label: "me@example.com",
                        host: "work-laptop", status: "error",
                        observedAt: "2026-08-09T18:05:00Z", errorCategory: "unavailable"),
    ]

    let groups = ProviderSubscriptionGrouping.group(
        accounts, hostTitles: ["work-laptop": "work-laptop"]
    )

    #expect(groups.count == 1)
    // Healthy member represents the card even though it is the older reading.
    #expect(groups[0].status == .ok)
    #expect(groups[0].representative.snapshotID == "s1")
    let reason = try #require(groups[0].reasonText)
    #expect(reason.contains("Couldn't reach"))
    #expect(reason.contains("work-laptop"))
}

/// "Couldn't reach" sends a signed-in reader looking for a network fault. A CLI
/// the daemon cannot locate is a different repair, so it needs its own wording.
@Test func aMissingCLIReadsAsMissingRatherThanUnreachable() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "openai", label: "Codex",
                        host: "work-laptop", status: "error",
                        observedAt: "2026-08-09T18:00:00Z",
                        errorCategory: "cli_not_found"),
    ]

    let groups = ProviderSubscriptionGrouping.group(
        accounts, hostTitles: ["work-laptop": "work-laptop"]
    )

    let reason = try #require(groups[0].reasonText)
    #expect(reason.contains("CLI not found on"))
    #expect(reason.contains("work-laptop"))
}

@Test func aHealthySubscriptionCarriesNoReason() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "google", label: "Antigravity",
                        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z"),
        providerAccount(snapshot: "s2", provider: "google", label: "Antigravity",
                        host: "nas", observedAt: "2026-08-09T17:00:00Z"),
    ]

    let groups = ProviderSubscriptionGrouping.group(accounts)

    #expect(groups.count == 1)
    #expect(groups[0].reasonText == nil)
}

/// Hosts disagree about the plan: the same Anthropic account reported "max"
/// from two machines and nothing from a third, which split one subscription
/// back into the duplicate cards grouping exists to remove.
@Test func hostsDisagreeingAboutThePlanStillFormOneSubscription() throws {
    let accounts = try [
        providerAccount(snapshot: "s1", provider: "anthropic", label: "Claude Code",
                        plan: "max", host: "mac-mini", status: "usage_unavailable",
                        observedAt: "2026-08-09T18:00:00Z"),
        providerAccount(snapshot: "s2", provider: "anthropic", label: "Claude Code",
                        host: "work-laptop", status: "usage_unavailable",
                        observedAt: "2026-08-09T18:01:00Z"),
    ]

    let groups = ProviderSubscriptionGrouping.group(accounts)

    #expect(groups.count == 1)
    #expect(groups[0].hostIDs == ["mac-mini", "work-laptop"])
    // The host that could read the plan speaks for the subscription.
    #expect(groups[0].planLabel == "max")
}

/// Windows are not an OpenAI-only concept. Claude Code reports them too, and
/// the cards must render its numbers without any provider-specific branch.
@Test func anAnthropicAccountWithWindowsRendersItsNumbers() throws {
    let json = """
    {"snapshot_id":"s1","dedup_key":"k1","provider":"anthropic",\
    "account_label":"Claude Code","plan_label":"max","host_id":"mac-mini",\
    "status":"ok","observed_at":"2026-08-09T18:00:00Z","source":"claude-oauth-usage",\
    "windows":[{"kind":"five_hour","used_percent":34.5,"resets_at":"2026-08-09T20:00:00Z"}]}
    """
    let account = try JSONDecoder().decode(ProviderAccount.self, from: Data(json.utf8))

    let groups = ProviderSubscriptionGrouping.group([account])
    let value = ProviderCapacityPresentation(
        account: account,
        window: try #require(groups[0].windows.first),
        now: Date(timeIntervalSince1970: 1786000000)
    )

    #expect(groups.count == 1)
    #expect(groups[0].status == .ok)
    #expect(value.usedText.contains("34.5"))
    #expect(value.remainingText.contains("65.5"))
}

// MARK: - Headline window

private let fourAnthropicWindows = """
[{"kind":"extra_usage","used_percent":3.6},\
{"kind":"five_hour","used_percent":4,"resets_at":"2026-08-09T20:00:00Z"},\
{"kind":"nimbus_quill","used_percent":0},\
{"kind":"seven_day","used_percent":26,"resets_at":"2026-08-10T08:00:00Z"}]
"""

/// The card shows one window, and it has to be the one that will stop you
/// first. Anthropic reports four; picking a fixed one would hide a five-hour
/// window at 99% behind a seven-day window at 3%.
@Test func theTightestWindowLeadsTheCard() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "anthropic", label: "me@example.com", plan: "max",
        host: "work-laptop", observedAt: "2026-08-09T18:00:00Z",
        windows: fourAnthropicWindows
    )

    let groups = ProviderSubscriptionGrouping.group([account], now: account.observedAt)

    let headline = groups[0].headline
    #expect(headline.windowTitle == "Seven day")
    #expect(headline.usedText == "26% used")
    #expect(headline.detailText == "74% remaining · Resets in 14h")
    #expect(headline.fraction == 0.26)
    #expect(headline.isCritical == false)
}

/// Gemini and every failed probe must render the same shell as everyone else,
/// or the strip goes back to cards of four different heights.
@Test func aSubscriptionWithNoWindowsStillPresentsAHeadline() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "google", label: "Antigravity",
        host: "nas", status: "usage_unavailable", observedAt: "2026-08-09T18:00:00Z"
    )

    let headline = ProviderSubscriptionGrouping
        .group([account], now: account.observedAt)[0].headline

    #expect(headline.windowTitle == "Usage")
    #expect(headline.usedText == "Usage unavailable")
    #expect(headline.detailText == nil)
    #expect(headline.fraction == nil)
    #expect(headline.isCritical == false)
}

/// A card's bar must never disagree with its own text, so the fraction walks
/// the same limit/remaining ladder the wording does.
@Test func aUnitBasedWindowDerivesItsFractionFromTheLimit() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "openai", label: "me@example.com",
        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z",
        windows: #"[{"kind":"primary","limit_value":1000,"remaining_value":250,"unit":"credits"}]"#
    )

    let headline = ProviderSubscriptionGrouping
        .group([account], now: account.observedAt)[0].headline

    #expect(headline.fraction == 0.75)
    #expect(headline.usedText == "750 credits used")
    #expect(headline.detailText == "250 credits remaining · Reset unavailable")
}

/// A provider reporting a zero limit would divide by zero into an infinite bar.
@Test func aZeroLimitWindowHasNoFractionRatherThanAnInfinity() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "openai", label: "me@example.com",
        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z",
        windows: #"[{"kind":"primary","limit_value":0,"remaining_value":0,"unit":"credits"}]"#
    )

    let headline = ProviderSubscriptionGrouping
        .group([account], now: account.observedAt)[0].headline

    #expect(headline.fraction == nil)
}

/// The bar and the text beside it have to describe the same window. A window
/// carrying both a zero limit and a percentage used to split them: the wording
/// took the limit rung and said "0 credits used" while the fraction fell
/// through to the percentage and drew a nearly-full critical bar.
@Test func aZeroLimitWindowDoesNotFallThroughToAPercentageTheTextIgnores() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "openai", label: "me@example.com",
        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z",
        windows: #"""
        [{"kind":"primary","limit_value":0,"remaining_value":0,"unit":"credits","used_percent":90}]
        """#
    )

    let headline = ProviderSubscriptionGrouping
        .group([account], now: account.observedAt)[0].headline

    #expect(headline.usedText == "0 credits used")
    #expect(headline.fraction == nil)
    #expect(headline.isCritical == false)
}

@Test func criticalBeginsAtExactlyEightyFivePercent() throws {
    func headline(usedPercent: String) throws -> ProviderHeadline {
        let account = try providerAccount(
            snapshot: "s1", provider: "openai", label: "me@example.com",
            host: "mac-mini", observedAt: "2026-08-09T18:00:00Z",
            windows: #"[{"kind":"primary","used_percent":\#(usedPercent)}]"#
        )
        return ProviderSubscriptionGrouping.group([account], now: account.observedAt)[0].headline
    }

    #expect(try headline(usedPercent: "85").isCritical == true)
    #expect(try headline(usedPercent: "84.9").isCritical == false)
}

/// Two windows at the same fraction must not swap places between refreshes.
@Test func equalFractionsTieBreakOnWindowKind() throws {
    func headline(_ windows: String) throws -> ProviderHeadline {
        let account = try providerAccount(
            snapshot: "s1", provider: "anthropic", label: "me@example.com",
            host: "mac-mini", observedAt: "2026-08-09T18:00:00Z", windows: windows
        )
        return ProviderSubscriptionGrouping.group([account], now: account.observedAt)[0].headline
    }

    let forward = #"[{"kind":"alpha","used_percent":50},{"kind":"beta","used_percent":50}]"#
    let reversed = #"[{"kind":"beta","used_percent":50},{"kind":"alpha","used_percent":50}]"#

    #expect(try headline(forward).windowTitle == "Alpha")
    #expect(try headline(reversed).windowTitle == "Alpha")
}

/// A window nobody can put a number on tells you nothing about how full the
/// account is, so it must never outrank one that can.
@Test func aWindowWithoutAFractionRanksBehindOneThatHasIt() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "anthropic", label: "me@example.com",
        host: "mac-mini", observedAt: "2026-08-09T18:00:00Z",
        windows: #"[{"kind":"alpha"},{"kind":"beta","used_percent":2}]"#
    )

    let headline = ProviderSubscriptionGrouping
        .group([account], now: account.observedAt)[0].headline

    #expect(headline.windowTitle == "Beta")
}

/// Freshness hangs off the subscription, not off a window — an unavailable
/// card has no window and must still say when it was last read.
@Test func freshnessSurvivesASubscriptionWithNoWindows() throws {
    let account = try providerAccount(
        snapshot: "s1", provider: "google", label: "Antigravity",
        host: "nas", status: "usage_unavailable", observedAt: "2026-08-09T18:00:00Z"
    )

    let groups = ProviderSubscriptionGrouping.group(
        [account], now: account.observedAt.addingTimeInterval(120)
    )

    #expect(groups[0].freshnessText == "Updated 2m ago")
}
