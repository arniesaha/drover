import Foundation
import Testing
@testable import DroverKit

/// `.serialized`: two tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
extension MockNetworkTests {
@Suite(.serialized)
struct StoreTests {

@Test @MainActor func refreshBucketsSessions() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }  // Task 2 fixture
    let store = SessionStore(client: client())
    await store.refresh()
    #expect(store.needsYou.map(\.id) == ["harness-1"])
    #expect(store.working.map(\.id) == ["harness-2"])
    #expect(store.isReachable)
}

@Test @MainActor func activeSessionsAreSortedByMostRecentActivityAcrossStates() async throws {
    let newestRunning = SessionSummary(
        id: "new-running",
        hostID: "mac-mini",
        harness: "codex",
        mode: "structured",
        status: "running",
        awaiting: nil,
        cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 300)
    )
    let olderInput = SessionSummary(
        id: "older-input",
        hostID: "mac-mini",
        harness: "claude-code",
        mode: "structured",
        status: "running",
        awaiting: "input",
        cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 200)
    )
    let oldestApproval = SessionSummary(
        id: "oldest-approval",
        hostID: "mac-mini",
        harness: "claude-code",
        mode: "structured",
        status: "running",
        awaiting: "approval",
        cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 100)
    )

    #expect(SessionStore.activeSessions(from: [
        olderInput,
        newestRunning,
        oldestApproval,
    ]).map(\.id) == ["new-running", "older-input", "oldest-approval"])
}

/// The inbox list is one run, newest first, with nothing in between. The screen
/// used to assemble this itself out of two buckets with four analytics sections
/// between them (#80), which read as a sort bug — a session touched minutes ago
/// rendered below one last touched two days ago, with three sections in the gap.
/// Pinning capacity closed the gap; ordering by recency closed the rest.
@Test @MainActor func inboxSessionsAreOneContiguousRunNewestFirst() async throws {
    let waitingTwoDaysAgo = SessionSummary(
        id: "old-input", hostID: "mac-mini", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "input", cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 100)
    )
    let approvalYesterday = SessionSummary(
        id: "approval", hostID: "mac-mini", harness: "claude-code", mode: "structured",
        status: "running", awaiting: "approval", cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 200)
    )
    let runningMinutesAgo = SessionSummary(
        id: "new-running", hostID: "mac-mini", harness: "codex", mode: "structured",
        status: "running", awaiting: nil, cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 900)
    )
    let runningEarlier = SessionSummary(
        id: "old-running", hostID: "nas", harness: "codex", mode: "structured",
        status: "running", awaiting: nil, cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 300)
    )
    let finished = SessionSummary(
        id: "done", hostID: "nas", harness: "codex", mode: "structured",
        status: "completed", awaiting: nil, cwd: nil,
        lastActivity: Date(timeIntervalSince1970: 800)
    )

    let inbox = SessionStore.inboxSessions(from: [
        runningEarlier, finished, waitingTwoDaysAgo, runningMinutesAgo, approvalYesterday,
    ])

    // Newest first, regardless of bucket. The two-day-old question sorts
    // below live work rather than above it. Finished is not here: it has its
    // own collapsed section under the list.
    #expect(inbox.map(\.id) == ["new-running", "old-running", "approval", "old-input"])
}

/// The ordering itself, stated as the invariant rather than as one example:
/// activity never increases as you go down the list, whatever the buckets do.
/// Interleaving is now expected — that is the point of ordering by recency.
@Test @MainActor func inboxSessionsAreOrderedByRecencyAcrossBuckets() async throws {
    let sessions = (0..<12).map { index in
        SessionSummary(
            id: "s\(index)", hostID: "mac-mini", harness: "codex", mode: "structured",
            status: "running",
            awaiting: [nil, "input", "approval"][index % 3],
            cwd: nil,
            lastActivity: Date(timeIntervalSince1970: TimeInterval(index * 60))
        )
    }

    let inbox = SessionStore.inboxSessions(from: sessions)
    let activity = inbox.compactMap(\.activityDate)

    #expect(inbox.count == sessions.count)
    #expect(activity == activity.sorted(by: >),
            "inbox is not newest-first: \(inbox.map(\.id))")
    // And the buckets really are allowed to mix now, so this corpus proves
    // the ordering is doing work rather than accidentally agreeing with a
    // bucket-first result.
    let needsYouFlags = inbox.map { $0.attention == .needsApproval || $0.attention == .needsInput }
    #expect(needsYouFlags != needsYouFlags.sorted(by: { $0 && !$1 }),
            "expected interleaving with recency ordering")
}

