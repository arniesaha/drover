import Foundation
import Testing
@testable import NexusKit

/// `.serialized`: two tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
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
    #expect(store.lastError?.contains("token") == true)
    #expect(!store.isReachable)
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

}
