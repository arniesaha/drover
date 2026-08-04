import Foundation
import Testing
@testable import NexusKit

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
    #expect(!message.contains("NexusError"))
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

@Test func nexusErrorDescriptionsAreHumanReadable() {
    #expect(NexusError.unauthorized.localizedDescription == "Token rejected — check Settings")
    #expect(NexusError.conflict("turn in flight").localizedDescription == "turn in flight")
    #expect(NexusError.transport("cancelled").localizedDescription == "Request cancelled")
    #expect(NexusError.transport("offline").localizedDescription == "Can't reach the hub")
    #expect(NexusError.decoding("bad json").localizedDescription
            == "Unexpected response from the hub")
    #expect(NexusError.httpStatus(500, "").localizedDescription == "Server error (500)")
}

@Test func cancellationDetectionOnlyMatchesTransportCancels() {
    #expect(NexusError.transport(NexusError.cancellationDetail).isCancellation)
    #expect(!NexusError.transport("offline").isCancellation)
    #expect(!NexusError.conflict(NexusError.cancellationDetail).isCancellation)
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
    let continued = await store.continueSession("harness-1", targetHarness: "gemini")
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
    let continued = await store.continueSession("harness-1", targetHarness: "gemini")
    #expect(continued?.sessionID == "harness-9")
    #expect(sentTarget == "gemini")
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

}

}  // extension MockNetworkTests