@Test @MainActor func refreshFailureKeepsSnapshotSetsError() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    MockURLProtocol.handler = { _ in (401, Data(#"{"error": "authentication required"}"#.utf8)) }
    await store.refresh()
    #expect(store.snapshot != nil)          // cached snapshot survives
    #expect(store.lastError?.localizedCaseInsensitiveContains("token") == true)
    #expect(!store.isReachable)
}

@Test @MainActor func refreshErrorIsHumanReadableNotEnumReflection() async throws {
    // Regression: the banner used to render `"\(error)"`, so users saw the
    // literal Swift enum case — `transport("cancelled")` — as the message.
    MockURLProtocol.transportError = URLError(.cannotConnectToHost)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())
    await store.refresh()
    let message = try #require(store.lastError)
    #expect(!message.contains("transport("))
    #expect(!message.contains("DroverError"))
    #expect(message == "Can't reach the hub")
    #expect(!store.isReachable)
}

@Test @MainActor func cancelledRefreshIsNotTreatedAsUnreachable() async throws {
    // A superseded poll or a dismissed screen cancels its own request. That
    // used to flash an unreachable banner over a perfectly healthy fleet.
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    #expect(store.isReachable)

    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    await store.refresh()
    #expect(store.isReachable)
    #expect(store.lastError == nil)
    #expect(store.snapshot != nil)
}

@Test func droverErrorDescriptionsAreHumanReadable() {
    #expect(DroverError.unauthorized.localizedDescription == "Token rejected — check Settings")
    #expect(DroverError.conflict("turn in flight").localizedDescription == "turn in flight")
    #expect(DroverError.transport("cancelled").localizedDescription == "Request cancelled")
    #expect(DroverError.transport("offline").localizedDescription == "Can't reach the hub")
    #expect(DroverError.decoding("bad json").localizedDescription
            == "Unexpected response from the hub")
    #expect(DroverError.httpStatus(500, "").localizedDescription == "Server error (500)")
}

@Test func cancellationDetectionOnlyMatchesTransportCancels() {
    #expect(DroverError.transport(DroverError.cancellationDetail).isCancellation)
    #expect(!DroverError.transport("offline").isCancellation)
    #expect(!DroverError.conflict(DroverError.cancellationDetail).isCancellation)
}

@Test func needsYouDiffForNotifications() {
    let a = SessionSummary.fixture(id: "a", status: "running", awaiting: "approval")
    let b = SessionSummary.fixture(id: "b", status: "running", awaiting: "input")
    let fresh = SessionStore.newlyNeedsYou(current: [a, b], previousIDs: ["a"])
    #expect(fresh.map(\.id) == ["b"])
}

