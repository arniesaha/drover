import Foundation
import Testing
@testable import DroverKit

private final class PollingRequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func increment() {
        lock.lock()
        count += 1
        lock.unlock()
    }

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return count
    }
}

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

/// The snapshot's own age, which is the thing the cards were missing (#81).
///
/// Keeping the cached snapshot through a failed refresh is deliberate and
/// stays; what could not be asked before is *when* that snapshot was true.
/// A failed refresh must not move the timestamp — a clock that ticks on
/// failure is exactly the deception the cards were committing.
@Test @MainActor func aFailedRefreshDoesNotMoveTheSnapshotsTimestamp() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    let landed = try #require(store.lastSuccessfulRefresh)
    #expect(store.freshness(now: landed.addingTimeInterval(1)).isStale == false)

    MockURLProtocol.handler = { _ in (500, Data()) }
    await store.refresh()

    #expect(store.snapshot != nil, "the cached snapshot must survive — that behaviour stays")
    #expect(store.lastSuccessfulRefresh == landed, "a failure must not restamp the snapshot")
    let freshness = store.freshness(now: landed.addingTimeInterval(247))
    #expect(freshness.isStale)
    #expect(freshness.staleNote?.contains("4m") == true)
}

/// A cancelled refresh is not a failure and not a success: it leaves the
/// timestamp where it was, so the snapshot simply keeps ageing.
@Test @MainActor func aCancelledRefreshLeavesTheTimestampAlone() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    let landed = try #require(store.lastSuccessfulRefresh)

    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    await store.refresh()

    #expect(store.lastSuccessfulRefresh == landed)
    #expect(store.isReachable, "a cancellation is still not an unreachable hub")
    // Reachable, but nothing has landed in ten minutes — the cards say so.
    #expect(store.freshness(now: landed.addingTimeInterval(600)).isStale)
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

/// The stuck screen itself, reproduced without a device (#85).
///
/// Captured live on a phone 2026-08-11 17:09: "Connecting… / 3 attempts ·
/// request cancelled", sitting there while the hub was up. Every cancellation
/// returns early without touching `lastError`, so a first load that keeps
/// being torn down renders an eternal spinner — strictly worse than an honest
/// failure, which at least offers Retry. A *run* of them has to become
/// actionable, or the only cure is luck.
@Test @MainActor func aFirstLoadLostToRepeatedCancellationBecomesRetriable() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())

    await store.refresh()
    await store.refresh()
    #expect(store.lastError == nil, "a blip or two is still just a slow start")

    await store.refresh()

    #expect(!store.hasLoadedOnce)
    #expect(store.lastError != nil, "an eternal spinner is not an outcome")
    #expect(!store.isReachable)
    #expect(store.connectingDetail?.contains("cancel") == true,
            "the instrumentation that made this diagnosable stays")
}

/// The other half of the same decision: giving up is only ever about the
/// *first* load. A superseded poll over a fleet that has already loaded must
/// still pass in silence, however many of them there are — flashing an
/// unreachable banner over a healthy fleet is the bug that put the early
/// return in `refresh()` in the first place.
@Test @MainActor func cancellationsNeverFlashUnreachableOverALoadedFleet() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let store = SessionStore(client: client())
    await store.refresh()
    MockURLProtocol.handler = nil

    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    for _ in 0..<5 { await store.refresh() }

    #expect(store.isReachable)
    #expect(store.lastError == nil)
    #expect(store.snapshot != nil)
}

@Test @MainActor func aLandedSnapshotClearsTheGivenUpFirstLoad() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    let store = SessionStore(client: client())
    for _ in 0..<3 { await store.refresh() }
    #expect(store.lastError != nil)

    MockURLProtocol.transportError = nil
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    defer { MockURLProtocol.handler = nil }
    await store.refresh()

    #expect(store.hasLoadedOnce)
    #expect(store.lastError == nil)
    #expect(store.isReachable)
}

/// A cancelled first load must not cost a whole poll interval (#85).
///
/// The loop slept the full five seconds after a cancellation exactly as it
/// does after a success, so a burst of launch-time churn could starve the
/// first load for tens of seconds and the screen had nothing to show for it.
/// With a minute-long interval the difference is unmistakable: retry promptly
/// and this resolves in about a second; wait out the interval and the test
/// times out on one attempt.
@Test @MainActor func aCancelledFirstLoadRetriesWithoutWaitingOutTheInterval() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())

    store.startPolling(every: 60)
    let deadline = Date().addingTimeInterval(3)
    while store.lastError == nil, Date() < deadline {
        try? await Task.sleep(for: .milliseconds(50))
    }
    store.stopPolling()

    #expect(store.refreshAttempts >= 3)
    #expect(store.lastError != nil,
            "a first load stuck behind cancellations must become retriable in seconds")
}

