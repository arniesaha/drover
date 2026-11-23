import DroverKit
import Foundation
import Testing
@testable import Drover

@Suite(.serialized)
struct CockpitRootLifecycleTests {
    @Test @MainActor func lateBackgroundSnapshotCannotRestartPollingAndForegroundRefreshes() async throws {
        let client = LifecycleCockpitClient(overviews: [try overview(), try overview()])
        let store = CockpitStore(client: client)
        let snapshot = try capableSnapshot()

        #expect(CockpitRootLifecycle.apply(
            snapshot: snapshot, isActive: true, cockpitStore: store
        ))
        try await waitForOverview(store, client: client, count: 1)
        #expect(store.isPolling)

        store.stopForegroundPolling()
        #expect(!store.isPolling)

        #expect(!CockpitRootLifecycle.apply(
            snapshot: snapshot, isActive: false, cockpitStore: store
        ))
        for _ in 0 ..< 5 { await Task.yield() }
        #expect(!store.isPolling)
        #expect(store.isCockpitAvailable)
        #expect(await client.overviewRequestCount == 1)

        #expect(!CockpitRootLifecycle.apply(
            snapshot: nil, isActive: false, cockpitStore: store
        ))
        #expect(!store.isCockpitAvailable)
        #expect(!store.isPolling)

        #expect(CockpitRootLifecycle.apply(
            snapshot: snapshot, isActive: true, cockpitStore: store
        ))
        try await waitForOverview(store, client: client, count: 2)
        #expect(store.isPolling)
        #expect(store.overview != nil)
        store.stopForegroundPolling()
    }

    @MainActor
    private func waitForOverview(
        _ store: CockpitStore,
        client: LifecycleCockpitClient,
        count: Int
    ) async throws {
        for _ in 0 ..< 50 {
            if store.overview != nil, await client.overviewRequestCount >= count { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        throw LifecycleTestError.timedOut
    }

    private func capableSnapshot() throws -> HarnessSnapshot {
        try HarnessSnapshot.decode(from: Data(
            #"{"hosts":[],"sessions":[],"cockpit_api_version":1,"cockpit_sections":["provider_capacity","activity"]}"#.utf8
        ))
    }

    private func overview() throws -> CockpitOverview {
        try JSONDecoder().decode(CockpitOverview.self, from: Data("""
        {"cockpit_api_version":1,
         "provider_capacity":{"status":"ok","data":[]},
         "activity":{"status":"ok","coverage":{"token_percent":100},
           "data":{"totals":{"session_count":1,"total_tokens":1,"cost_usd":0,
             "cache_read_tokens":0,"cache_write_tokens":0,"total_latency_ms":1,
             "average_latency_ms":1},"projects":[],"harnesses":[],"hosts":[],"models":[],
             "project_metric":"sessions","coverage":{"token_percent":100}}},
         "popular_projects":[]}
        """.utf8))
    }
}

private enum LifecycleTestError: Error {
    case timedOut
}

private actor LifecycleCockpitClient: CockpitClient {
    private var overviews: [CockpitOverview]
    private(set) var overviewRequestCount = 0

    init(overviews: [CockpitOverview]) {
        self.overviews = overviews
    }

    func cockpitOverview(days: Int) async throws -> CockpitOverview {
        overviewRequestCount += 1
        guard !overviews.isEmpty else { throw DroverError.unavailable("no overview") }
        return overviews.removeFirst()
    }

    func analytics(filters: AnalyticsFilters) async throws -> AnalyticsSnapshot {
        throw DroverError.unavailable("not used")
    }

    func insights(filters: InsightFilters) async throws -> InsightPage {
        throw DroverError.unavailable("not used")
    }

    func acknowledgeInsight(findingID: String) async throws -> InsightFinding {
        throw DroverError.unavailable("not used")
    }

    func dismissInsight(findingID: String, reason: String) async throws -> InsightFinding {
        throw DroverError.unavailable("not used")
    }

    func checkInsight(findingID: String) async throws -> InsightCheckResponse {
        throw DroverError.unavailable("not used")
    }

    func contentAnalysisStatus() async throws -> ContentAnalysisStatus {
        throw DroverError.unavailable("not used")
    }

    func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool
    ) async throws -> ContentAnalysisConsentResult {
        throw DroverError.unavailable("not used")
    }

    func revokeContentAnalysis() async throws -> ContentAnalysisConsentResult {
        throw DroverError.unavailable("not used")
    }

    func purgeContentExcerpts() async throws -> PurgeContentExcerptsResponse {
        throw DroverError.unavailable("not used")
    }
}