@Test @MainActor func continueSessionReturnsNewIDOnSuccess() async throws {
    MockURLProtocol.handler = { _ in (200, Data(#"{"session_id": "harness-9"}"#.utf8)) }
    let store = SessionStore(client: client())
    let continued = await store.continueSession("harness-1")
    #expect(continued?.sessionID == "harness-9")
    #expect(continued?.isStructured == false)
    #expect(store.lastError == nil)
}

@Test @MainActor func continueSessionSurfacesStructuredMode() async throws {
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"session_id": "harness-9", "mode": "structured"}"#.utf8))
    }
    let store = SessionStore(client: client())
    let continued = await store.continueSession("harness-1", targetHarness: "agy")
    #expect(continued?.isStructured == true)
}

@Test @MainActor func continueSessionPostsTargetHarness() async throws {
    nonisolated(unsafe) var sentTarget: String?
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentTarget = body["target_harness"] as? String
        return (200, Data(#"{"session_id": "harness-9"}"#.utf8))
    }
    let store = SessionStore(client: client())
    let continued = await store.continueSession("harness-1", targetHarness: "agy")
    #expect(continued?.sessionID == "harness-9")
    #expect(sentTarget == "agy")
}

@Test @MainActor func continueSessionSurfacesServerExplanation() async throws {
    MockURLProtocol.handler = { _ in (409, Data(#"{"error": "host mac-mini is offline"}"#.utf8)) }
    let store = SessionStore(client: client())
    let newID = await store.continueSession("harness-1")
    #expect(newID == nil)
    #expect(store.lastError == "host mac-mini is offline")
}

@Test @MainActor func continueSessionNonServerFailureGetsGenericError() async throws {
    MockURLProtocol.handler = { _ in (200, Data("not json".utf8)) }
    let store = SessionStore(client: client())
    let newID = await store.continueSession("harness-1")
    #expect(newID == nil)
    #expect(store.lastError == "Couldn't start handoff — is the host online?")
}

@Test @MainActor func hasLoadedOnceFlipsOnFirstSuccessfulRefresh() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    #expect(store.hasLoadedOnce == false)
    await store.refresh()
    #expect(store.hasLoadedOnce)
}

@Test @MainActor func hasLoadedOnceSurvivesLaterFailure() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    MockURLProtocol.handler = { _ in (500, Data()) }
    await store.refresh()
    #expect(store.hasLoadedOnce)
    #expect(store.isReachable == false)
    #expect(store.snapshot != nil)   // last-known state kept, list never blanks
}

@Test @MainActor func fleetSnapshotProducesHostGroups() async throws {
    MockURLProtocol.handler = { _ in (200, fleetSnapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    #expect(store.hostGroups.map(\.id) == ["mac-mini", "nas", "ghost-host", "work-laptop"])
    #expect(store.hostGroups[0].sessions.map(\.id) == ["mac-input", "mac-running"])
}

@Test @MainActor func repeatedStartPollingStillClearsTheConnectingGate() async throws {
    // The reported bug (#85): the app sat on "Connecting…" while the server
    // answered in 70ms. startPolling() tore down the in-flight refresh, whose
    // cancellation returns early *without* setting hasLoadedOnce — and it is
    // called from both `.task` and the scenePhase change, so a foreground
    // event during the first load could leave the gate shut. A slow server
    // (see #91) widened that window from milliseconds to tens of seconds.
    MockURLProtocol.handler = { _ in
        Thread.sleep(forTimeInterval: 0.15)  // a server that is not instant
        return (200, snapshotJSON)
    }
    defer { MockURLProtocol.handler = nil }

    let store = SessionStore(client: client())
    // Churn faster than a request can complete, as scene-phase changes do.
    for _ in 0..<6 {
        store.startPolling(every: 5)
        try? await Task.sleep(for: .milliseconds(40))
    }

    // Give the surviving loop room to finish one request.
    let deadline = Date().addingTimeInterval(3)
    while !store.hasLoadedOnce, Date() < deadline {
        try? await Task.sleep(for: .milliseconds(50))
    }
    store.stopPolling()

    #expect(store.hasLoadedOnce,
            "restarting the poll loop must not leave the app stuck on Connecting")
}

/// A stuck "Connecting…" has to say why (#85).
///
/// A cancelled refresh returns early and records nothing, so an app wedged
/// before its first successful load looks identical to one that cannot reach
/// the hub at all — which is exactly why the recurring report could not be
/// diagnosed from the phone.
@Test @MainActor func connectingDetailNamesTheCancellationsNobodyCanSee() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())

    await store.refresh()
    await store.refresh()

    #expect(!store.hasLoadedOnce)
    #expect(store.refreshAttempts == 2)
    let detail = try #require(store.connectingDetail)
    #expect(detail.contains("2 attempts"))
    #expect(detail.localizedCaseInsensitiveContains("cancel"),
            "cancellations must be nameable, not silent: \(detail)")
}

@Test @MainActor func connectingDetailReportsAnUnreachableHubDifferently() async throws {
    MockURLProtocol.transportError = URLError(.cannotConnectToHost)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())

    await store.refresh()
    await store.refresh()

    let detail = try #require(store.connectingDetail)
    #expect(detail.contains("Can't reach the hub"))
}

@Test @MainActor func connectingDetailStaysQuietOnTheFirstAttemptAndAfterSuccess() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    let store = SessionStore(client: client())

    await store.refresh()
    // One blip is normal; the screen should not start explaining itself.
    #expect(store.connectingDetail == nil)

    MockURLProtocol.transportError = nil
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    defer { MockURLProtocol.handler = nil }
    await store.refresh()

    #expect(store.hasLoadedOnce)
    #expect(store.connectingDetail == nil)
    #expect(store.lastRefreshOutcome == nil)
}

}

}  // extension MockNetworkTests
