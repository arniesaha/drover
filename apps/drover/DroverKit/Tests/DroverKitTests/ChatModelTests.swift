import Foundation
import Testing
@testable import DroverKit

private func chatWireMessage(seq: Int, text: String) -> String {
    #"{"event_id": "e\#(seq)", "seq": \#(seq), "type": "assistant_output", "role": "assistant", "text": "\#(text)", "payload": {}}"#
}

private struct TimeoutError: Error {}

private func chatTestStore() -> HarnessModelCatalogStore {
    HarnessModelCatalogStore(
        defaults: UserDefaults(suiteName: "chat-model-test-\(UUID().uuidString)")!
    )
}

private func encodedCatalog(_ catalog: HarnessModelCatalog) -> Data {
    try! JSONEncoder().encode(catalog)
}

/// Thread-safe counter: `MockURLProtocol.handler` runs off the main actor.
private final class RequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func bump() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}

/// Supplies ordered `/harness` snapshots through the same URL-protocol
/// transport used by the production client, while exposing just the request
/// count needed to prove the refresh is bounded.
private final class SnapshotClient: @unchecked Sendable {
    private let lock = NSLock()
    private var responses: [Data]
    private let repeating: Data?
    private var requestCount = 0

    let client = DroverClient(
        config: ServerConfig(urlString: "http://test.local:7080")!,
        token: "test-token",
        session: MockURLProtocol.session()
    )

    init(responses: [Data] = [], repeating: Data? = nil) {
        self.responses = responses
        self.repeating = repeating
    }

    func nextResponse() -> Data {
        lock.lock()
        defer { lock.unlock() }
        requestCount += 1
        if !responses.isEmpty { return responses.removeFirst() }
        return repeating ?? Data(#"{"hosts": [], "sessions": [], "cwd_suggestions": []}"#.utf8)
    }

    var snapshotRequestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return requestCount
    }
}

/// A deliberately non-cooperative response: once the request is in flight,
/// cancellation cannot make its already-produced snapshot disappear. This
/// exposes stale state writes after a chat has been stopped or superseded.
private final class DelayedSnapshotResponse: @unchecked Sendable {
    private let lock = NSLock()
    private var started = false
    private let release = DispatchSemaphore(value: 0)
    private let response: Data

    init(_ response: Data) {
        self.response = response
    }

    func waitForRelease() -> Data {
        lock.lock()
        started = true
        lock.unlock()
        release.wait()
        return response
    }

    var hasStarted: Bool {
        lock.lock()
        defer { lock.unlock() }
        return started
    }

    func finish() { release.signal() }
}

private func snapshotClient(responses: [Data] = [], repeating: Data? = nil) -> SnapshotClient {
    let snapshotClient = SnapshotClient(responses: responses, repeating: repeating)
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        return (200, snapshotClient.nextResponse())
    }
    return snapshotClient
}

