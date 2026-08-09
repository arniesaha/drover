import Foundation
import Testing
@testable import DroverKit

@Suite(.serialized)
struct CockpitStoreTests {
    @Test @MainActor func insightsAvailabilityFollowsAdvertisedCapability() throws {
        let store = CockpitStore(client: CockpitClientStub())

        store.updateCapability(from: try capableSnapshot())
        #expect(store.isInsightsAvailable)

        store.updateCapability(from: try HarnessSnapshot.decode(from: Data(
            #"{"hosts":[],"sessions":[],"cockpit_api_version":1,"cockpit_sections":["activity"]}"#.utf8
        )))
        #expect(!store.isInsightsAvailable)
    }

    @Test @MainActor func refreshUsesIndependentSectionStateAndRetainsLastGoodValues() async throws {
        let client = CockpitClientStub(overviews: [
            try decodeOverview(providerStatus: "ok", activitySessions: 10),
            try decodeOverview(providerStatus: "error", activitySessions: 18),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.refresh()
        let originalProviders = store.providerAccounts
        await store.refresh()

        #expect(store.activity?.totals.sessionCount == 18)
        #expect(store.providerAccounts == originalProviders)
        #expect(store.providerError != nil)
        #expect(store.activityError == nil)
    }

    @Test @MainActor func unavailableEmptyProviderSectionKeepsLastGoodCards() async throws {
        let client = CockpitClientStub(overviews: [
            try decodeOverview(providerStatus: "ok", activitySessions: 10),
            try decodeOverview(providerStatus: "unavailable", activitySessions: 18),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.refresh()
        let originalProviders = store.providerAccounts
        await store.refresh()

        #expect(store.providerAccounts == originalProviders)
        #expect(store.providerError == "Provider usage is unavailable.")
        #expect(store.activity?.totals.sessionCount == 18)
        #expect(store.activityError == nil)
    }

    @Test @MainActor func olderRefreshCompletionCannotOverwriteNewerStateOrErrors() async throws {
        let client = ControlledRefreshCockpitClient(
            responses: [
                7: try decodeOverview(providerStatus: "error", activitySessions: 7),
                30: try decodeOverview(providerStatus: "ok", activitySessions: 30),
            ]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        let older = Task { await store.refresh(days: 7) }
        await client.waitUntilRequested(days: 7)
        let newer = Task { await store.refresh(days: 30) }
        await client.waitUntilRequested(days: 30)

        await client.release(days: 30)
        await newer.value
        await client.release(days: 7)
        await older.value

        #expect(store.activity?.totals.sessionCount == 30)
        #expect(store.providerAccounts.count == 1)
        #expect(store.providerError == nil)
        #expect(store.activityError == nil)
    }

    @Test @MainActor func olderServerNeverRequestsCockpitOrShowsAnError() async throws {
        let client = CockpitClientStub(overviews: [
            try decodeOverview(providerStatus: "error", activitySessions: 10),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.refresh()
        #expect(store.providerError != nil)

        store.updateCapability(from: try oldSnapshot())

        await store.refresh()

        #expect(await client.overviewRequestCount == 1)
        #expect(store.providerError == nil)
        #expect(store.activityError == nil)
        #expect(store.activity?.totals.sessionCount == 10)
    }

    @Test @MainActor func cancelledRefreshKeepsContentAndSuppressesErrors() async throws {
        let client = CockpitClientStub(overviews: [
            try decodeOverview(providerStatus: "ok", activitySessions: 10),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.refresh()
        await client.setError(.transport(DroverError.cancellationDetail))

        await store.refresh()

        #expect(store.activity?.totals.sessionCount == 10)
        #expect(store.providerAccounts.count == 1)
        #expect(store.providerError == nil)
        #expect(store.activityError == nil)
    }

    @Test @MainActor func analyticsAndInsightsLoadOnlyOnDemand() async throws {
        let client = CockpitClientStub(
            analytics: try decodeAnalytics(),
            insightPages: [try decodeInsightPage(ids: ["one"], nextCursor: nil)]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.refresh()
        #expect(await client.analyticsRequestCount == 0)
        #expect(await client.insightsRequestCount == 0)

        await store.loadAnalytics()
        await store.loadInsights()

        #expect(await client.analyticsRequestCount == 1)
        #expect(await client.insightsRequestCount == 1)
        #expect(store.analytics?.activity.data?.totals.sessionCount == 22)
        #expect(store.insights.map(\.findingID) == ["one"])
    }

    @Test @MainActor func insightCursorAppendsWithoutReplacingExistingRows() async throws {
        let client = CockpitClientStub(insightPages: [
            try decodeInsightPage(ids: ["one", "two"], nextCursor: "page-2"),
            try decodeInsightPage(ids: ["three"], nextCursor: nil),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.loadInsights()
        await store.loadMoreInsights()

        #expect(store.insights.map(\.findingID) == ["one", "two", "three"])
        #expect(store.nextInsightsCursor == nil)
        #expect(await client.requestedCursors == [nil, "page-2"])
    }

    @Test @MainActor func optimisticAcknowledgeRollsBackOnFailure() async throws {
        let client = CockpitClientStub(
            insightPages: [try decodeInsightPage(ids: ["one"], nextCursor: nil)],
            lifecycleError: .httpStatus(500, "could not acknowledge"),
            lifecycleDelay: .milliseconds(80)
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights()

        let task = Task { await store.acknowledgeInsight(findingID: "one") }
        try await Task.sleep(for: .milliseconds(10))
        #expect(store.state(forFindingID: "one") == .acknowledged)
        let succeeded = await task.value

        #expect(!succeeded)
        #expect(store.state(forFindingID: "one") == .open)
        #expect(store.lifecycleError == "could not acknowledge")
    }

    @Test @MainActor func dismissalRequiresANonblankReasonBeforeRequesting() async throws {
        let client = CockpitClientStub()
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        let succeeded = await store.dismissInsight(findingID: "one", reason: "  \n")

        #expect(!succeeded)
        #expect(store.lifecycleError == "A dismissal reason is required.")
        #expect(await client.dismissRequestCount == 0)
    }

    @Test @MainActor func concurrentLifecycleActionsAreSerialized() async throws {
        let finding = try decodeInsightFinding(id: "one", state: "acknowledged")
        let client = CockpitClientStub(acknowledgedFinding: finding, lifecycleDelay: .milliseconds(80))
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        async let first = store.acknowledgeInsight(findingID: "one")
        async let second = store.acknowledgeInsight(findingID: "two")
        _ = await (first, second)

        #expect(await client.maximumConcurrentLifecycleRequests == 1)
        #expect(await client.acknowledgeRequestCount == 2)
    }

    @Test @MainActor func foregroundPollingStartsAndStopsWithSceneLifecycle() async throws {
        let client = CockpitClientStub(overviews: [
            try decodeOverview(providerStatus: "ok", activitySessions: 10),
        ])
        let store = CockpitStore(client: client)

        store.startForegroundPolling(for: try capableSnapshot(), every: 60)
        #expect(store.isPolling)
        store.stopForegroundPolling()

        #expect(!store.isPolling)
    }

    @Test @MainActor func cloudConsentRequiresDisclosureAcknowledgementBeforeRequesting() async throws {
        let client = CockpitClientStub()
        let store = CockpitStore(client: client)

        let succeeded = await store.enableContentAnalysis(
            backend: .cloud,
            disclosureAccepted: false
        )

        #expect(!succeeded)
        #expect(store.contentConsentError == CockpitStore.cloudDisclosureRequiredMessage)
        #expect(await client.contentConsentRequestCount == 0)
    }

    @Test @MainActor func localConsentDoesNotRequireExternalDisclosure() async throws {
        let client = CockpitClientStub(contentStatus: try decodeContentStatus(
            enabled: true,
            backend: "local",
            disclosureAccepted: false
        ))
        let store = CockpitStore(client: client)

        let succeeded = await store.enableContentAnalysis(
            backend: .local,
            disclosureAccepted: false
        )

        #expect(succeeded)
        #expect(store.contentAnalysisStatus?.enabled == true)
        #expect(store.contentAnalysisStatus?.backend == .local)
        #expect(store.contentConsentError == nil)
        #expect(await client.requestedContentConsents == [.init(
            backend: .local,
            disclosureAccepted: false
        )])
    }

    @Test @MainActor func revocationPreservesFindingsAndReportsItsOwnFailure() async throws {
        let client = CockpitClientStub(
            insightPages: [try decodeInsightPage(ids: ["one"], nextCursor: nil)],
            contentError: .httpStatus(503, "revocation unavailable")
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights()

        let succeeded = await store.revokeContentAnalysis()

        #expect(!succeeded)
        #expect(store.insights.map(\.findingID) == ["one"])
        #expect(store.contentRevocationError == "revocation unavailable")
        #expect(store.contentConsentError == nil)
        #expect(store.contentPurgeError == nil)
    }

    @Test @MainActor func confirmedRevocationCallsServerUpdatesStatusAndPreservesFindings() async throws {
        let revoked = try decodeContentStatus(
            enabled: false,
            backend: "cloud",
            disclosureAccepted: false
        )
        let client = CockpitClientStub(
            insightPages: [try decodeInsightPage(ids: ["one"], nextCursor: nil)],
            contentStatus: revoked
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights()

        let succeeded = await store.revokeContentAnalysis()

        #expect(succeeded)
        #expect(store.contentAnalysisStatus == revoked)
        #expect(store.insights.map(\.findingID) == ["one"])
        #expect(await client.revokeRequestCount == 1)
    }

    @Test @MainActor func purgeReportsCountWithoutChangingConsentOrFindings() async throws {
        let status = try decodeContentStatus(
            enabled: true,
            backend: "local",
            disclosureAccepted: false
        )
        let client = CockpitClientStub(
            insightPages: [try decodeInsightPage(ids: ["one"], nextCursor: nil)],
            contentStatus: status,
            purgedExcerptCount: 7
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights()
        _ = await store.enableContentAnalysis(backend: .local, disclosureAccepted: false)

        let succeeded = await store.purgeContentExcerpts()

        #expect(succeeded)
        #expect(store.purgedExcerptCount == 7)
        #expect(store.contentAnalysisStatus == status)
        #expect(store.insights.map(\.findingID) == ["one"])
        #expect(CockpitStore.revokeConfirmationMessage.contains("findings remain"))
        #expect(CockpitStore.purgeConfirmationMessage.contains("does not disable"))
    }
}

private struct RequestedContentConsent: Sendable, Equatable {
    let backend: ContentAnalysisBackend
    let disclosureAccepted: Bool
}

private actor CockpitClientStub: CockpitClient {
    private var overviews: [CockpitOverview]
    private let analyticsValue: AnalyticsSnapshot?
    private var pages: [InsightPage]
    private let acknowledgedFinding: InsightFinding?
    private let lifecycleError: DroverError?
    private let lifecycleDelay: Duration
    private let contentStatusValue: ContentAnalysisStatus?
    private let contentError: DroverError?
    private let purgedExcerptCount: Int
    private var refreshError: DroverError?

    private(set) var overviewRequestCount = 0
    private(set) var analyticsRequestCount = 0
    private(set) var insightsRequestCount = 0
    private(set) var requestedCursors: [String?] = []
    private(set) var dismissRequestCount = 0
    private(set) var acknowledgeRequestCount = 0
    private(set) var maximumConcurrentLifecycleRequests = 0
    private(set) var contentConsentRequestCount = 0
    private(set) var requestedContentConsents: [RequestedContentConsent] = []
    private(set) var revokeRequestCount = 0
    private var currentLifecycleRequests = 0

    init(
        overviews: [CockpitOverview] = [],
        analytics: AnalyticsSnapshot? = nil,
        insightPages: [InsightPage] = [],
        acknowledgedFinding: InsightFinding? = nil,
        lifecycleError: DroverError? = nil,
        lifecycleDelay: Duration = .zero,
        contentStatus: ContentAnalysisStatus? = nil,
        contentError: DroverError? = nil,
        purgedExcerptCount: Int = 0
    ) {
        self.overviews = overviews
        self.analyticsValue = analytics
        self.pages = insightPages
        self.acknowledgedFinding = acknowledgedFinding
        self.lifecycleError = lifecycleError
        self.lifecycleDelay = lifecycleDelay
        self.contentStatusValue = contentStatus
        self.contentError = contentError
        self.purgedExcerptCount = purgedExcerptCount
    }

    func setError(_ error: DroverError?) { refreshError = error }

    func cockpitOverview(days: Int) async throws -> CockpitOverview {
        overviewRequestCount += 1
        if let refreshError { throw refreshError }
        guard !overviews.isEmpty else { throw DroverError.unavailable("no overview") }
        return overviews.removeFirst()
    }

    func analytics(filters: AnalyticsFilters) async throws -> AnalyticsSnapshot {
        analyticsRequestCount += 1
        guard let analyticsValue else { throw DroverError.unavailable("no analytics") }
        return analyticsValue
    }

    func insights(filters: InsightFilters) async throws -> InsightPage {
        insightsRequestCount += 1
        requestedCursors.append(filters.cursor)
        guard !pages.isEmpty else { throw DroverError.unavailable("no insights") }
        return pages.removeFirst()
    }

    func acknowledgeInsight(findingID: String) async throws -> InsightFinding {
        acknowledgeRequestCount += 1
        currentLifecycleRequests += 1
        maximumConcurrentLifecycleRequests = max(
            maximumConcurrentLifecycleRequests, currentLifecycleRequests
        )
        defer { currentLifecycleRequests -= 1 }
        if lifecycleDelay != .zero { try await Task.sleep(for: lifecycleDelay) }
        if let lifecycleError { throw lifecycleError }
        guard let acknowledgedFinding else {
            throw DroverError.unavailable("no acknowledged finding")
        }
        return acknowledgedFinding
    }

    func dismissInsight(findingID: String, reason: String) async throws -> InsightFinding {
        dismissRequestCount += 1
        if let lifecycleError { throw lifecycleError }
        return try decodeInsightFinding(id: findingID, state: "dismissed")
    }

    func checkInsight(findingID: String) async throws -> InsightCheckResponse {
        throw DroverError.unavailable("not configured")
    }

    func contentAnalysisStatus() async throws -> ContentAnalysisStatus {
        if let contentError { throw contentError }
        guard let contentStatusValue else { throw DroverError.unavailable("not configured") }
        return contentStatusValue
    }

    func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool
    ) async throws -> ContentAnalysisStatus {
        contentConsentRequestCount += 1
        requestedContentConsents.append(.init(
            backend: backend,
            disclosureAccepted: externalDisclosureAccepted
        ))
        if let contentError { throw contentError }
        guard let contentStatusValue else { throw DroverError.unavailable("not configured") }
        return contentStatusValue
    }

    func revokeContentAnalysis() async throws -> ContentAnalysisStatus {
        revokeRequestCount += 1
        if let contentError { throw contentError }
        guard let contentStatusValue else { throw DroverError.unavailable("not configured") }
        return contentStatusValue
    }

    func purgeContentExcerpts() async throws -> PurgeContentExcerptsResponse {
        if let contentError { throw contentError }
        return try JSONDecoder().decode(
            PurgeContentExcerptsResponse.self,
            from: Data("{\"purged_excerpt_count\":\(purgedExcerptCount)}".utf8)
        )
    }
}

private actor ControlledRefreshCockpitClient: CockpitClient {
    private let responses: [Int: CockpitOverview]
    private var requestedDays: Set<Int> = []
    private var requestWaiters: [Int: [CheckedContinuation<Void, Never>]] = [:]
    private var releaseWaiters: [Int: CheckedContinuation<Void, Never>] = [:]

    init(responses: [Int: CockpitOverview]) {
        self.responses = responses
    }

    func waitUntilRequested(days: Int) async {
        guard !requestedDays.contains(days) else { return }
        await withCheckedContinuation { continuation in
            requestWaiters[days, default: []].append(continuation)
        }
    }

    func release(days: Int) {
        releaseWaiters.removeValue(forKey: days)?.resume()
    }

    func cockpitOverview(days: Int) async throws -> CockpitOverview {
        requestedDays.insert(days)
        requestWaiters.removeValue(forKey: days)?.forEach { $0.resume() }
        await withCheckedContinuation { continuation in
            releaseWaiters[days] = continuation
        }
        guard let response = responses[days] else {
            throw DroverError.unavailable("no response for \(days) days")
        }
        return response
    }

    func analytics(filters: AnalyticsFilters) async throws -> AnalyticsSnapshot {
        throw DroverError.unavailable("not configured")
    }

    func insights(filters: InsightFilters) async throws -> InsightPage {
        throw DroverError.unavailable("not configured")
    }

    func acknowledgeInsight(findingID: String) async throws -> InsightFinding {
        throw DroverError.unavailable("not configured")
    }

    func dismissInsight(findingID: String, reason: String) async throws -> InsightFinding {
        throw DroverError.unavailable("not configured")
    }

    func checkInsight(findingID: String) async throws -> InsightCheckResponse {
        throw DroverError.unavailable("not configured")
    }

    func contentAnalysisStatus() async throws -> ContentAnalysisStatus {
        throw DroverError.unavailable("not configured")
    }

    func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool
    ) async throws -> ContentAnalysisStatus {
        throw DroverError.unavailable("not configured")
    }

    func revokeContentAnalysis() async throws -> ContentAnalysisStatus {
        throw DroverError.unavailable("not configured")
    }

    func purgeContentExcerpts() async throws -> PurgeContentExcerptsResponse {
        throw DroverError.unavailable("not configured")
    }
}

private func capableSnapshot() throws -> HarnessSnapshot {
    try HarnessSnapshot.decode(from: Data(
        #"{"hosts":[],"sessions":[],"cockpit_api_version":1,"cockpit_sections":["provider_capacity","activity","popular_projects","insights"]}"#.utf8
    ))
}

private func oldSnapshot() throws -> HarnessSnapshot {
    try HarnessSnapshot.decode(from: Data(#"{"hosts":[],"sessions":[]}"#.utf8))
}

private func decodeOverview(providerStatus: String, activitySessions: Int) throws -> CockpitOverview {
    let providerData = providerStatus == "ok" ? """
    [{"snapshot_id":"snapshot-1","dedup_key":"codex:personal",
      "provider":"openai","account_label":"Personal","plan_label":"Plus",
      "host_id":"mac-mini","status":"ok","observed_at":"2026-08-08T18:00:00Z",
      "windows":[],"source":"codex_app_server"}]
    """ : (providerStatus == "unavailable" ? "[]" : "null")
    return try JSONDecoder().decode(CockpitOverview.self, from: Data("""
    {"cockpit_api_version":1,
     "provider_capacity":{"status":"\(providerStatus)","observed_at":"2026-08-08T18:00:00Z",
       "coverage":{"source":"provider_reported"},"data":\(providerData)},
     "activity":{"status":"ok","observed_at":"2026-08-08T18:01:00Z",
       "coverage":{"token_percent":42},"data":\(activityJSON(sessions: activitySessions))},
     "popular_projects":[],"insight_counts":{"critical":0,"high":0,"medium":1,"low":0}}
    """.utf8))
}

private func decodeAnalytics() throws -> AnalyticsSnapshot {
    try JSONDecoder().decode(AnalyticsSnapshot.self, from: Data("""
    {"cockpit_api_version":1,"filters":{"days":7},
     "provider_capacity":{"status":"ok","data":[]},
     "activity":{"status":"ok","coverage":{"token_percent":90},
       "data":\(activityJSON(sessions: 22))}}
    """.utf8))
}

private func activityJSON(sessions: Int) -> String {
    """
    {"totals":{"session_count":\(sessions),"total_tokens":1000,"cost_usd":1.0,
      "cache_read_tokens":100,"cache_write_tokens":10,"total_latency_ms":500,
      "average_latency_ms":25},"projects":[],"harnesses":[],"hosts":[],"models":[],
      "project_metric":"sessions","coverage":{"token_percent":42}}
    """
}

private func decodeInsightPage(ids: [String], nextCursor: String?) throws -> InsightPage {
    let findings = ids.map { insightSummaryJSON(id: $0) }.joined(separator: ",")
    let cursor = nextCursor.map { "\"\($0)\"" } ?? "null"
    return try JSONDecoder().decode(InsightPage.self, from: Data(
        "{\"findings\":[\(findings)],\"next_cursor\":\(cursor)}".utf8
    ))
}

private func insightSummaryJSON(id: String) -> String {
    """
    {"finding_id":"\(id)","analyzer_id":"health","rule_id":"stale",
     "target_type":"host","target_id":"mac-mini","analyzer_class":"deterministic",
     "severity":"medium","confidence":"confirmed","title":"Stale host","state":"open",
     "first_seen_at":"2026-08-08T18:00:00Z","last_seen_at":"2026-08-08T18:01:00Z"}
    """
}

private func decodeInsightFinding(id: String, state: String) throws -> InsightFinding {
    try JSONDecoder().decode(InsightFinding.self, from: Data("""
    {"finding_id":"\(id)","analyzer_id":"health","rule_id":"stale",
     "target_type":"host","target_id":"mac-mini","analyzer_class":"deterministic",
     "severity":"medium","confidence":"confirmed","title":"Stale host",
     "impact":"May miss work","remediation":["Reconnect"],"state":"\(state)",
     "first_seen_at":"2026-08-08T18:00:00Z","last_seen_at":"2026-08-08T18:01:00Z"}
    """.utf8))
}

private func decodeContentStatus(
    enabled: Bool,
    backend: String,
    disclosureAccepted: Bool
) throws -> ContentAnalysisStatus {
    try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data("""
    {"enabled":\(enabled),"backend":"\(backend)",
     "external_disclosure_accepted":\(disclosureAccepted),"pending_model_jobs":0}
    """.utf8))
}
