import DroverKit
import SwiftUI

#if DEBUG
/// A credential-free root that exercises `InsightDetailView` through its
/// normal detail request. The fixture transport rejects every non-synthetic
/// request, so this root cannot contact a configured or production service.
@MainActor
struct InsightDetailFixtureRoot: View {
    let client: DroverClient
    @State private var store: CockpitStore

    init(client: DroverClient) {
        self.client = client
        _store = State(initialValue: CockpitStore(client: client))
    }

    var body: some View {
        NavigationStack {
            InsightDetailView(
                client: client,
                store: store,
                summary: fixtureSummary
            )
        }
        .task { store.updateCapability(from: fixtureCapabilitySnapshot) }
    }

    /// Without this the store reports the cockpit unavailable, and every action
    /// this fixture exists to exercise short-circuits before reaching the
    /// transport.
    private var fixtureCapabilitySnapshot: HarnessSnapshot? {
        try? JSONDecoder().decode(
            HarnessSnapshot.self,
            from: FixtureScenarioData.insightCapabilitySnapshotData()
        )
    }

    private var fixtureSummary: InsightSummary {
        // The JSON is static and kept alongside the response fixture. Decoding
        // here ensures the view receives the same wire model as production.
        try! JSONDecoder().decode(InsightSummary.self, from: FixtureScenarioData.insightSummaryData())
    }
}
#endif