/// And the prompt retry stops once it has become retriable. Racing ahead is
/// worth it only while the screen is a spinner the user cannot act on; past
/// that it is four requests a second at a hub whose slowness (#95) is what
/// widened the cancellation window in the first place.
@Test @MainActor func theFastRetryStopsOnceTheScreenIsActionable() async throws {
    MockURLProtocol.transportError = URLError(.cancelled)
    defer { MockURLProtocol.transportError = nil }
    let store = SessionStore(client: client())

    store.startPolling(every: 60)
    let deadline = Date().addingTimeInterval(3)
    while store.lastError == nil, Date() < deadline {
        try? await Task.sleep(for: .milliseconds(50))
    }
    let attemptsWhenItGaveUp = store.refreshAttempts
    try? await Task.sleep(for: .seconds(1))
    store.stopPolling()

    #expect(store.refreshAttempts == attemptsWhenItGaveUp,
            "kept hammering after giving up: \(store.refreshAttempts) attempts")
}

/// Re-entry must leave a running loop alone (#85).
///
/// `startPolling()` used to call `stopPolling()` first, which cancels the
/// in-flight request — and `SessionsView` calls it from both `.task` and the
/// scene-phase change, so an ordinary launch cancelled its own first load.
/// A `/harness` answering in 10ms hides that; one answering in seconds (#95)
/// does not.
@Test @MainActor func startPollingDoesNotTearDownARunningLoop() async throws {
    let requests = PollingRequestCounter()
    MockURLProtocol.handler = { _ in
        requests.increment()
        Thread.sleep(forTimeInterval: 0.3)  // a hub under load, not an instant one
        return (200, snapshotJSON)
    }
    defer { MockURLProtocol.handler = nil }

    let store = SessionStore(client: client())
    store.startPolling(every: 5)

    // A full parallel suite can keep the main-actor polling task from starting
    // for longer than the churn window below. Wait for the transport request;
    // the production `refreshAttempts` value resets on success, so it is not a
    // stable count of requests after a slow test process resumes.
    let startDeadline = Date().addingTimeInterval(3)
    while requests.value == 0, Date() < startDeadline {
        try? await Task.sleep(for: .milliseconds(10))
    }
    try #require(requests.value == 1, "initial polling request never started")

    // The churn a launch produces, all of it inside one request's window.
    for _ in 0..<5 {
        try? await Task.sleep(for: .milliseconds(20))
        store.startPolling(every: 5)
    }

    #expect(requests.value == 1, "re-entry started \(requests.value) requests")
    #expect(store.lastRefreshOutcome == nil, "re-entry cancelled the first load")

    let deadline = Date().addingTimeInterval(3)
    while !store.hasLoadedOnce, Date() < deadline {
        try? await Task.sleep(for: .milliseconds(50))
    }
    store.stopPolling()
    #expect(store.hasLoadedOnce)
}

@Test func droverErrorTailscaleDescriptions() {
    #expect(DroverError.connectionFailureReason(URLError(.cannotConnectToHost), isTailscale: true) == "Can't reach the hub over Tailscale")
    #expect(DroverError.transport("offline").localizedDescription(isTailscale: true) == "Can't reach the hub over Tailscale")
    #expect(DroverError.transport(DroverError.cancellationDetail).localizedDescription(isTailscale: true) == "Request cancelled")
    #expect(DroverError.unauthorized.localizedDescription(isTailscale: true) == "Token rejected — check Settings")
}

@Test @MainActor func refreshErrorOnTailscaleReflectsTailscaleContext() async throws {
    let tsConfig = ServerConfig(urlString: "http://100.64.0.1:7080")!
    let tsClient = DroverClient(config: tsConfig, token: "test-token", session: MockURLProtocol.session())
    let store = SessionStore(client: tsClient)

    #expect(store.isTailscaleAddress)
    #expect(store.tailscaleHost == "100.64.0.1")

    MockURLProtocol.transportError = URLError(.cannotConnectToHost)
    defer { MockURLProtocol.transportError = nil }

    await store.refresh()

    let message = try #require(store.lastError)
    #expect(message == "Can't reach the hub over Tailscale")
    #expect(!store.isReachable)
}

@Test @MainActor func connectingDetailReportsTailscaleUnreachableHub() async throws {
    let tsConfig = ServerConfig(urlString: "http://my-mac.ts.net:7080")!
    let tsClient = DroverClient(config: tsConfig, token: "test-token", session: MockURLProtocol.session())
    let store = SessionStore(client: tsClient)

    MockURLProtocol.transportError = URLError(.cannotConnectToHost)
    defer { MockURLProtocol.transportError = nil }

    await store.refresh()
    await store.refresh()

    let detail = try #require(store.connectingDetail)
    #expect(detail.contains("Can't reach the hub over Tailscale"))
}

}

}  // extension MockNetworkTests