private func sessionJSON(
    recap: String? = nil,
    source: Int? = nil,
    preview: String? = nil,
    model: String? = nil,
    thinkingEffort: String? = nil
) -> Data {
    let recapField = recap.map { #", "recap": "\#($0)""# } ?? ""
    let sourceField = source.map { #", "recap_source_seq": \#($0)"# } ?? ""
    let previewField = preview.map { #", "preview": "\#($0)""# } ?? ""
    let modelField = model.map { #", "model": "\#($0)""# } ?? ""
    let effortField = thinkingEffort.map { #", "thinking_effort": "\#($0)""# } ?? ""
    return Data("""
    {"hosts": [{"host_id": "host-1", "status": "online",
      "capabilities": {"display_name": "Host", "harnesses": [
        {"name": "codex", "enabled": true}]}}],
     "sessions": [{"session_id": "s1", "host_id": "host-1", "harness": "codex",
       "mode": "structured", "status": "running", "awaiting": null\(recapField)\(sourceField)\(previewField)\(modelField)\(effortField)}],
     "cwd_suggestions": []}
    """.utf8)
}

private func turnComplete(seq: Int) -> HarnessMessage {
    .fixture(seq: seq, type: .status, payload: ["turn_complete": .bool(true)])
}

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

@MainActor
private func eventually(_ condition: () -> Bool) async {
    do {
        try await waitUntil(timeout: .seconds(1), condition)
    } catch {
        Issue.record("Timed out waiting for asynchronous condition")
    }
}

/// `.serialized`: several tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
extension MockNetworkTests {
@Suite(.serialized)
struct ChatModelTests {

@Test @MainActor func initialRecapBecomesHeaderTitle() {
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex",
                          recap: "Improving previews; awaiting tests.", recapSourceSeq: 8)

    #expect(model.headerTitle == "Improving previews; awaiting tests.")
}

@Test @MainActor func missingContextUsesHarnessOnlyHeaderMetadata() {
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex")

    #expect(model.headerMetadata == "Codex")
}

@Test @MainActor func headerMetadataJoinsHarnessAndContextGauge() {
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "codex",
        initialMessages: [.fixture(
            seq: 12, type: .status,
            payload: [
                "turn_complete": .bool(true),
                "usage": .object(["input_tokens": .number(100)]),
                "context_input_tokens": .number(100),
                "model_context_window": .number(1_000),
            ]
        )]
    )

    #expect(model.headerMetadata == "Codex · ctx 100 / 1K · 10%")
}

@Test @MainActor func turnCompletePollsUntilRecapReachesSourceSequence() async {
    let snapshots = snapshotClient(responses: [
        sessionJSON(recap: "Old", source: 8),
        sessionJSON(recap: "New", source: 12),
    ])
    let model = ChatModel(client: snapshots.client, sessionID: "s1", harness: "codex",
                          recap: "Old", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 3)

    model.ingest(.message(turnComplete(seq: 12)))

    await eventually { model.recap == "New" }
    #expect(model.recapSourceSeq == 12)
    #expect(snapshots.snapshotRequestCount == 2)
}

@Test @MainActor func recapPollRetriesAfterFailedSnapshot() async {
    let requests = RequestCounter()
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        requests.bump()
        if requests.value == 1 {
            return (500, Data(#"{"error": "temporary failure"}"#.utf8))
        }
        return (200, sessionJSON(recap: "Recovered", source: 12))
    }
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex",
                          recap: "Old", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 3)

    model.ingest(.message(turnComplete(seq: 12)))

    await eventually { model.recap == "Recovered" }
    #expect(model.recapSourceSeq == 12)
    #expect(requests.value == 2)
}

@Test @MainActor func recapPollKeepsCurrentTextUntilTargetSourceArrives() async {
    let requests = RequestCounter()
    let target = DelayedSnapshotResponse(sessionJSON(recap: "Target", source: 12))
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        requests.bump()
        if requests.value == 1 {
            return (200, sessionJSON(recap: "Intermediate", source: 10))
        }
        return (200, target.waitForRelease())
    }
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex",
                          recap: "Old", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 3)

    model.ingest(.message(turnComplete(seq: 12)))
    await eventually { target.hasStarted }

    #expect(model.recap == "Old")
    #expect(model.recapSourceSeq == 8)

    target.finish()
    await eventually { model.recap == "Target" }
    #expect(model.recapSourceSeq == 12)
}

@Test @MainActor func delayedRecapDoesNotReseedEditedTurnPreferences() async {
    let store = chatTestStore()
    let editedModel = HarnessModelOption(
        id: "edited-model", displayName: "Edited", description: nil,
        isDefault: false,
        reasoning: HarnessReasoningOptions(supported: ["high"], default: "high")
    )
    let catalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "session-model",
        supportedEfforts: ["low"], additionalModels: [editedModel]
    )
    let delayed = DelayedSnapshotResponse(sessionJSON(
        recap: "Target", source: 12,
        model: "session-model", thinkingEffort: "low"
    ))
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        return (200, delayed.waitForRelease())
    }
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "codex", store: store,
        recap: "Old", recapSourceSeq: 8,
        recapPollInterval: .zero, recapPollAttempts: 1
    )
    model.runPreferences.select(hostID: "host-1", harness: "codex")
    model.runPreferences.apply(catalog)
    model.runPreferences.selectedModel = "session-model"
    model.runPreferences.thinkingEffort = "low"

    model.ingest(.message(turnComplete(seq: 12)))
    await eventually { delayed.hasStarted }
    model.runPreferences.selectedModel = "edited-model"
    model.runPreferences.thinkingEffort = "high"
    delayed.finish()

    await eventually { model.recap == "Target" }
    #expect(model.runPreferences.selectedModel == "edited-model")
    #expect(model.runPreferences.thinkingEffort == "high")
}

