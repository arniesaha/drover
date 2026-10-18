import Foundation
import Testing
@testable import DroverKit

struct FleetFilterTests {
    @Test func filterByHostOnly() {
        let s1 = SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "studio")
        let s2 = SessionSummary.fixture(id: "s2", status: "running", awaiting: nil, hostID: "nas")
        let s3 = SessionSummary.fixture(id: "s3", status: "running", awaiting: nil, hostID: "studio")

        let filter = FleetFilterState(hostID: "studio")
        #expect(filter.isFiltered)

        let result = filter.filter([s1, s2, s3])
        #expect(result.map(\.id) == ["s1", "s3"])
    }

    @Test func filterByProjectOnly() {
        var s1 = SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "studio")
        s1.repoName = "arniesaha/drover"

        var s2 = SessionSummary.fixture(id: "s2", status: "running", awaiting: nil, hostID: "gpu-pc")
        s2.repoName = "arniesaha/openclaw"

        var s3 = SessionSummary.fixture(id: "s3", status: "running", awaiting: nil, hostID: "nas")
        s3.repoName = "arniesaha/drover"

        let filter = FleetFilterState(project: "arniesaha/drover")
        let result = filter.filter([s1, s2, s3])
        #expect(result.map(\.id) == ["s1", "s3"])

        // Also test matching by short project basename (e.g. "drover")
        let shortFilter = FleetFilterState(project: "drover")
        let shortResult = shortFilter.filter([s1, s2, s3])
        #expect(shortResult.map(\.id) == ["s1", "s3"])
    }

    @Test func filterByHostAndProjectConjunction() {
        var s1 = SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "studio")
        s1.repoName = "arniesaha/drover"

        var s2 = SessionSummary.fixture(id: "s2", status: "running", awaiting: nil, hostID: "gpu-pc")
        s2.repoName = "arniesaha/drover"

        var s3 = SessionSummary.fixture(id: "s3", status: "running", awaiting: nil, hostID: "gpu-pc")
        s3.repoName = "arniesaha/openclaw"

        var s4 = SessionSummary.fixture(id: "s4", status: "running", awaiting: nil, hostID: "studio")
        s4.repoName = "arniesaha/openclaw"

        // Filter: "drover on GPU PC" (AND condition)
        let filter = FleetFilterState(hostID: "gpu-pc", project: "drover")
        let result = filter.filter([s1, s2, s3, s4])
        #expect(result.map(\.id) == ["s2"])
    }

    @Test func persistenceRoundTrip() {
        let suiteName = "test.drover.filter.persistence.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        var filter = FleetFilterState(hostID: "mac-studio", project: "arniesaha/drover")
        filter.save(to: defaults)

        let loaded = FleetFilterState.load(from: defaults)
        #expect(loaded.hostID == "mac-studio")
        #expect(loaded.project == "arniesaha/drover")
        #expect(loaded.isFiltered)

        // Clear and verify persistence clears
        var cleared = loaded
        cleared.clear()
        #expect(!cleared.isFiltered)
        cleared.save(to: defaults)

        let reloaded = FleetFilterState.load(from: defaults)
        #expect(reloaded.hostID == nil)
        #expect(reloaded.project == nil)
        #expect(!reloaded.isFiltered)
    }

    @Test func offlineHostFiltering() {
        let onlineHost = HostSummary.fixture(id: "studio", status: "online")
        let offlineHost = HostSummary.fixture(id: "nas", status: "offline")
        let sOffline = SessionSummary.fixture(id: "s-offline", status: "running", awaiting: nil, hostID: "nas")

        let filter = FleetFilterState(hostID: offlineHost.id)
        #expect(filter.matches(session: sOffline))
    }
}
