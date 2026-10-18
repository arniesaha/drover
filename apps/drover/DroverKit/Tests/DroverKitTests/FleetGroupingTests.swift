import Foundation
import Testing
@testable import DroverKit

// Pure derivation tests — no MockURLProtocol, so deliberately OUTSIDE
// the MockNetworkTests serialized suite (see ClientTests' doc comment).
struct FleetGroupingTests {
    @Test func retiredSessionDoesNotSynthesizeFleetHost() throws {
        let data = Data(#"{"session_id":"old","host_id":"mac-mini","host_display_name":"Original Mac","host_retired_at":"2026-10-01T00:00:00Z","harness":"claude-code","status":"running"}"#.utf8)
        let session = try JSONDecoder().decode(SessionSummary.self, from: data)
        #expect(session.hostDisplayName == "Original Mac")
        #expect(session.hostRetiredAt != nil)
        #expect(SessionStore.hostGroups(hosts: [], sessions: [session]).isEmpty)
    }

    @Test func groupsActiveSessionsUnderTheirHost() {
        let hosts = [HostSummary.fixture(id: "mac-mini"), HostSummary.fixture(id: "nas")]
        let sessions = [
            SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "s2", status: "running", awaiting: nil, hostID: "nas"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups.map(\.id) == ["mac-mini", "nas"])
        #expect(groups[0].sessions.map(\.id) == ["s1"])
        #expect(groups[1].sessions.map(\.id) == ["s2"])
    }

    @Test func waitingSessionsSortToTopOfTheirGroup() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let sessions = [
            SessionSummary.fixture(id: "working", status: "running", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "input", status: "running", awaiting: "input", hostID: "mac-mini"),
            SessionSummary.fixture(id: "approval", status: "running", awaiting: "approval", hostID: "mac-mini"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups[0].sessions.map(\.id) == ["approval", "input", "working"])
    }

    @Test func offlineAndStaleHostsSortAfterOnline() {
        let hosts = [
            HostSummary.fixture(id: "laptop", status: "offline"),
            HostSummary.fixture(id: "nas", status: "stale"),
            HostSummary.fixture(id: "mac", status: "online"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: [])
        #expect(groups.map(\.id) == ["mac", "nas", "laptop"])
    }

    @Test func hostWithNoActiveSessionsStillAppears() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: [])
        #expect(groups.map(\.id) == ["mac-mini"])
        #expect(groups[0].sessions.isEmpty)
    }

    @Test func finishedSessionsAreExcludedFromGroups() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let sessions = [
            SessionSummary.fixture(id: "done", status: "completed", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "err", status: "errored", awaiting: nil, hostID: "mac-mini"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups[0].sessions.isEmpty)
    }

    @Test func sessionOnUnknownHostGetsSynthesizedOfflineGroup() {
        let sessions = [
            SessionSummary.fixture(id: "orphan", status: "running", awaiting: nil, hostID: "ghost-host"),
        ]
        let groups = SessionStore.hostGroups(hosts: [], sessions: sessions)
        #expect(groups.map(\.id) == ["ghost-host"])
        #expect(groups[0].host.presence == .offline)
        #expect(groups[0].sessions.map(\.id) == ["orphan"])
    }

    @Test func hostChipCountIsExplicitlyActiveSessionsOnly() {
        let hosts = [HostSummary.fixture(id: "studio")]
        let sessions = [
            SessionSummary.fixture(id: "r1", status: "running", awaiting: nil, hostID: "studio"),
            SessionSummary.fixture(id: "r2", status: "running", awaiting: nil, hostID: "studio"),
            SessionSummary.fixture(id: "r3", status: "running", awaiting: nil, hostID: "studio"),
            SessionSummary.fixture(id: "w1", status: "running", awaiting: "approval", hostID: "studio"),
            SessionSummary.fixture(id: "w2", status: "running", awaiting: "input", hostID: "studio"),
            // 24 finished sessions (which must not be counted in the host chip active count)
            SessionSummary.fixture(id: "d1", status: "completed", awaiting: nil, hostID: "studio"),
            SessionSummary.fixture(id: "d2", status: "completed", awaiting: nil, hostID: "studio"),
            SessionSummary.fixture(id: "d3", status: "completed", awaiting: nil, hostID: "studio"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        // 3 running + 2 needs-you = 5 active; finished sessions (3) are excluded
        #expect(groups[0].sessions.count == 5)
    }
}
