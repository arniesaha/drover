import Foundation
import Testing
@testable import NexusKit

private func chatWireMessage(seq: Int, text: String) -> String {
    #"{"event_id": "e\#(seq)", "seq": \#(seq), "type": "assistant_output", "role": "assistant", "text": "\#(text)", "payload": {}}"#
}

private struct TimeoutError: Error {}

@MainActor
private func waitUntil(
    timeout: Duration = .seconds(5), _ condition: () -> Bool
) async throws {
    let deadline = ContinuousClock.now + timeout
    while !condition() {
        guard ContinuousClock.now < deadline else { throw TimeoutError() }
        try await Task.sleep(for: .milliseconds(10))
    }
}

/// `.serialized`: several tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
@Suite(.serialized)
struct ChatModelTests {

@Test @MainActor func pendingApprovalTracksAnswerPairs() async throws {
    let model = ChatModel.fixture()   // internal init taking [HarnessMessage] directly
    model.ingest(.message(.fixture(seq: 1, type: .approvalPrompt,
                                   payload: ["request_id": .string("r1")])))
    #expect(model.pendingApproval?.payload["request_id"]?.stringValue == "r1")
    model.ingest(.message(.fixture(seq: 2, type: .approvalResponse,
                                   payload: ["request_id": .string("r1")])))
    #expect(model.pendingApproval == nil)
}

@Test @MainActor func conflictBecomesHintNotError() async throws {
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "turn already in flight"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "next thing"
    await model.sendTurn()
    #expect(model.hint == "turn already in flight")
    #expect(model.composerText == "next thing")   // preserved for retry
}

@Test @MainActor func handOffReturnsNewSessionID() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/continue")
        return (201, Data(#"{"session_id": "harness-continued"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let newID = await model.handOff()
    #expect(newID == "harness-continued")
    #expect(model.hint == nil)
}

@Test @MainActor func handOffWithTargetHarnessPostsTarget() async throws {
    nonisolated(unsafe) var sentTarget: String?
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/continue")
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentTarget = body["target_harness"] as? String
        return (201, Data(#"{"session_id": "harness-continued"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let newID = await model.handOff(targetHarness: "codex")
    #expect(newID == "harness-continued")
    #expect(sentTarget == "codex")
}

@Test @MainActor func loadHandoffTargetsListsHostHarnesses() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let model = ChatModel(client: client(), sessionID: "harness-1")
    #expect(model.handoffHarnesses.isEmpty)
    await model.loadHandoffTargets()
    #expect(model.handoffHarnesses == ["shell", "claude-code", "gemini"])
}

@Test @MainActor func loadHandoffTargetsUnknownSessionLeavesListEmpty() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let model = ChatModel(client: client(), sessionID: "not-in-snapshot")
    await model.loadHandoffTargets()
    #expect(model.handoffHarnesses.isEmpty)
}

@Test @MainActor func handOffFailureBecomesHint() async throws {
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "host offline"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let newID = await model.handOff()
    #expect(newID == nil)
    #expect(model.hint == "host offline")
}

@Test @MainActor func sentTurnClearsComposer() async throws {
    MockURLProtocol.handler = { _ in (202, Data(#"{"turn_id": "t9"}"#.utf8)) }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "do it"
    await model.sendTurn()
    #expect(model.composerText.isEmpty)
    #expect(model.hint == nil)
}

@Test @MainActor func pendingApprovalIgnoresUnrelatedRequestIDs() async throws {
    let model = ChatModel.fixture()
    model.ingest(.message(.fixture(seq: 1, type: .approvalPrompt,
                                   payload: ["request_id": .string("r1")])))
    // A response for a *different* request_id must not clear r1's prompt.
    model.ingest(.message(.fixture(seq: 2, type: .approvalResponse,
                                   payload: ["request_id": .string("other")])))
    #expect(model.pendingApproval?.payload["request_id"]?.stringValue == "r1")
}

@Test @MainActor func pendingApprovalPicksNewestUnanswered() async throws {
    let model = ChatModel.fixture()
    model.ingest(.message(.fixture(seq: 1, type: .approvalPrompt,
                                   payload: ["request_id": .string("r1")])))
    model.ingest(.message(.fixture(seq: 2, type: .approvalPrompt,
                                   payload: ["request_id": .string("r2")])))
    #expect(model.pendingApproval?.payload["request_id"]?.stringValue == "r2")
}

@Test @MainActor func connectionEventTogglesIsConnected() async throws {
    let model = ChatModel.fixture()
    #expect(model.isConnected == false)
    model.ingest(.connection(true))
    #expect(model.isConnected == true)
    model.ingest(.connection(false))
    #expect(model.isConnected == false)
}

@Test @MainActor func ingestAppendsInOrder() async throws {
    let model = ChatModel.fixture()
    model.ingest(.message(.fixture(seq: 1, type: .userInput)))
    model.ingest(.message(.fixture(seq: 2, type: .assistantOutput)))
    #expect(model.messages.map(\.seq) == [1, 2])
}

@Test @MainActor func approveSendsRequestIDAndDecision() async throws {
    nonisolated(unsafe) var sentDecision: String?
    nonisolated(unsafe) var sentRequestID: String?
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentRequestID = body["request_id"] as? String
        sentDecision = body["decision"] as? String
        return (200, Data())
    }
    let model = ChatModel.fixture(messages: [
        .fixture(seq: 1, type: .approvalPrompt, payload: ["request_id": .string("r1")]),
    ])
    await model.approve("allow")
    #expect(sentRequestID == "r1")
    #expect(sentDecision == "allow")
    #expect(model.hint == nil)
}

@Test @MainActor func approveBadRequestBecomesHint() async throws {
    MockURLProtocol.handler = { _ in
        (400, Data(#"{"error": "codex exec has no approval channel"}"#.utf8))
    }
    let model = ChatModel.fixture(messages: [
        .fixture(seq: 1, type: .approvalPrompt, payload: ["request_id": .string("r1")]),
    ])
    await model.approve("allow")
    #expect(model.hint == "codex exec has no approval channel")
}

@Test @MainActor func interruptAndTerminatePostToRoutes() async throws {
    nonisolated(unsafe) var paths: [String] = []
    MockURLProtocol.handler = { request in
        paths.append(request.url?.path ?? "")
        return (200, Data())
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    await model.interrupt()
    await model.terminate()
    #expect(paths == ["/harness/sessions/s1/interrupt", "/harness/sessions/s1/terminate"])
}

@Test @MainActor func transportFailureGetsGenericRetryHint() async throws {
    MockURLProtocol.handler = { _ in (500, Data(#"{"error": "boom"}"#.utf8)) }
    let model = ChatModel(client: client(), sessionID: "s1")
    await model.interrupt()
    #expect(model.hint == "Could not interrupt — try again.")
}

// MARK: - Stream lifecycle (fix round 1)

/// Regression for the stop()→start() re-entrancy race: a stale
/// fire-and-forget `stream.stop()` from the first stop() must not cancel
/// the second start()'s pump, and a finished pump must not leave
/// `pumpTask` non-nil (which would wedge start()'s idempotency guard).
@Test @MainActor func restartAfterStopStillStreamsMessages() async throws {
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"messages": [], "max_seq": 0}"#.utf8))
    }
    let connector = FakeConnector([
        .frames([chatWireMessage(seq: 1, text: "before stop")], thenError: false),
        .frames([chatWireMessage(seq: 2, text: "after restart")], thenError: false),
    ])
    let model = ChatModel(client: client(), sessionID: "s1", streamFactory: { c, s in
        MessageStream(client: c, sessionID: s, connector: connector,
                      reconnectBaseDelay: .milliseconds(10))
    })

    model.start()
    try await waitUntil { model.messages.count == 1 }

    model.stop()
    model.start()
    try await waitUntil { model.messages.count == 2 }
    #expect(model.messages.map(\.seq) == [1, 2])
}

@Test @MainActor func hasConnectedOnceLatchesOnFirstConnection() async throws {
    let model = ChatModel.fixture()
    #expect(model.hasConnectedOnce == false)
    model.ingest(.connection(false))
    #expect(model.hasConnectedOnce == false)   // never connected yet
    model.ingest(.connection(true))
    #expect(model.hasConnectedOnce == true)
    model.ingest(.connection(false))
    #expect(model.hasConnectedOnce == true)    // latches
}

/// Regression: a 401 mid-chat (surfaced via REST catch-up) must not leave
/// the UI stuck behind a silent, permanently-retrying "Reconnecting…" pill —
/// it should set a token-rejected hint and stop reconnecting.
@Test @MainActor func unauthorizedDuringCatchUpSetsHintAndStopsReconnecting() async throws {
    nonisolated(unsafe) var restCalls = 0
    MockURLProtocol.handler = { _ in
        restCalls += 1
        return (401, Data(#"{"error": "authentication required"}"#.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let model = ChatModel(client: client(), sessionID: "s1", streamFactory: { c, s in
        MessageStream(client: c, sessionID: s, connector: connector,
                      reconnectBaseDelay: .milliseconds(10))
    })

    model.start()
    try await waitUntil { model.hint == "Token rejected — check Settings." }
    // Give any errant reconnect loop a moment to fire before asserting it
    // didn't: a spinning loop would have issued a second REST call by now.
    try await Task.sleep(for: .milliseconds(50))
    #expect(restCalls == 1)
    #expect(model.isConnected == false)
}

@Test @MainActor func approveIgnoredWhileAnswerInFlight() async throws {
    nonisolated(unsafe) var calls = 0
    MockURLProtocol.handler = { _ in
        calls += 1
        return (200, Data())
    }
    let model = ChatModel.fixture(messages: [
        .fixture(seq: 1, type: .approvalPrompt, payload: ["request_id": .string("r1")]),
    ])
    // First approve sets isAnswering before its network suspension; the
    // second, started while the first is in flight, must be a no-op.
    let first = Task { await model.approve("allow") }
    let second = Task { await model.approve("deny") }
    await first.value
    await second.value
    #expect(calls == 1)
    #expect(model.isAnswering == false)
}

}
