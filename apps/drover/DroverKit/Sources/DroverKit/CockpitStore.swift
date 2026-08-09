import Foundation
import Observation

/// The subset of the authenticated client used by cockpit state. Keeping this
/// boundary actor-friendly makes refresh, pagination, and mutation behavior
/// deterministic in tests without weakening the real client's isolation.
public protocol CockpitClient: Sendable {
    func cockpitOverview(days: Int) async throws -> CockpitOverview
    func analytics(filters: AnalyticsFilters) async throws -> AnalyticsSnapshot
    func insights(filters: InsightFilters) async throws -> InsightPage
    func acknowledgeInsight(findingID: String) async throws -> InsightFinding
    func dismissInsight(findingID: String, reason: String) async throws -> InsightFinding
    func checkInsight(findingID: String) async throws -> InsightCheckResponse
    func contentAnalysisStatus() async throws -> ContentAnalysisStatus
    func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool
    ) async throws -> ContentAnalysisStatus
    func revokeContentAnalysis() async throws -> ContentAnalysisStatus
    func purgeContentExcerpts() async throws -> PurgeContentExcerptsResponse
}

extension DroverClient: CockpitClient {}

/// Observable cockpit state with independent last-known-good sections.
@MainActor
@Observable
public final class CockpitStore {
    private let client: any CockpitClient
    private var cockpitAPIVersion: Int?
    private var cockpitSections: Set<String> = []
    private var insightFilters = InsightFilters()
    private var stateOverrides: [String: InsightState] = [:]
    private var refreshGeneration = 0
    private var lifecycleTail: Task<Void, Never>?
    private nonisolated(unsafe) var pollingTask: Task<Void, Never>?

    public private(set) var overview: CockpitOverview?
    public private(set) var providerAccounts: [ProviderAccount] = []
    public private(set) var activity: ActivitySummary?
    public private(set) var popularProjects: [PopularProject] = []
    public private(set) var insightCounts: InsightCounts?
    public private(set) var providerError: String?
    public private(set) var activityError: String?

    public private(set) var analytics: AnalyticsSnapshot?
    public private(set) var analyticsError: String?
    public private(set) var insights: [InsightSummary] = []
    public private(set) var nextInsightsCursor: String?
    public private(set) var insightsError: String?
    public private(set) var lifecycleError: String?
    public private(set) var lastCheckJobID: String?
    public private(set) var isPolling = false

    public private(set) var contentAnalysisStatus: ContentAnalysisStatus?
    public private(set) var contentStatusError: String?
    public private(set) var contentConsentError: String?
    public private(set) var contentRevocationError: String?
    public private(set) var contentPurgeError: String?
    public private(set) var purgedExcerptCount: Int?
    public private(set) var isUpdatingContentConsent = false
    public private(set) var isRevokingContentAnalysis = false
    public private(set) var isPurgingContentExcerpts = false

    public static let cloudDisclosureRequiredMessage =
        "Review and accept the external analysis disclosure."
    public static let cloudDisclosureMessage =
        "System prompts, prompt excerpts, instructions, hook configuration, and skill content "
        + "from explicitly allowed targets may be sent to the selected cloud model provider. "
        + "Drover redacts detected secrets before sending, but this content leaves this device."
    public static let revokeConfirmationMessage =
        "Future model analysis will stop. Existing derived findings remain available."
    public static let purgeConfirmationMessage =
        "All retained redacted evidence excerpts will be deleted. This does not disable "
        + "content analysis or delete finding lifecycle history."

    public init(client: any CockpitClient) {
        self.client = client
    }

    deinit {
        pollingTask?.cancel()
    }

    public var isCockpitAvailable: Bool {
        (cockpitAPIVersion ?? 0) >= 1
    }

    public var isInsightsAvailable: Bool {
        isCockpitAvailable && cockpitSections.contains("insights")
    }

    public func updateCapability(from snapshot: HarnessSnapshot?) {
        cockpitAPIVersion = snapshot?.cockpitAPIVersion
        cockpitSections = Set(snapshot?.cockpitSections ?? [])
        if !isCockpitAvailable {
            stopForegroundPolling()
            providerError = nil
            activityError = nil
            analyticsError = nil
            insightsError = nil
            lifecycleError = nil
        }
    }

