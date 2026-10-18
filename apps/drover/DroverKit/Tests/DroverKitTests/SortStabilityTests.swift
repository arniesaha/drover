import Foundation
import Testing
@testable import DroverKit

struct SortStabilityTests {
    @Test func sameInputOrderUnderTimestampJitter() {
        let baseTime = Date(timeIntervalSince1970: 10_000)
        let sessionA = SessionSummary.fixture(
            id: "session-a",
            status: "running",
            awaiting: nil,
            hostID: "host-1"
        )
        var sA = sessionA
        sA.lastActivity = baseTime

        let sessionB = SessionSummary.fixture(
            id: "session-b",
            status: "running",
            awaiting: nil,
            hostID: "host-1"
        )
        var sB = sessionB
        sB.lastActivity = baseTime.addingTimeInterval(-5)

        // Initial sort: A (10,000) precedes B (9,995)
        let initial = SessionOrdering.sort([sA, sB])
        #expect(initial.map(\.id) == ["session-a", "session-b"])

        // Second pass: B received a heartbeat and its timestamp jittered by +8s (to 10,003),
        // while A stayed at 10,000. Under raw recency B would leapfrog A.
        // Under hysteresis (30s window), the order [session-a, session-b] is preserved!
        var sBJittered = sB
        sBJittered.lastActivity = baseTime.addingTimeInterval(3) // 8s newer than its previous 9,995

        let afterJitter = SessionOrdering.sort([sA, sBJittered], existing: initial)
        #expect(
            afterJitter.map(\.id) == ["session-a", "session-b"],
            "Jitter within hysteresis window must not flip session order"
        )
    }

    @Test func orderUpdatesOnMaterialActivityExceedingHysteresis() {
        let baseTime = Date(timeIntervalSince1970: 10_000)
        var sA = SessionSummary.fixture(id: "session-a", status: "running", awaiting: nil)
        sA.lastActivity = baseTime

        var sB = SessionSummary.fixture(id: "session-b", status: "running", awaiting: nil)
        sB.lastActivity = baseTime.addingTimeInterval(-10)

        let initial = SessionOrdering.sort([sA, sB])
        #expect(initial.map(\.id) == ["session-a", "session-b"])

        // B has real activity: completed a turn 90s later (well beyond the 30s hysteresis threshold)
        var sBActive = sB
        sBActive.lastActivity = baseTime.addingTimeInterval(90)

        let afterActivity = SessionOrdering.sort([sA, sBActive], existing: initial)
        #expect(
            afterActivity.map(\.id) == ["session-b", "session-a"],
            "Activity exceeding hysteresis threshold should promote the active session"
        )
    }

    @Test func needsYouAlwaysPrecedesRunningRegardlessOfRecency() {
        let now = Date(timeIntervalSince1970: 50_000)
        let oldWaiting = Date(timeIntervalSince1970: 1_000) // 13 hours ago

        var runningFresh = SessionSummary.fixture(id: "running-fresh", status: "running", awaiting: nil)
        runningFresh.lastActivity = now

        var waitingOld = SessionSummary.fixture(id: "waiting-old", status: "running", awaiting: "approval")
        waitingOld.lastActivity = oldWaiting

        let sorted = SessionOrdering.sort([runningFresh, waitingOld])
        #expect(
            sorted.map(\.id) == ["waiting-old", "running-fresh"],
            "Needs-you must always precede running sessions even if running is hours fresher"
        )
    }

    @Test func stateChangeImmediatelyResortsWithoutHysteresisDelay() {
        let baseTime = Date(timeIntervalSince1970: 10_000)
        var sA = SessionSummary.fixture(id: "session-a", status: "running", awaiting: nil)
        sA.lastActivity = baseTime

        var sB = SessionSummary.fixture(id: "session-b", status: "running", awaiting: nil)
        sB.lastActivity = baseTime.addingTimeInterval(-2)

        let initial = SessionOrdering.sort([sA, sB])
        #expect(initial.map(\.id) == ["session-a", "session-b"])

        // Session B asks a question (transitions to needsInput).
        // It must immediately jump above session A despite timestamp differences.
        var sBNeedingInput = sB
        sBNeedingInput.awaiting = "input"

        let afterStateChange = SessionOrdering.sort([sA, sBNeedingInput], existing: initial)
        #expect(
            afterStateChange.map(\.id) == ["session-b", "session-a"],
            "State transition to needs-you must instantly re-sort above running"
        )
    }

    @Test func approvalTakesPriorityOverInputWithinNeedsYou() {
        let baseTime = Date(timeIntervalSince1970: 10_000)
        var inputSession = SessionSummary.fixture(id: "input-session", status: "running", awaiting: "input")
        inputSession.lastActivity = baseTime

        var approvalSession = SessionSummary.fixture(id: "approval-session", status: "running", awaiting: "approval")
        approvalSession.lastActivity = baseTime.addingTimeInterval(-100)

        let sorted = SessionOrdering.sort([inputSession, approvalSession])
        #expect(
            sorted.map(\.id) == ["approval-session", "input-session"],
            "Approval requests take priority over general input questions"
        )
    }

    @Test func deterministicTieBreakingByID() {
        let s1 = SessionSummary.fixture(id: "alpha", status: "running", awaiting: nil)
        let s2 = SessionSummary.fixture(id: "beta", status: "running", awaiting: nil)
        let s3 = SessionSummary.fixture(id: "gamma", status: "running", awaiting: nil)

        let sorted = SessionOrdering.sort([s3, s1, s2])
        #expect(sorted.map(\.id) == ["alpha", "beta", "gamma"])
    }
}
