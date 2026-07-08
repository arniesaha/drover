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

}