    public func refresh(for snapshot: HarnessSnapshot, days: Int = 7) async {
        updateCapability(from: snapshot)
        await refresh(days: days)
    }

    public func refresh(days: Int = 7) async {
        guard isCockpitAvailable else { return }
        refreshGeneration &+= 1
        let generation = refreshGeneration
        do {
            let fresh = try await client.cockpitOverview(days: days)
            guard generation == refreshGeneration, isCockpitAvailable else { return }
            overview = fresh

            switch fresh.providerCapacity.status {
            case .ok:
                if let data = fresh.providerCapacity.data { providerAccounts = data }
                providerError = nil
            case .stale:
                if let data = fresh.providerCapacity.data, !data.isEmpty {
                    providerAccounts = data
                }
                providerError = "Provider capacity is stale."
            case .unavailable:
                if let data = fresh.providerCapacity.data, !data.isEmpty {
                    providerAccounts = data
                }
                providerError = "Provider usage is unavailable."
            case .error, .unknown:
                providerError = "Provider capacity could not be refreshed."
            }

            switch fresh.activity.status {
            case .ok, .stale:
                if let data = fresh.activity.data { activity = data }
                activityError = fresh.activity.status == .stale
                    ? "Observed activity is stale."
                    : nil
            case .unavailable:
                activityError = "Observed activity is unavailable."
            case .error, .unknown:
                activityError = "Observed activity could not be refreshed."
            }

            if fresh.activity.status != .error && fresh.activity.status != .unknown {
                popularProjects = fresh.popularProjects
            }
            insightCounts = fresh.insightCounts
        } catch {
            guard generation == refreshGeneration, isCockpitAvailable else { return }
            guard !Self.isCancellation(error) else { return }
            let message = Self.errorMessage(error)
            providerError = message
            activityError = message
        }
    }

