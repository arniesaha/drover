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

    @Test @MainActor func analyticsDimensionsPageIndependentlyAndDedupeRows() async throws {
        let client = CockpitClientStub(analyticsPages: [
            try decodeAnalyticsPage(projects: ["one", "two"], hosts: ["mac"],
                                    projectCursor: "projects-2", hostCursor: "hosts-2"),
            try decodeAnalyticsPage(projects: ["two", "three"], hosts: [],
                                    projectCursor: nil, hostCursor: nil),
            try decodeAnalyticsPage(projects: [], hosts: ["mac", "nas"],
                                    projectCursor: nil, hostCursor: nil),
        ])
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.loadAnalytics(filters: AnalyticsFilters(days: 30, limit: 2))
        await store.loadMoreAnalytics(.projects)
        await store.loadMoreAnalytics(.hosts)

        #expect(store.analyticsProjects.map(\.projectKey) == ["one", "two", "three"])
        #expect(store.analyticsHosts.map(\.key) == ["mac", "nas"])
        #expect(store.nextAnalyticsCursor(for: .projects) == nil)
        #expect(store.nextAnalyticsCursor(for: .hosts) == nil)
        let filters = await client.requestedAnalyticsFilters
        #expect(filters.map(\.days) == [30, 30, 30])
        #expect(filters[1].projectCursor == "projects-2")
        #expect(filters[1].hostCursor == nil)
        #expect(filters[2].hostCursor == "hosts-2")
        #expect(filters[2].projectCursor == nil)
    }

    @Test @MainActor func analyticsPageFailureIsIsolatedAndRetainsItsCursor() async throws {
        let client = CockpitClientStub(
            analyticsPages: [
                try decodeAnalyticsPage(projects: ["one"], hosts: ["mac"],
                                        projectCursor: "projects-2", hostCursor: "hosts-2"),
                try decodeAnalyticsPage(projects: ["two"], hosts: [],
                                        projectCursor: nil, hostCursor: nil),
            ],
            failingAnalyticsCursors: ["hosts-2"]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        await store.loadAnalytics()
        await store.loadMoreAnalytics(.hosts)
        await store.loadMoreAnalytics(.projects)

        #expect(store.analyticsPaginationError(for: .hosts) == "analytics page failed")
        #expect(store.analyticsPaginationError(for: .projects) == nil)
        #expect(store.nextAnalyticsCursor(for: .hosts) == "hosts-2")
        #expect(store.analyticsHosts.map(\.key) == ["mac"])
        #expect(store.analyticsProjects.map(\.projectKey) == ["one", "two"])
    }

    @Test @MainActor func newerAnalyticsFilterGenerationIgnoresOlderResponse() async throws {
        let client = CockpitClientStub(
            analyticsByDays: [
                7: try decodeAnalyticsPage(projects: ["old"], hosts: [],
                                           projectCursor: nil, hostCursor: nil),
                30: try decodeAnalyticsPage(projects: ["new"], hosts: [],
                                            projectCursor: nil, hostCursor: nil),
            ],
            analyticsDelays: [7: .milliseconds(80)]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        let old = Task { await store.loadAnalytics(filters: AnalyticsFilters(days: 7)) }
        try await Task.sleep(for: .milliseconds(10))
        await store.loadAnalytics(filters: AnalyticsFilters(days: 30))
        await old.value

        #expect(store.analyticsProjects.map(\.projectKey) == ["new"])
    }

    @Test @MainActor func stalePageCompletionDoesNotClearNewGenerationLoadingState() async throws {
        let oldInitial = try decodeAnalyticsPage(
            projects: ["old"], hosts: [], projectCursor: "old-page", hostCursor: nil
        )
        let newInitial = try decodeAnalyticsPage(
            projects: ["new"], hosts: [], projectCursor: "new-page", hostCursor: nil
        )
        let client = CockpitClientStub(
            analyticsPages: [oldInitial, newInitial],
            analyticsByCursor: [
                "old-page": try decodeAnalyticsPage(
                    projects: ["old-more"], hosts: [], projectCursor: nil, hostCursor: nil
                ),
                "new-page": try decodeAnalyticsPage(
                    projects: ["new-more"], hosts: [], projectCursor: nil, hostCursor: nil
                ),
            ],
            analyticsCursorDelays: [
                "old-page": .milliseconds(80), "new-page": .milliseconds(500),
            ]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadAnalytics(filters: AnalyticsFilters(days: 7))

        let oldPage = Task { await store.loadMoreAnalytics(.projects) }
        try await Task.sleep(for: .milliseconds(10))
        await store.loadAnalytics(filters: AnalyticsFilters(days: 30))
        let newPage = Task { await store.loadMoreAnalytics(.projects) }
        try await Task.sleep(for: .milliseconds(20))
        await oldPage.value

        #expect(store.isLoadingAnalytics(.projects))
        await newPage.value
        #expect(store.analyticsProjects.map(\.projectKey) == ["new", "new-more"])
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

    @Test @MainActor func newerInsightFilterCancelsAndIgnoresOlderCompletion() async throws {
        let client = CockpitClientStub(
            insightsByHost: [
                "old": try decodeInsightPage(ids: ["old"], nextCursor: "old-page"),
                "new": try decodeInsightPage(ids: ["new"], nextCursor: nil),
            ],
            insightDelaysByHost: ["old": .milliseconds(80)]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        let old = Task { await store.loadInsights(filters: InsightFilters(host: "old")) }
        try await Task.sleep(for: .milliseconds(10))
        await store.loadInsights(filters: InsightFilters(host: "new"))
        await old.value

        #expect(store.insights.map(\.findingID) == ["new"])
        #expect(store.nextInsightsCursor == nil)
        #expect(store.insightsError == nil)
        #expect(!store.isLoadingInsights)
    }

    @Test @MainActor func staleInsightFilterErrorCannotOverwriteNewerSuccess() async throws {
        let client = CockpitClientStub(
            insightsByHost: [
                "new": try decodeInsightPage(ids: ["new"], nextCursor: nil),
            ],
            insightDelaysByHost: ["old": .milliseconds(80)],
            failingInsightHosts: ["old"]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())

        let old = Task { await store.loadInsights(filters: InsightFilters(host: "old")) }
        try await Task.sleep(for: .milliseconds(10))
        await store.loadInsights(filters: InsightFilters(host: "new"))
        await old.value

        #expect(store.insights.map(\.findingID) == ["new"])
        #expect(store.insightsError == nil)
    }

    @Test @MainActor func newInsightFilterImmediatelyResetsOldCursorWhileLoading() async throws {
        let client = CockpitClientStub(
            insightsByHost: [
                "old": try decodeInsightPage(ids: ["old"], nextCursor: "old-page"),
                "new": try decodeInsightPage(ids: ["new"], nextCursor: nil),
            ],
            insightDelaysByHost: ["new": .milliseconds(80)]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights(filters: InsightFilters(host: "old"))

        let loading = Task { await store.loadInsights(filters: InsightFilters(host: "new")) }
        try await Task.sleep(for: .milliseconds(10))

        #expect(store.nextInsightsCursor == nil)
        #expect(store.isLoadingInsights)
        await loading.value
    }

    @Test @MainActor func staleInsightPageCannotAppendOrClearNewPageLoadingState() async throws {
        let client = CockpitClientStub(
            insightsByHost: [
                "old": try decodeInsightPage(ids: ["old"], nextCursor: "old-page"),
                "new": try decodeInsightPage(ids: ["new"], nextCursor: "new-page"),
            ],
            insightsByCursor: [
                "old-page": try decodeInsightPage(ids: ["old-more"], nextCursor: nil),
                "new-page": try decodeInsightPage(ids: ["new-more"], nextCursor: nil),
            ],
            insightCursorDelays: [
                "old-page": .milliseconds(80), "new-page": .milliseconds(300),
            ]
        )
        let store = CockpitStore(client: client)
        store.updateCapability(from: try capableSnapshot())
        await store.loadInsights(filters: InsightFilters(host: "old"))

        let oldPage = Task { await store.loadMoreInsights() }
        try await Task.sleep(for: .milliseconds(10))
        await store.loadInsights(filters: InsightFilters(host: "new"))
        let newPage = Task { await store.loadMoreInsights() }
        try await Task.sleep(for: .milliseconds(20))
        await oldPage.value

        #expect(store.insights.map(\.findingID) == ["new"])
        #expect(store.nextInsightsCursor == "new-page")
        #expect(store.isLoadingMoreInsights)
        await newPage.value
        #expect(store.insights.map(\.findingID) == ["new", "new-more"])
        #expect(!store.isLoadingMoreInsights)
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

    @Test @MainActor func partialEnableKeepsCentralEnabledTruthAndFleetOutcome() async throws {
        let status = try contentConsentFixture("content-consent-partial")
        let client = CockpitClientStub(
            contentStatus: status, contentMutationOutcome: .partial
        )
        let store = CockpitStore(client: client)

        let completed = await store.enableContentAnalysis(
            backend: .cloud, disclosureAccepted: true
        )

        #expect(!completed)
        #expect(store.contentAnalysisStatus?.enabled == true)
        #expect(store.contentConsentOutcome == .partial)
        #expect(store.contentAnalysisStatus?.affectedHosts.map(\.hostID) == ["offline-laptop"])
    }

    @Test @MainActor func statusReloadSurfacesServerReportedPartialPropagation() async throws {
        let status = try contentConsentFixture("content-consent-partial")
        let store = CockpitStore(client: CockpitClientStub(contentStatus: status))

        await store.loadContentAnalysisStatus()

        #expect(store.contentAnalysisStatus?.enabled == true)
        #expect(store.contentConsentOutcome == .partial)
    }

    @Test @MainActor func failedRevokeKeepsCentralDisabledTruthAndCanRetryConsentOnly() async throws {
        let status = try contentConsentFixture("content-consent-failed")
        let client = CockpitClientStub(
            contentStatus: status, contentMutationOutcome: .failed
        )
        let store = CockpitStore(client: client)

        let completed = await store.revokeContentAnalysis()
        let retried = await store.retryContentAnalysisPropagation()

        #expect(!completed)
        #expect(!retried)
        #expect(store.contentAnalysisStatus?.enabled == false)
        #expect(store.contentRevocationOutcome == .failed)
        #expect(await client.revokeRequestCount == 2)
        #expect(await client.contentConsentRequestCount == 0)
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
    private var analyticsValues: [AnalyticsSnapshot]
    private var pages: [InsightPage]
    private let acknowledgedFinding: InsightFinding?
    private let lifecycleError: DroverError?
    private let lifecycleDelay: Duration
    private let contentStatusValue: ContentAnalysisStatus?
    private let contentMutationOutcome: ContentAnalysisMutationOutcome
    private let contentError: DroverError?
    private let purgedExcerptCount: Int
    private var refreshError: DroverError?
    private let failingAnalyticsCursors: Set<String>
    private let analyticsByDays: [Int: AnalyticsSnapshot]
    private let analyticsDelays: [Int: Duration]
    private let analyticsByCursor: [String: AnalyticsSnapshot]
    private let analyticsCursorDelays: [String: Duration]
    private let insightsByHost: [String: InsightPage]
    private let insightDelaysByHost: [String: Duration]
    private let insightsByCursor: [String: InsightPage]
    private let insightCursorDelays: [String: Duration]
    private let failingInsightHosts: Set<String>

    private(set) var overviewRequestCount = 0
    private(set) var analyticsRequestCount = 0
    private(set) var requestedAnalyticsFilters: [AnalyticsFilters] = []
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
        analyticsPages: [AnalyticsSnapshot] = [],
        failingAnalyticsCursors: Set<String> = [],
        analyticsByDays: [Int: AnalyticsSnapshot] = [:],
        analyticsDelays: [Int: Duration] = [:],
        analyticsByCursor: [String: AnalyticsSnapshot] = [:],
        analyticsCursorDelays: [String: Duration] = [:],
        insightPages: [InsightPage] = [],
        insightsByHost: [String: InsightPage] = [:],
        insightDelaysByHost: [String: Duration] = [:],
        insightsByCursor: [String: InsightPage] = [:],
        insightCursorDelays: [String: Duration] = [:],
        failingInsightHosts: Set<String> = [],
        acknowledgedFinding: InsightFinding? = nil,
        lifecycleError: DroverError? = nil,
        lifecycleDelay: Duration = .zero,
        contentStatus: ContentAnalysisStatus? = nil,
        contentMutationOutcome: ContentAnalysisMutationOutcome = .complete,
        contentError: DroverError? = nil,
        purgedExcerptCount: Int = 0
    ) {
        self.overviews = overviews
        self.analyticsValues = analyticsPages.isEmpty ? analytics.map { [$0] } ?? [] : analyticsPages
        self.failingAnalyticsCursors = failingAnalyticsCursors
        self.analyticsByDays = analyticsByDays
        self.analyticsDelays = analyticsDelays
        self.analyticsByCursor = analyticsByCursor
        self.analyticsCursorDelays = analyticsCursorDelays
        self.pages = insightPages
        self.insightsByHost = insightsByHost
        self.insightDelaysByHost = insightDelaysByHost
        self.insightsByCursor = insightsByCursor
        self.insightCursorDelays = insightCursorDelays
        self.failingInsightHosts = failingInsightHosts
        self.acknowledgedFinding = acknowledgedFinding
        self.lifecycleError = lifecycleError
        self.lifecycleDelay = lifecycleDelay
        self.contentStatusValue = contentStatus
        self.contentMutationOutcome = contentMutationOutcome
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
        requestedAnalyticsFilters.append(filters)
        if let delay = analyticsDelays[filters.days] { try await Task.sleep(for: delay) }
        let cursor = filters.projectCursor ?? filters.harnessCursor
            ?? filters.hostCursor ?? filters.modelCursor
        if let cursor, let delay = analyticsCursorDelays[cursor] {
            try await Task.sleep(for: delay)
        }
        if let cursor, failingAnalyticsCursors.contains(cursor) {
            throw DroverError.unavailable("analytics page failed")
        }
        if let cursor, let value = analyticsByCursor[cursor] { return value }
        if let value = analyticsByDays[filters.days] { return value }
        guard !analyticsValues.isEmpty else { throw DroverError.unavailable("no analytics") }
        return analyticsValues.removeFirst()
    }

    func insights(filters: InsightFilters) async throws -> InsightPage {
        insightsRequestCount += 1
        requestedCursors.append(filters.cursor)
        if let host = filters.host, let delay = insightDelaysByHost[host] {
            try? await Task.sleep(for: delay)
        }
        if let cursor = filters.cursor, let delay = insightCursorDelays[cursor] {
            try? await Task.sleep(for: delay)
        }
        if let host = filters.host, failingInsightHosts.contains(host) {
            throw DroverError.unavailable("insights filter failed")
        }
        if let cursor = filters.cursor, let page = insightsByCursor[cursor] { return page }
        if let host = filters.host, let page = insightsByHost[host] { return page }
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
    ) async throws -> ContentAnalysisConsentResult {
        contentConsentRequestCount += 1
        requestedContentConsents.append(.init(
            backend: backend,
            disclosureAccepted: externalDisclosureAccepted
        ))
        if let contentError { throw contentError }
        guard let contentStatusValue else { throw DroverError.unavailable("not configured") }
        return ContentAnalysisConsentResult(
            status: contentStatusValue, outcome: contentMutationOutcome
        )
    }

    func revokeContentAnalysis() async throws -> ContentAnalysisConsentResult {
        revokeRequestCount += 1
        if let contentError { throw contentError }
        guard let contentStatusValue else { throw DroverError.unavailable("not configured") }
        return ContentAnalysisConsentResult(
            status: contentStatusValue, outcome: contentMutationOutcome
        )
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
    ) async throws -> ContentAnalysisConsentResult {
        throw DroverError.unavailable("not configured")
    }

    func revokeContentAnalysis() async throws -> ContentAnalysisConsentResult {
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

private func contentConsentFixture(_ name: String) throws -> ContentAnalysisStatus {
    let url = try #require(Bundle.module.url(
        forResource: name, withExtension: "json", subdirectory: "Fixtures"
    ))
    return try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data(contentsOf: url))
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

private func decodeAnalyticsPage(
    projects: [String], hosts: [String], projectCursor: String?, hostCursor: String?
) throws -> AnalyticsSnapshot {
    let projectRows = projects.map {
        "{\"project_key\":\"\($0)\",\"session_count\":1,\"total_tokens\":10,\"cost_usd\":0,\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"total_latency_ms\":1,\"harnesses\":[\"codex\"],\"hosts\":[\"mac\"]}"
    }.joined(separator: ",")
    let hostRows = hosts.map {
        "{\"key\":\"\($0)\",\"session_count\":1,\"total_tokens\":10,\"cost_usd\":0,\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"total_latency_ms\":1}"
    }.joined(separator: ",")
    let projectNext = projectCursor.map { "\"\($0)\"" } ?? "null"
    let hostNext = hostCursor.map { "\"\($0)\"" } ?? "null"
    return try JSONDecoder().decode(AnalyticsSnapshot.self, from: Data("""
    {"cockpit_api_version":1,"filters":{"days":30,"limit":2},
     "provider_capacity":{"status":"unavailable","data":[]},
     "activity":{"status":"ok","coverage":{"token_percent":100},"data":{
      "totals":{"session_count":1,"total_tokens":10,"cost_usd":0,"cache_read_tokens":0,"cache_write_tokens":0,"total_latency_ms":1},
      "projects":[\(projectRows)],"harnesses":[],"hosts":[\(hostRows)],"models":[],
      "project_metric":"tokens","coverage":{"token_percent":100},
      "pagination":{"projects":{"limit":2,"next_cursor":\(projectNext)},
       "harnesses":{"limit":2,"next_cursor":null},"hosts":{"limit":2,"next_cursor":\(hostNext)},
       "models":{"limit":2,"next_cursor":null}}}}}
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