@Test @MainActor func recapPollStopsAfterConfiguredAttemptsAndKeepsLastGoodText() async {
    let snapshots = snapshotClient(repeating: sessionJSON(recap: "Old", source: 8))
    let model = ChatModel(client: snapshots.client, sessionID: "s1", harness: "codex",
                          recap: "Old", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 3)

    model.ingest(.message(turnComplete(seq: 12)))

    await eventually { snapshots.snapshotRequestCount == 3 }
    #expect(model.recap == "Old")
}

@Test @MainActor func missingGeneratedRecapDoesNotReplaceCurrentTextWithPreview() async {
    let snapshots = snapshotClient(responses: [sessionJSON(preview: "Fallback preview")])
    let model = ChatModel(client: snapshots.client, sessionID: "s1", harness: "codex",
                          recap: "Current", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 1)

    model.ingest(.message(turnComplete(seq: 12)))

    await eventually { snapshots.snapshotRequestCount == 1 }
    #expect(model.recap == "Current")
    #expect(model.recapSourceSeq == 8)
}

@Test @MainActor func olderGeneratedRecapDoesNotReplaceNewerState() async {
    let snapshots = snapshotClient(responses: [sessionJSON(recap: "Stale", source: 7)])
    let model = ChatModel(client: snapshots.client, sessionID: "s1", harness: "codex",
                          recap: "Current", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 1)

    model.ingest(.message(turnComplete(seq: 12)))

    await eventually { snapshots.snapshotRequestCount == 1 }
    #expect(model.recap == "Current")
    #expect(model.recapSourceSeq == 8)
}

@Test @MainActor func recapTransportFailureKeepsCurrentText() async throws {
    MockURLProtocol.transportError = URLError(.notConnectedToInternet)
    defer { MockURLProtocol.transportError = nil }
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex",
                          recap: "Current", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 1)

    model.ingest(.message(turnComplete(seq: 12)))
    try await Task.sleep(for: .milliseconds(50))

    #expect(model.recap == "Current")
    #expect(model.recapSourceSeq == 8)
}

@Test @MainActor func stopCancelsPendingRecapPoll() async throws {
    let snapshots = snapshotClient(repeating: sessionJSON(recap: "Old", source: 8))
    let model = ChatModel(client: snapshots.client, sessionID: "s1", harness: "codex",
                          recap: "Old", recapSourceSeq: 8,
                          recapPollInterval: .seconds(1), recapPollAttempts: 3)

    model.ingest(.message(turnComplete(seq: 12)))
    await eventually { snapshots.snapshotRequestCount == 1 }
    model.stop()
    try await Task.sleep(for: .milliseconds(50))

    #expect(snapshots.snapshotRequestCount == 1)
}

@Test @MainActor func stopDoesNotApplyALateRecapSnapshot() async throws {
    let delayed = DelayedSnapshotResponse(sessionJSON(recap: "Late", source: 12))
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        return (200, delayed.waitForRelease())
    }
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex",
                          recap: "Current", recapSourceSeq: 8,
                          recapPollInterval: .zero, recapPollAttempts: 1)

    model.ingest(.message(turnComplete(seq: 12)))
    await eventually { delayed.hasStarted }
    model.stop()
    delayed.finish()
    try await Task.sleep(for: .milliseconds(50))

    #expect(model.recap == "Current")
    #expect(model.recapSourceSeq == 8)
}

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
    // Note: the specific "turn already in flight" conflict queues instead
    // (see inFlightConflictQueuesTurnAndAutoSendsOnTurnComplete); every
    // other 409 still surfaces verbatim as a hint.
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "session is terminating"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "next thing"
    await model.sendTurn()
    #expect(model.hint == "session is terminating")
    #expect(model.composerText == "next thing")   // preserved for retry
}

@Test @MainActor func concurrentSendsIssueExactlyOneRequest() async throws {
    // Regression: nine taps during a cellular stall produced nine accepted
    // turns (seq 876, 878-885, all "Yes") because sendTurn had no guard.
    let counter = RequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        Thread.sleep(forTimeInterval: 0.3)   // hold the request in flight
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "Yes"

    async let first: Void = model.sendTurn()
    async let second: Void = model.sendTurn()
    async let third: Void = model.sendTurn()
    _ = await (first, second, third)

    #expect(counter.value == 1)
    #expect(model.composerText == "")
    #expect(model.isSending == false)
}

