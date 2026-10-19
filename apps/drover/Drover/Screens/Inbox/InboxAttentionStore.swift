import DroverKit
import Observation

/// A small, independent read model for the dock badge and Needs you segment.
/// Keeping it apart from `CockpitStore.insights` lets the Insights segment own
/// its filters without making the badge depend on whichever filters were last
/// selected there.
@MainActor
@Observable
final class InboxAttentionStore {
    private let client: DroverClient
    private var generation = 0

    private(set) var insights: [InsightSummary] = []
    private(set) var error: String?
    private(set) var isLoading = false

    init(client: DroverClient) { self.client = client }

    func refresh(isAvailable: Bool) async {
        guard isAvailable else {
            insights = []
            error = nil
            return
        }
        generation &+= 1
        let requestGeneration = generation
        isLoading = true
        error = nil
        defer {
            if requestGeneration == generation { isLoading = false }
        }
        do {
            let page = try await client.insights(filters: InsightFilters(state: .open, limit: 100))
            guard requestGeneration == generation else { return }
            insights = InboxAttentionPresentation.openPriorityInsights(page.findings)
        } catch is CancellationError {
            return
        } catch {
            guard requestGeneration == generation else { return }
            self.error = "Couldn't load priority insights. Pull to refresh to try again."
        }
    }
}