    public func startForegroundPolling(
        for snapshot: HarnessSnapshot,
        every seconds: Double = 30,
        days: Int = 7
    ) {
        updateCapability(from: snapshot)
        guard isCockpitAvailable else { return }
        stopForegroundPolling()
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    guard let self else { return }
                    await self.refresh(days: days)
                }
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
        isPolling = true
    }

    public func stopForegroundPolling() {
        refreshGeneration &+= 1
        pollingTask?.cancel()
        pollingTask = nil
        isPolling = false
    }

    public func loadContentAnalysisStatus() async {
        do {
            contentAnalysisStatus = try await client.contentAnalysisStatus()
            contentStatusError = nil
        } catch {
            guard !Self.isCancellation(error) else { return }
            contentStatusError = Self.errorMessage(error)
        }
    }

    @discardableResult
    public func enableContentAnalysis(
        backend: ContentAnalysisBackend,
        disclosureAccepted: Bool
    ) async -> Bool {
        guard backend != .cloud || disclosureAccepted else {
            contentConsentError = Self.cloudDisclosureRequiredMessage
            return false
        }
        isUpdatingContentConsent = true
        contentConsentError = nil
        defer { isUpdatingContentConsent = false }
        do {
            contentAnalysisStatus = try await client.setContentAnalysisConsent(
                backend: backend,
                externalDisclosureAccepted: backend == .cloud && disclosureAccepted
            )
            return true
        } catch {
            guard !Self.isCancellation(error) else { return false }
            contentConsentError = Self.errorMessage(error)
            return false
        }
    }

    @discardableResult
    public func revokeContentAnalysis() async -> Bool {
        isRevokingContentAnalysis = true
        contentRevocationError = nil
        defer { isRevokingContentAnalysis = false }
        do {
            contentAnalysisStatus = try await client.revokeContentAnalysis()
            return true
        } catch {
            guard !Self.isCancellation(error) else { return false }
            contentRevocationError = Self.errorMessage(error)
            return false
        }
    }

    @discardableResult
    public func purgeContentExcerpts() async -> Bool {
        isPurgingContentExcerpts = true
        contentPurgeError = nil
        purgedExcerptCount = nil
        defer { isPurgingContentExcerpts = false }
        do {
            let response = try await client.purgeContentExcerpts()
            purgedExcerptCount = response.purgedExcerptCount
            return true
        } catch {
            guard !Self.isCancellation(error) else { return false }
            contentPurgeError = Self.errorMessage(error)
            return false
        }
    }

    public func loadAnalytics(filters: AnalyticsFilters = AnalyticsFilters()) async {
        guard isCockpitAvailable else { return }
        do {
            analytics = try await client.analytics(filters: filters)
            analyticsError = nil
        } catch {
            guard !Self.isCancellation(error) else { return }
            analyticsError = Self.errorMessage(error)
        }
    }

    public func loadInsights(filters: InsightFilters = InsightFilters()) async {
        guard isCockpitAvailable else { return }
        var firstPageFilters = filters
        firstPageFilters.cursor = nil
        do {
            let page = try await client.insights(filters: firstPageFilters)
            insightFilters = firstPageFilters
            insights = page.findings
            nextInsightsCursor = page.nextCursor
            insightsError = nil
            for finding in page.findings { stateOverrides[finding.findingID] = finding.state }
        } catch {
            guard !Self.isCancellation(error) else { return }
            insightsError = Self.errorMessage(error)
        }
    }

    public func loadMoreInsights() async {
        guard isCockpitAvailable, let cursor = nextInsightsCursor else { return }
        var filters = insightFilters
        filters.cursor = cursor
        do {
            let page = try await client.insights(filters: filters)
            let existing = Set(insights.map(\.findingID))
            insights.append(contentsOf: page.findings.filter { !existing.contains($0.findingID) })
            nextInsightsCursor = page.nextCursor
            insightsError = nil
            for finding in page.findings { stateOverrides[finding.findingID] = finding.state }
        } catch {
            guard !Self.isCancellation(error) else { return }
            insightsError = Self.errorMessage(error)
        }
    }

    public func state(forFindingID findingID: String) -> InsightState? {
        stateOverrides[findingID]
            ?? insights.first(where: { $0.findingID == findingID })?.state
    }

    @discardableResult
    public func acknowledgeInsight(findingID: String) async -> Bool {
        await enqueueLifecycleAction { [weak self] in
            guard let self else { return false }
            let prior = self.stateOverrides[findingID]
            self.stateOverrides[findingID] = .acknowledged
            self.lifecycleError = nil
            do {
                let finding = try await self.client.acknowledgeInsight(findingID: findingID)
                self.stateOverrides[findingID] = finding.state
                return true
            } catch {
                self.stateOverrides[findingID] = prior
                guard !Self.isCancellation(error) else { return false }
                self.lifecycleError = Self.errorMessage(error)
                return false
            }
        }
    }

    @discardableResult
    public func dismissInsight(findingID: String, reason: String) async -> Bool {
        let trimmed = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            lifecycleError = "A dismissal reason is required."
            return false
        }
        return await enqueueLifecycleAction { [weak self] in
            guard let self else { return false }
            self.lifecycleError = nil
            do {
                let finding = try await self.client.dismissInsight(
                    findingID: findingID,
                    reason: trimmed
                )
                self.stateOverrides[findingID] = finding.state
                return true
            } catch {
                guard !Self.isCancellation(error) else { return false }
                self.lifecycleError = Self.errorMessage(error)
                return false
            }
        }
    }

    @discardableResult
    public func checkInsight(findingID: String) async -> Bool {
        await enqueueLifecycleAction { [weak self] in
            guard let self else { return false }
            self.lifecycleError = nil
            do {
                let response = try await self.client.checkInsight(findingID: findingID)
                self.lastCheckJobID = response.jobID
                return true
            } catch {
                guard !Self.isCancellation(error) else { return false }
                self.lifecycleError = Self.errorMessage(error)
                return false
            }
        }
    }

    private func enqueueLifecycleAction(
        _ action: @escaping @MainActor @Sendable () async -> Bool
    ) async -> Bool {
        guard isCockpitAvailable else { return false }
        let preceding = lifecycleTail
        let task = Task { @MainActor in
            await preceding?.value
            guard !Task.isCancelled else { return false }
            return await action()
        }
        lifecycleTail = Task { @MainActor in _ = await task.value }
        return await task.value
    }

    private nonisolated static func isCancellation(_ error: Error) -> Bool {
        if let droverError = error as? DroverError { return droverError.isCancellation }
        return (error as? URLError)?.code == .cancelled || error is CancellationError
    }

    private nonisolated static func errorMessage(_ error: Error) -> String {
        if let droverError = error as? DroverError {
            return droverError.localizedDescription
        }
        return (error as NSError).localizedDescription
    }
}