@Test @MainActor func guardClearsSoTheNextSendStillWorks() async throws {
    let counter = RequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "first"
    await model.sendTurn()
    model.composerText = "second"
    await model.sendTurn()
    #expect(counter.value == 2)
}

@Test @MainActor func failedSendReArmsAndKeepsTheText() async throws {
    MockURLProtocol.transportError = URLError(.notConnectedToInternet)
    defer { MockURLProtocol.transportError = nil }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "Yes"
    await model.sendTurn()
    #expect(model.composerText == "Yes")   // retry without retyping
    #expect(model.isSending == false)      // not wedged
}

@Test @MainActor func handOffReturnsNewSessionID() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/continue")
        return (201, Data(#"{"session_id": "harness-continued"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let continued = await model.handOff()
    #expect(continued?.sessionID == "harness-continued")
    #expect(continued?.isStructured == false)
    #expect(model.hint == nil)
}

@Test @MainActor func handOffSurfacesStructuredModeForNavigation() async throws {
    MockURLProtocol.handler = { _ in
        (201, Data(#"{"session_id": "harness-continued", "mode": "structured"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let continued = await model.handOff(targetHarness: "codex")
    #expect(continued?.isStructured == true)
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
    let continued = await model.handOff(targetHarness: "codex")
    #expect(continued?.sessionID == "harness-continued")
    #expect(sentTarget == "codex")
}

@Test @MainActor func initUsesProvidedHarnessForPresentation() async throws {
    let model = ChatModel(client: client(), sessionID: "s1", harness: "codex")
    #expect(model.harnessPresentation.name == "Codex")
    #expect(model.harnessPresentation.symbolName == "chevron.left.forwardslash.chevron.right")
}

@Test @MainActor func loadSessionMetadataUpdatesHarnessPresentation() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let model = ChatModel(client: client(), sessionID: "harness-1", harness: "codex")
    #expect(model.harnessPresentation.name == "Codex")
    await model.loadSessionMetadata()
    #expect(model.harnessPresentation.name == "Antigravity")
    #expect(model.harnessPresentation.symbolName == "sparkles")
}

@Test @MainActor func loadSessionMetadataListsHostHarnesses() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let model = ChatModel(client: client(), sessionID: "harness-1")
    #expect(model.handoffHarnesses.isEmpty)
    await model.loadSessionMetadata()
    #expect(model.handoffHarnesses == ["shell", "claude-code", "agy"])
}

@Test @MainActor func sessionMetadataSelectsActualPairAndOverridesStoredPreference() async throws {
    let store = chatTestStore()
    let sessionModel = HarnessModelOption(
        id: "session-model", displayName: "Session", description: nil,
        isDefault: false,
        reasoning: HarnessReasoningOptions(supported: ["high"], default: "high")
    )
    let catalog = fixtureCatalog(
        hostID: "mac-mini", harness: "codex", scope: "scope-chat",
        model: "stored-model", supportedEfforts: ["low"],
        additionalModels: [sessionModel]
    )
    store.save(catalog: catalog)
    store.save(
        selection: HarnessModelSelection(
            accountScopeID: "scope-chat", model: "stored-model", thinkingEffort: "low"
        ),
        hostID: "mac-mini", harness: "codex"
    )
    let snapshot = Data("""
    {"hosts": [{"host_id": "mac-mini", "status": "online",
      "capabilities": {"display_name": "Mac Mini", "harnesses": [
        {"name": "codex", "enabled": true}]}}],
     "sessions": [
      {"session_id": "harness-preferred", "host_id": "mac-mini", "harness": "codex",
       "mode": "structured", "status": "running", "awaiting": null,
       "model": "session-model", "thinking_effort": "high"}],
     "cwd_suggestions": []}
    """.utf8)
    MockURLProtocol.handler = { request in
        request.url?.path == "/harness" ? (200, snapshot) : (200, encodedCatalog(catalog))
    }
    let model = ChatModel(
        client: client(), sessionID: "harness-preferred", store: store
    )

    await model.loadSessionMetadata()

    #expect(model.runPreferences.hostID == "mac-mini")
    #expect(model.runPreferences.harness == "codex")
    #expect(model.runPreferences.selectedModel == "session-model")
    #expect(model.runPreferences.thinkingEffort == "high")
}

@Test @MainActor func whitespaceSessionMetadataRestoresStoredScopedPreference() async throws {
    let store = chatTestStore()
    let catalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "stored-model",
        supportedEfforts: ["high"]
    )
    store.save(catalog: catalog)
    store.save(
        selection: HarnessModelSelection(
            accountScopeID: "scope-chat", model: "stored-model", thinkingEffort: "high"
        ),
        hostID: "host-1", harness: "codex"
    )
    MockURLProtocol.handler = { request in
        request.url?.path == "/harness"
            ? (200, sessionJSON(model: "   ", thinkingEffort: "  \t "))
            : (200, encodedCatalog(catalog))
    }
    let model = ChatModel(client: client(), sessionID: "s1", store: store)

    await model.loadSessionMetadata()

    #expect(model.runPreferences.selectedModel == "stored-model")
    #expect(model.runPreferences.thinkingEffort == "high")
}

@Test @MainActor func freshCatalogRestoresSessionSeedMissingFromStaleCache() async throws {
    let store = chatTestStore()
    let staleCatalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "stale-model", stale: true
    )
    store.save(catalog: staleCatalog)
    let freshCatalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "session-model",
        supportedEfforts: ["high"]
    )
    let delayedCatalog = DelayedSnapshotResponse(encodedCatalog(freshCatalog))
    MockURLProtocol.handler = { request in
        if request.url?.path == "/harness" {
            return (200, sessionJSON(model: "session-model", thinkingEffort: "high"))
        }
        return (200, delayedCatalog.waitForRelease())
    }
    let model = ChatModel(client: client(), sessionID: "s1", store: store)

    let load = Task { await model.loadSessionMetadata() }
    await eventually { delayedCatalog.hasStarted }
    #expect(model.runPreferences.modelOverride == nil)
    #expect(model.runPreferences.thinkingEffortOverride == nil)
    delayedCatalog.finish()
    await load.value

    #expect(model.runPreferences.selectedModel == "session-model")
    #expect(model.runPreferences.thinkingEffort == "high")
    #expect(model.runPreferences.modelOverride == "session-model")
    #expect(model.runPreferences.thinkingEffortOverride == "high")
}

