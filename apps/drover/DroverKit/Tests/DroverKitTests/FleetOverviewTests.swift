import Foundation
import Testing
@testable import DroverKit

struct FleetOverviewTests {
    @Test func activeCountsPerHostAndPerProjectAndMatrix() {
        let hosts = [
            HostSummary.fixture(id: "studio", displayName: "Mac Studio", status: "online"),
            HostSummary.fixture(id: "gpu", displayName: "GPU PC", status: "online"),
            HostSummary.fixture(id: "nas", displayName: "NAS", status: "offline"),
        ]

        var s1 = SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "studio")
        s1.repoName = "arniesaha/drover"

        var s2 = SessionSummary.fixture(id: "s2", status: "running", awaiting: "input", hostID: "studio")
        s2.repoName = "arniesaha/drover"

        var s3 = SessionSummary.fixture(id: "s3", status: "running", awaiting: nil, hostID: "studio")
        s3.repoName = "arniesaha/openclaw"

        var s4 = SessionSummary.fixture(id: "s4", status: "running", awaiting: nil, hostID: "gpu")
        s4.repoName = "arniesaha/drover"

        // Finished sessions must NOT count towards active counts
        var s5 = SessionSummary.fixture(id: "s5", status: "completed", awaiting: nil, hostID: "studio")
        s5.repoName = "arniesaha/drover"

        let overview = FleetOverviewPresentation(hosts: hosts, sessions: [s1, s2, s3, s4, s5])

        #expect(overview.totalActiveSessions == 4)

        // Per-host counts
        let studioHost = overview.hosts.first { $0.id == "studio" }
        #expect(studioHost?.activeCount == 3)

        let gpuHost = overview.hosts.first { $0.id == "gpu" }
        #expect(gpuHost?.activeCount == 1)

        let nasHost = overview.hosts.first { $0.id == "nas" }
        #expect(nasHost?.activeCount == 0)

        // Per-project counts
        let droverProject = overview.projects.first { $0.name == "arniesaha/drover" }
        #expect(droverProject?.activeCount == 3) // 2 on studio + 1 on gpu

        let openclawProject = overview.projects.first { $0.name == "arniesaha/openclaw" }
        #expect(openclawProject?.activeCount == 1) // 1 on studio

        // Matrix (project × host) counts
        #expect(overview.count(project: "arniesaha/drover", hostID: "studio") == 2)
        #expect(overview.count(project: "arniesaha/drover", hostID: "gpu") == 1)
        #expect(overview.count(project: "arniesaha/drover", hostID: "nas") == 0)

        #expect(overview.count(project: "arniesaha/openclaw", hostID: "studio") == 1)
        #expect(overview.count(project: "arniesaha/openclaw", hostID: "gpu") == 0)
    }

    @Test func performanceWithOver100Sessions() {
        let hosts = (1...5).map { index in
            HostSummary.fixture(id: "host-\(index)", displayName: "Host \(index)", status: "online")
        }

        let projects = ["drover", "openclaw", "relay", "backend", "ios-client"]

        let sessions = (1...150).map { index in
            var s = SessionSummary.fixture(
                id: "session-\(index)",
                status: index % 6 == 0 ? "completed" : "running",
                awaiting: index % 7 == 0 ? "approval" : nil,
                hostID: "host-\((index % 5) + 1)"
            )
            s.repoName = "arniesaha/\(projects[index % projects.count])"
            return s
        }

        let start = Date()
        let overview = FleetOverviewPresentation(hosts: hosts, sessions: sessions)
        let duration = Date().timeIntervalSince(start)

        #expect(overview.totalActiveSessions == 125) // 150 - (150/6 completed)
        #expect(overview.projects.count == 5)
        #expect(overview.hosts.count == 5)
        #expect(duration < 0.05, "Computing overview for 150 sessions must take < 50ms (took \(duration)s)")
    }
}