@Test @MainActor func loadSessionMetadataUnknownSessionLeavesListEmpty() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    let model = ChatModel(client: client(), sessionID: "not-in-snapshot")
    await model.loadSessionMetadata()
    #expect(model.handoffHarnesses.isEmpty)
}

@Test @MainActor func handOffFailureBecomesHint() async throws {
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "host offline"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    let continued = await model.handOff()
    #expect(continued == nil)
    #expect(model.hint == "host offline")
}

// MARK: - Turn queueing (409 "turn already in flight")

@Test @MainActor func inFlightConflictQueuesTurnAndAutoSendsOnTurnComplete() async throws {
    nonisolated(unsafe) var turnPosts: [String] = []
    MockURLProtocol.handler = { request in
        if request.httpMethod == "GET" { return (200, sessionJSON()) }
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        turnPosts.append(body["text"] as? String ?? "")
        if turnPosts.count == 1 {
            return (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        return (202, Data(#"{"turn_id": "t2"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "follow-up question"
    await model.sendTurn()

    // Rejected turn is queued, not lost — composer clears, soft hint shows.
    #expect(model.queuedTurn == "follow-up question")
    #expect(model.composerText.isEmpty)
    #expect(model.hint == "Queued — sends when the current response finishes.")

    // The harness finishing its turn (status event with turn_complete)
    // dispatches the queued text automatically.
    model.ingest(.message(.fixture(seq: 9, type: .status,
                                   payload: ["turn_complete": .bool(true),
                                             "awaiting": .string("input")])))
    try await waitUntil { turnPosts.count == 2 }
    #expect(turnPosts[1] == "follow-up question")
    try await waitUntil { model.queuedTurn == nil }
    #expect(model.hint == nil)
}

@Test @MainActor func historicalTurnCompletionDoesNotDispatchQueuedTurn() async throws {
    let counter = RequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        return (409, Data(#"{"error": "turn already in flight"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "wait for the live completion"
    await model.sendTurn()
    #expect(model.queuedTurn == "wait for the live completion")

    model.ingest(.history([
        .fixture(
            seq: 9,
            type: .status,
            payload: ["turn_complete": .bool(true), "awaiting": .string("input")]
        ),
    ], decodeIssues: []))
    try await Task.sleep(for: .milliseconds(30))

    #expect(counter.value == 1)
    #expect(model.queuedTurn == "wait for the live completion")
}

@Test @MainActor func sendTurnPassesAttachmentsAndClearsThem() async throws {
    let attachment = TurnAttachment(mediaType: "image/jpeg", data: Data([0x01, 0x02]))
    nonisolated(unsafe) var sentImages: [[String: Any]] = []
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentImages = body["images"] as? [[String: Any]] ?? []
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "hi"
    model.pendingAttachments = [attachment]
    await model.sendTurn()
    #expect(sentImages.count == 1)
    #expect(sentImages[0]["data_base64"] as? String == attachment.data.base64EncodedString())
    #expect(model.pendingAttachments.isEmpty)
}

@Test @MainActor func refreshedCatalogRemovesUnsupportedPreferenceBeforeTurn() async throws {
    let store = chatTestStore()
    let oldCatalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "removed-model"
    )
    store.save(catalog: oldCatalog)
    store.save(
        selection: HarnessModelSelection(
            accountScopeID: "scope-chat", model: "removed-model", thinkingEffort: "high"
        ),
        hostID: "host-1", harness: "codex"
    )
    let freshCatalog = fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "available-model"
    )
    nonisolated(unsafe) var preferenceKeys: [String] = []
    MockURLProtocol.handler = { request in
        switch (request.httpMethod, request.url?.path) {
        case ("GET", "/harness"):
            return (200, sessionJSON())
        case ("GET", _):
            return (200, encodedCatalog(freshCatalog))
        default:
            let body = try! JSONSerialization.jsonObject(
                with: request.bodyStreamData()) as! [String: Any]
            preferenceKeys = body.keys.filter {
                $0 == "model" || $0 == "thinking_effort"
            }
            return (202, Data(#"{"turn_id":"t1"}"#.utf8))
        }
    }
    let model = ChatModel(client: client(), sessionID: "s1", store: store)

    await model.loadSessionMetadata()
    model.composerText = "hi"
    await model.sendTurn()

    #expect(model.runPreferences.selectedModel.isEmpty)
    #expect(model.runPreferences.thinkingEffort.isEmpty)
    #expect(preferenceKeys.isEmpty)
}

@Test @MainActor func codexSendsValidCatalogOverridesUnchanged() async throws {
    nonisolated(unsafe) var sentModel: String?
    nonisolated(unsafe) var sentThinking: String?
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentModel = body["model"] as? String
        sentThinking = body["thinking_effort"] as? String
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "codex", store: chatTestStore()
    )
    model.runPreferences.select(hostID: "host-1", harness: "codex")
    model.runPreferences.apply(fixtureCatalog(
        hostID: "host-1", scope: "scope-chat", model: "gpt-5.6-sol",
        supportedEfforts: ["xhigh"]
    ))
    model.composerText = "hi"
    model.runPreferences.selectedModel = "gpt-5.6-sol"
    model.runPreferences.thinkingEffort = "xhigh"
    await model.sendTurn()
    #expect(sentModel == "gpt-5.6-sol")
    #expect(sentThinking == "xhigh")
}

@Test @MainActor func agySendsModelWithoutSeparateEffortWhenMetadataHasNone() async throws {
    nonisolated(unsafe) var sentModel: String?
    nonisolated(unsafe) var sentThinking = false
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        sentModel = body["model"] as? String
        sentThinking = body.keys.contains("thinking_effort")
        return (202, Data(#"{"turn_id":"t1"}"#.utf8))
    }
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "agy", store: chatTestStore()
    )
    model.runPreferences.select(hostID: "host-1", harness: "agy")
    model.runPreferences.apply(HarnessModelCatalog(
        schemaVersion: 1, hostID: "host-1", harness: "agy",
        accountScopeID: "scope-agy", harnessVersion: nil, discoveredAt: nil,
        stale: false, staleReason: nil,
        models: [HarnessModelOption(
            id: "gemini-3.6-flash-high", displayName: "Gemini", description: nil,
            isDefault: false, reasoning: nil
        )]
    ))
    model.runPreferences.selectedModel = "gemini-3.6-flash-high"
    model.runPreferences.thinkingEffort = "high"
    model.composerText = "hi"

    await model.sendTurn()

    #expect(sentModel == "gemini-3.6-flash-high")
    #expect(sentThinking == false)
}

@Test @MainActor func sendTurnOmitsLockedClaudePreferences() async throws {
    nonisolated(unsafe) var sentModel = false
    nonisolated(unsafe) var sentThinking = false
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentModel = body.keys.contains("model")
        sentThinking = body.keys.contains("thinking_effort")
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "claude-code",
        store: chatTestStore()
    )
    model.runPreferences.select(hostID: "host-1", harness: "claude-code")
    model.runPreferences.apply(fixtureCatalog(
        hostID: "host-1", harness: "claude-code", scope: "scope-chat",
        model: "opus", supportedEfforts: ["high"]
    ))
    model.composerText = "hi"
    model.runPreferences.selectedModel = "opus"
    model.runPreferences.thinkingEffort = "high"

    await model.sendTurn()

    #expect(sentModel == false)
    #expect(sentThinking == false)
    #expect(HarnessRunPreferences.canChangeInExistingSession("claude-code") == false)
}

@Test @MainActor func queuedTurnOmitsLockedClaudePreferences() async throws {
    nonisolated(unsafe) var preferenceKeys: [[String]] = []
    MockURLProtocol.handler = { request in
        if request.httpMethod == "GET" { return (200, sessionJSON()) }
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        preferenceKeys.append(body.keys.filter { $0 == "model" || $0 == "thinking_effort" })
        if preferenceKeys.count == 1 {
            return (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        return (202, Data(#"{"turn_id": "t2"}"#.utf8))
    }
    let model = ChatModel(
        client: client(), sessionID: "s1", harness: "claude-code",
        store: chatTestStore()
    )
    model.runPreferences.select(hostID: "host-1", harness: "claude-code")
    model.runPreferences.apply(fixtureCatalog(
        hostID: "host-1", harness: "claude-code", scope: "scope-chat",
        model: "opus", supportedEfforts: ["high"]
    ))
    model.composerText = "queued"
    model.runPreferences.selectedModel = "opus"
    model.runPreferences.thinkingEffort = "high"
    await model.sendTurn()

    model.ingest(.message(.fixture(
        seq: 9,
        type: .status,
        payload: ["turn_complete": .bool(true), "awaiting": .string("input")]
    )))
    try await waitUntil { preferenceKeys.count == 2 }

    #expect(preferenceKeys == [[], []])
}

@Test @MainActor func imageOnlyTurnSends() async throws {
    nonisolated(unsafe) var sentTexts: [String] = []
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        sentTexts.append(body["text"] as? String ?? "missing")
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.pendingAttachments = [TurnAttachment(mediaType: "image/jpeg", data: Data([0x03]))]
    await model.sendTurn()
    #expect(sentTexts == [""])
    #expect(model.pendingAttachments.isEmpty)
}

@Test @MainActor func attachmentsSurviveConflictQueueing() async throws {
    let attachment = TurnAttachment(mediaType: "image/png", data: Data([0x0A, 0x0B]))
    nonisolated(unsafe) var turnPosts: [[String: Any]] = []
    MockURLProtocol.handler = { request in
        if request.httpMethod == "GET" { return (200, sessionJSON()) }
        let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
        turnPosts.append(body)
        if turnPosts.count == 1 {
            return (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        return (202, Data(#"{"turn_id": "t2"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "see attached"
    model.pendingAttachments = [attachment]
    await model.sendTurn()

    #expect(model.pendingAttachments.isEmpty)   // moved to the queue, not lost
    #expect(model.queuedTurn == "see attached")

    model.ingest(.message(.fixture(seq: 9, type: .status,
                                   payload: ["turn_complete": .bool(true),
                                             "awaiting": .string("input")])))
    try await waitUntil { turnPosts.count == 2 }
    let retriedImages = turnPosts[1]["images"] as? [[String: Any]] ?? []
    #expect(retriedImages.count == 1)
    #expect(retriedImages[0]["data_base64"] as? String == attachment.data.base64EncodedString())
}

@Test @MainActor func otherConflictsStillSurfaceAsHintNotQueue() async throws {
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "approval pending; answer it first"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "do it anyway"
    await model.sendTurn()
    #expect(model.queuedTurn == nil)
    #expect(model.composerText == "do it anyway")   // preserved for retry
    #expect(model.hint == "approval pending; answer it first")
}

@Test @MainActor func unavailableRecoveryPreservesComposerForNewSession() async throws {
    let message = "Session cannot be resumed after the harness restart. Continue it in a new session."
    let attachment = TurnAttachment(mediaType: "image/png", data: Data([0x0C, 0x0D]))
    MockURLProtocol.handler = { _ in
        (409, Data(#"{"error": "\#(message)"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "continue from here"
    model.pendingAttachments = [attachment]

    await model.sendTurn()

    #expect(model.composerText == "continue from here")
    #expect(model.pendingAttachments.count == 1)
    #expect(model.pendingAttachments[0].data == attachment.data)
    #expect(model.queuedTurn == nil)
    #expect(model.hint == message)
}

@Test @MainActor func turnCompleteWithoutQueueDoesNotPostTurn() async throws {
    nonisolated(unsafe) var posts = 0
    MockURLProtocol.handler = { request in
        if request.httpMethod == "GET" { return (200, sessionJSON()) }
        posts += 1
        return (202, Data(#"{"turn_id": "t"}"#.utf8))
    }
    let model = ChatModel.fixture()
    model.ingest(.message(.fixture(seq: 1, type: .status,
                                   payload: ["turn_complete": .bool(true)])))
    try await Task.sleep(for: .milliseconds(50))
    #expect(posts == 0)
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
    MockURLProtocol.handler = { request in
        let afterSeq = URLComponents(
            url: request.url!, resolvingAgainstBaseURL: false
        )?.queryItems?.first(where: { $0.name == "after_seq" })?.value ?? "0"
        return (200, Data("""
        {"messages": [], "max_seq": \(afterSeq),
         "has_older": false, "has_newer": false}
        """.utf8))
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

@Test @MainActor func olderHistoryIsLoadedOnlyAfterExplicitRequest() async throws {
    MockURLProtocol.handler = { request in
        switch request.url?.query {
        case "limit=50":
            return (200, Data("""
            {"messages": [\(chatWireMessage(seq: 4, text: "four")), \(chatWireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        case "before_seq=4&limit=50":
            return (200, Data("""
            {"messages": [\(chatWireMessage(seq: 1, text: "one")), \(chatWireMessage(seq: 2, text: "two")), \(chatWireMessage(seq: 3, text: "three"))],
             "page_min_seq": 1, "page_max_seq": 3, "max_seq": 5,
             "has_older": false, "has_newer": true}
            """.utf8))
        default:
            Issue.record("unexpected request: \(request.url?.absoluteString ?? "nil")")
            return (500, Data())
        }
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let model = ChatModel(client: client(), sessionID: "s1", streamFactory: { client, sessionID in
        // Two-message cold window: the older page stays behind the explicit
        // request rather than being pulled in while assembling the window.
        MessageStream(client: client, sessionID: sessionID, connector: connector,
                      coldWindowSize: 2)
    })

    model.start()
    try await waitUntil { model.messages.map(\.seq) == [4, 5] }
    #expect(model.hasOlderHistory)

    let didLoad = await model.loadOlderHistory()

    #expect(didLoad)
    #expect(model.messages.map(\.seq) == [1, 2, 3, 4, 5])
    #expect(model.hasOlderHistory == false)
    #expect(model.isLoadingOlderHistory == false)
    model.stop()
}

@Test @MainActor func failedOlderHistoryLoadReportsNoPrepend() async throws {
    MockURLProtocol.handler = { request in
        if request.url?.query == "limit=50" {
            return (200, Data("""
            {"messages": [\(chatWireMessage(seq: 4, text: "four")), \(chatWireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
        return (500, Data(#"{"error": "transient"}"#.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let model = ChatModel(client: client(), sessionID: "s1", streamFactory: { client, sessionID in
        // Two-message cold window: the older page stays behind the explicit
        // request rather than being pulled in while assembling the window.
        MessageStream(client: client, sessionID: sessionID, connector: connector,
                      coldWindowSize: 2)
    })

    model.start()
    try await waitUntil { model.messages.map(\.seq) == [4, 5] }

    let didLoad = await model.loadOlderHistory()

    #expect(didLoad == false)
    #expect(model.messages.map(\.seq) == [4, 5])
    #expect(model.hint == "Could not load earlier messages — try again.")
    model.stop()
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

}  // extension MockNetworkTests
