import Foundation
import Testing
@testable import DroverKit

/// Scripted fake: each connect() call pops the next scenario.
final class FakeConnector: WebSocketConnecting, @unchecked Sendable {
    enum Scenario { case frames([String], thenError: Bool) }
    var scenarios: [Scenario]
    var requests: [URLRequest] = []
    var terminationCount = 0
    init(_ scenarios: [Scenario]) { self.scenarios = scenarios }
    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        requests.append(request)
        let scenario = scenarios.isEmpty ? .frames([], thenError: false) : scenarios.removeFirst()
        return AsyncThrowingStream { continuation in
            continuation.onTermination = { [weak self] _ in
                self?.terminationCount += 1
            }
            guard case let .frames(frames, thenError) = scenario else { return }
            for frame in frames { continuation.yield(frame) }
            if thenError {
                continuation.finish(throwing: URLError(.networkConnectionLost))
            }
            // no finish otherwise: socket stays open
        }
    }
}

private func wireMessage(seq: Int, text: String) -> String {
    #"{"event_id": "e\#(seq)", "seq": \#(seq), "type": "assistant_output", "role": "assistant", "text": "\#(text)", "payload": {}}"#
}

/// `.serialized`: every test in this file mutates the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment for why that
/// requires serialization rather than Swift Testing's default parallelism.
extension MockNetworkTests {
@Suite(.serialized)
struct StreamTests {

@Test func historyThenLiveDedupedAndOrdered() async throws {
    // REST returns history seq 1-2; WS replays 2 (dup) then delivers 3.
    MockURLProtocol.handler = { request in
        #expect(request.url!.query == "limit=50")
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two"))],
         "max_seq": 2}
        """.utf8))
    }
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: FakeConnector([.frames([wireMessage(seq: 2, text: "two"),
                                           wireMessage(seq: 3, text: "three")],
                                          thenError: false)]))
    var texts: [String] = []
    for await event in await stream.events() {
        switch event {
        case let .message(message): texts.append(message.text)
        case let .history(messages, _): texts.append(contentsOf: messages.map(\.text))
        case .connection, .unauthorized: break
        }
        if texts.count == 3 { break }
    }
    #expect(texts == ["one", "two", "three"])
}

@Test func reconnectCatchesUpFromLastSeq() async throws {
    // First WS errors after seq 1; catch-up REST must be called with after_seq=1.
    nonisolated(unsafe) var restCalls: [String] = []
    MockURLProtocol.handler = { request in
        restCalls.append(request.url!.query ?? "")
        if restCalls.count == 1 {
            return (200, Data(#"{"messages": [], "max_seq": 0}"#.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 2, text: "after-reconnect"))], "max_seq": 2}
        """.utf8))
    }
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: FakeConnector([
            .frames([wireMessage(seq: 1, text: "pre")], thenError: true),
            .frames([], thenError: false),
        ]),
        reconnectBaseDelay: .milliseconds(10))
    var got: [String] = []
    var sawDisconnect = false
    var sawReconnectUp = false
    for await event in await stream.events() {
        switch event {
        case let .message(m): got.append(m.text)
        case let .history(messages, _): got.append(contentsOf: messages.map(\.text))
        case let .connection(up):
            if !up { sawDisconnect = true }
            else if sawDisconnect { sawReconnectUp = true }
        case .unauthorized: break
        }
        if got.count == 2, sawReconnectUp { break }
    }
    #expect(got == ["pre", "after-reconnect"])
    #expect(sawDisconnect)
    #expect(sawReconnectUp)   // reconnecting indicator must clear again
    #expect(restCalls.last!.contains("after_seq=1"))
}

@Test func websocketStartsAfterRestCatchupSeq() async throws {
    MockURLProtocol.handler = { _ in
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two"))],
         "max_seq": 2}
        """.utf8))
    }
    let connector = FakeConnector([.frames([wireMessage(seq: 3, text: "live")], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: connector,
        reconnectBaseDelay: .milliseconds(10))
    var texts: [String] = []
    for await event in await stream.events() {
        switch event {
        case let .message(message): texts.append(message.text)
        case let .history(messages, _): texts.append(contentsOf: messages.map(\.text))
        case .connection, .unauthorized: break
        }
        if texts.count == 3 { break }
    }
    #expect(connector.requests.first?.url?.query == "after_seq=2")
}

@Test func catchUpFailureBacksOffAndRetries() async throws {
    // First REST catch-up 500s; the stream must not proceed to WS with a
    // history gap — it emits .connection(false), backs off, retries the
    // catch-up, and only then comes up.
    nonisolated(unsafe) var restCalls = 0
    MockURLProtocol.handler = { _ in
        restCalls += 1
        if restCalls == 1 {
            return (500, Data(#"{"error": "boom"}"#.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two"))],
         "max_seq": 2}
        """.utf8))
    }
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: FakeConnector([.frames([], thenError: false)]),
        reconnectBaseDelay: .milliseconds(10))
    var texts: [String] = []
    var connections: [Bool] = []
    for await event in await stream.events() {
        switch event {
        case let .message(m): texts.append(m.text)
        case let .history(messages, _): texts.append(contentsOf: messages.map(\.text))
        case let .connection(up): connections.append(up)
        case .unauthorized: break
        }
        if texts.count == 2, connections.contains(true) { break }
    }
    #expect(texts == ["one", "two"])
    #expect(connections == [false, true])
    #expect(restCalls == 2)
}

@Test func catchUpUnauthorizedEmitsTerminalSignalWithoutSpinning() async throws {
    // A 401 on REST catch-up is never recoverable by retrying the same
    // token: the stream must emit .unauthorized exactly once and stop for
    // good, never re-issuing the REST call or falling through to WS.
    nonisolated(unsafe) var restCalls = 0
    MockURLProtocol.handler = { _ in
        restCalls += 1
        return (401, Data(#"{"error": "authentication required"}"#.utf8))
    }
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: FakeConnector([.frames([], thenError: false)]),
        reconnectBaseDelay: .milliseconds(10))
    var events: [StreamEvent] = []
    for await event in await stream.events() {
        events.append(event)
    }
    #expect(events == [.unauthorized])
    #expect(restCalls == 1)
}

@Test func coldCatchUpEmitsOnlyTheNewestPageBeforeLiveMessages() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        switch queries.count {
        case 1:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 4, text: "four")), \(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        case 2:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
             "page_min_seq": 2, "page_max_seq": 3, "max_seq": 6,
             "has_older": true, "has_newer": true}
            """.utf8))
        default:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 1, text: "one"))],
             "page_min_seq": 1, "page_max_seq": 1, "max_seq": 6,
             "has_older": false, "has_newer": true}
            """.utf8))
        }
    }
    let connector = FakeConnector([
        .frames([wireMessage(seq: 6, text: "six")], thenError: false)
    ])
    // A two-message cold window, so the first page fills it and the older
    // pages the handler can still serve stay behind the explicit request.
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10), coldWindowSize: 2
    )

    var batches: [[Int]] = []
    var live: [Int] = []
    var catchUpFailed = false
    for await event in await stream.events() {
        switch event {
        case let .history(messages, issues):
            #expect(issues.isEmpty)
            batches.append(messages.map(\.seq))
        case let .message(message): live.append(message.seq)
        case .connection(false): catchUpFailed = true
        case .connection(true), .unauthorized: break
        }
        if live == [6] || catchUpFailed { break }
    }

    // The newest useful content must become observable after the first
    // request, without any automatic older-page fetch shifting the viewport.
    // The live stream still starts at the snapshot cursor captured at max_seq.
    #expect(batches == [[4, 5]])
    #expect(live == [6])
    #expect(queries == ["limit=50"])
    #expect(connector.requests.first?.url?.query == "after_seq=5")
}

@Test func coldCatchUpResumesFromPartialProgressAfterATransientFailure() async throws {
    // Issue #79: the newest chunk lands, then the link drops on the chunk
    // below it. The retry must continue from the retained cursor instead of
    // re-fetching the whole cold window from zero — otherwise a link that
    // cannot carry the window in one go never converges and the session sits
    // on "Reconnecting…" forever.
    nonisolated(unsafe) var queries: [String] = []
    nonisolated(unsafe) var olderAttempts = 0
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        if query == "limit=50" {
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 4, text: "four")), \(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
        olderAttempts += 1
        if olderAttempts == 1 {
            return (500, Data(#"{"error": "transient"}"#.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 5,
         "has_older": false, "has_newer": true}
        """.utf8))
    }
    let connector = FakeConnector([
        .frames([wireMessage(seq: 6, text: "six")], thenError: false)
    ])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var batches: [[Int]] = []
    var live: [Int] = []
    var drops = 0
    for await event in await stream.events() {
        switch event {
        case let .history(messages, _): batches.append(messages.map(\.seq))
        case let .message(message): live.append(message.seq)
        case .connection(false): drops += 1
        case .connection(true), .unauthorized: break
        }
        // One drop is the scripted failure; more means the stream is
        // restarting the cold window instead of resuming it.
        if live == [6] || drops > 1 { break }
    }

    // Exactly one snapshot-establishing request: the failed chunk is retried
    // on its own, and the window is still published as a single batch.
    #expect(queries == ["limit=50", "before_seq=4&limit=50", "before_seq=4&limit=50"])
    #expect(batches == [[1, 2, 3, 4, 5]])
    #expect(connector.requests.first?.url?.query == "after_seq=5")
}

@Test func coldCatchUpDiscardsARetainedWindowWhenAPageIsInconsistent() async throws {
    // Retaining progress must not wedge the stream on a page the snapshot
    // can never satisfy: a dropped request is resumable, a *malformed* one is
    // not, so the window is thrown away and the snapshot re-established.
    nonisolated(unsafe) var queries: [String] = []
    nonisolated(unsafe) var olderAttempts = 0
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        if query == "limit=50" {
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 4, text: "four")), \(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
        olderAttempts += 1
        if olderAttempts == 1 {
            // Does not abut seq 4 — a gap, not a transient failure.
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two"))],
             "page_min_seq": 1, "page_max_seq": 2, "max_seq": 5,
             "has_older": false, "has_newer": true}
            """.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 5,
         "has_older": false, "has_newer": true}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var batches: [[Int]] = []
    var drops = 0
    for await event in await stream.events() {
        switch event {
        case let .history(messages, _): batches.append(messages.map(\.seq))
        case .connection(false): drops += 1
        case .message, .connection(true), .unauthorized: break
        }
        if !batches.isEmpty || drops > 1 { break }
    }

    #expect(batches == [[1, 2, 3, 4, 5]])
    #expect(queries == [
        "limit=50", "before_seq=4&limit=50", "limit=50", "before_seq=4&limit=50",
    ])
}

@Test func coldCatchUpRendersAroundAPermanentGapAfterBoundedRetries() async throws {
    // Issue #99: the hub's copy of the stream is permanently missing events
    // harnessd recorded. Contiguity is the right invariant while the hole
    // might still be filled, but it is the wrong answer to one that never
    // will be — the window is discarded, the loop re-establishes, and it
    // fails identically forever, so a single lost event costs the ENTIRE
    // session's history and surfaces no error. Once the gap has outlived its
    // retries, render what is there and mark the hole.
    nonisolated(unsafe) var attempts = 0
    MockURLProtocol.handler = { _ in
        attempts += 1
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")),
                      \(wireMessage(seq: 5, text: "five"))],
         "page_min_seq": 1, "page_max_seq": 5, "max_seq": 5,
         "has_older": false, "has_newer": false}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var batch: [HarnessMessage] = []
    var drops = 0
    for await event in await stream.events() {
        switch event {
        case let .history(messages, _): batch = messages
        case .connection(false): drops += 1
        case .message, .connection(true), .unauthorized: break
        }
        // The old behaviour never yields history at all, so the drop ceiling
        // is what ends this loop when the fix is absent.
        if !batch.isEmpty || drops > MessageStream.gapRetryLimit + 2 { break }
    }

    // The hole (3 and 4) becomes one marker row carrying its own sequence.
    #expect(batch.map(\.seq) == [1, 2, 3, 5])
    #expect(batch.map(\.type) == [.assistantOutput, .assistantOutput,
                                  .transcriptGap, .assistantOutput])
    #expect(batch.map(\.text) == ["one", "two",
                                  "2 messages are missing from this transcript",
                                  "five"])
    // Degrading is a last resort, not the first response: the contiguous
    // retries all ran first.
    #expect(drops == MessageStream.gapRetryLimit)
    #expect(attempts == MessageStream.gapRetryLimit + 1)
    #expect(connector.requests.first?.url?.query == "after_seq=5")
}

@Test func coldCatchUpDoesNotDegradeOnAGapThatHeals() async throws {
    // A gap the hub is merely late on must not leave a marker in the
    // transcript: the retries exist precisely so a transient hole heals.
    nonisolated(unsafe) var attempts = 0
    MockURLProtocol.handler = { _ in
        attempts += 1
        if attempts == 1 {
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 3, text: "three"))],
             "page_min_seq": 1, "page_max_seq": 3, "max_seq": 3,
             "has_older": false, "has_newer": false}
            """.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")),
                      \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 3,
         "has_older": false, "has_newer": false}
        """.utf8))
    }
    let stream = MessageStream(
        client: client(), sessionID: "s1",
        connector: FakeConnector([.frames([], thenError: false)]),
        reconnectBaseDelay: .milliseconds(10)
    )

    var batch: [HarnessMessage] = []
    for await event in await stream.events() {
        if case let .history(messages, _) = event { batch = messages; break }
    }

    #expect(batch.map(\.seq) == [1, 2, 3])
    #expect(!batch.contains(where: { $0.type == .transcriptGap }))
    #expect(attempts == 2)
}

@Test func forwardCatchUpRendersAroundAPermanentGapAfterBoundedRetries() async throws {
    // The same treatment on the reconnect path: a live socket frame that
    // jumps the sequence sends the stream back through REST, and if REST
    // cannot produce the missing event either, the session must keep
    // updating rather than freeze on the last thing it managed to render.
    nonisolated(unsafe) var attempts = 0
    MockURLProtocol.handler = { _ in
        attempts += 1
        if attempts == 1 {
            return (200, Data(#"{"messages": [], "max_seq": 0, "has_older": false, "has_newer": false}"#.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 2, text: "two"))],
         "page_min_seq": 2, "page_max_seq": 2, "max_seq": 2,
         "has_older": true, "has_newer": false}
        """.utf8))
    }
    let connector = FakeConnector([
        .frames([wireMessage(seq: 2, text: "two")], thenError: true)
    ])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var batch: [HarnessMessage] = []
    var drops = 0
    for await event in await stream.events() {
        switch event {
        case let .history(messages, _) where !messages.isEmpty: batch = messages
        case .connection(false): drops += 1
        case .history, .message, .connection(true), .unauthorized: break
        }
        if !batch.isEmpty || drops > MessageStream.gapRetryLimit + 2 { break }
    }

    #expect(batch.map(\.seq) == [1, 2])
    #expect(batch.map(\.type) == [.transcriptGap, .assistantOutput])
    #expect(batch.map(\.text) == ["1 message is missing from this transcript",
                                  "two"])
}

@Test func coldCatchUpAssemblesItsWindowInBoundedChunks() async throws {
    // The cold window is unchanged at 200 messages, but no single request may
    // carry more than one page of it.
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        queries.append(request.url?.query ?? "")
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
            .queryItems ?? []
        func value(_ name: String) -> Int? {
            Int(items.first { $0.name == name }?.value ?? "")
        }
        let upper = (value("before_seq") ?? 261) - 1
        let lower = max(1, upper - (value("limit") ?? 0) + 1)
        let body = (lower...upper)
            .map { wireMessage(seq: $0, text: "m\($0)") }
            .joined(separator: ", ")
        return (200, Data("""
        {"messages": [\(body)],
         "page_min_seq": \(lower), "page_max_seq": \(upper), "max_seq": 260,
         "has_older": \(lower > 1), "has_newer": \(upper < 260)}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var batches: [[Int]] = []
    for await event in await stream.events() {
        if case let .history(messages, _) = event { batches.append(messages.map(\.seq)) }
        if event == .connection(true) || event == .connection(false) { break }
    }

    #expect(queries == [
        "limit=50",
        "before_seq=211&limit=50",
        "before_seq=161&limit=50",
        "before_seq=111&limit=50",
    ])
    #expect(batches == [Array(61...260)])
    #expect(await stream.olderHistoryAvailable())
    #expect(connector.requests.first?.url?.query == "after_seq=260")
}

@Test func olderHistoryLoadsOnePageOnlyWhenRequested() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        if query == "limit=50" {
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 4, text: "four")), \(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 6,
         "has_older": false, "has_newer": true}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10), coldWindowSize: 2
    )

    var iterator = await stream.events().makeAsyncIterator()
    while let event = await iterator.next() {
        if event == .connection(true) { break }
    }

    #expect(queries == ["limit=50"])
    let older = try await stream.loadOlderHistory()
    #expect(older?.messages.map(\.seq) == [1, 2, 3])
    #expect(older?.hasOlder == false)
    #expect(queries == ["limit=50", "before_seq=4&limit=50"])
    #expect(try await stream.loadOlderHistory() == nil)
    #expect(queries.count == 2)
}

@Test func coldCatchUpRejectsHistoryThatDoesNotReachSequenceOne() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        queries.append(request.url?.query ?? "")
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 2, "page_max_seq": 3, "max_seq": 3,
         "has_older": false, "has_newer": false}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: true)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    for await event in await stream.events() {
        if event == .connection(false) { break }
    }

    #expect(queries.first == "limit=50")
    #expect(connector.requests.isEmpty)
}

@Test func failedOlderHistoryRequestKeepsItsCursorForRetry() async throws {
    nonisolated(unsafe) var queries: [String] = []
    nonisolated(unsafe) var olderAttempts = 0
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        if query == "limit=50" {
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 4, text: "four")), \(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 4, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
        olderAttempts += 1
        if olderAttempts == 1 {
            return (500, Data(#"{"error": "transient"}"#.utf8))
        }
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 6,
         "has_older": false, "has_newer": true}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10), coldWindowSize: 2
    )

    let events = await stream.events()
    let consumeTask = Task {
        for await _ in events {
            if Task.isCancelled { break }
        }
    }
    while !(await stream.olderHistoryAvailable()) {
        await Task.yield()
    }

    do {
        _ = try await stream.loadOlderHistory()
        Issue.record("expected the first older-page request to fail")
    } catch {
        // The same cursor must remain available for a later explicit retry.
    }
    let retry = try await stream.loadOlderHistory()
    consumeTask.cancel()
    await stream.stop()

    #expect(retry?.messages.map(\.seq) == [1, 2, 3])
    #expect(queries.first == "limit=50")
    #expect(queries.filter { $0 == "before_seq=4&limit=50" }.count == 2)
}

@Test func catchUpGapRetriesFromLastContiguousSequenceWithoutWebSocket() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        queries.append(request.url?.query ?? "")
        return (200, Data("""
        {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 3,
         "has_older": false, "has_newer": false}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    for await event in await stream.events() {
        if event == .connection(false) { break }
    }

    #expect(queries.first == "limit=50")
    #expect(connector.requests.isEmpty)
}

@Test func malformedHistoryElementDoesNotAdvanceCursorOrAttachWebSocket() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        queries.append(request.url?.query ?? "")
        return (200, Data("""
        {"messages": [
          \(wireMessage(seq: 1, text: "one")),
          {"seq": 2, "type": "assistant_output", "role": "assistant", "text": "bad"},
          \(wireMessage(seq: 3, text: "three"))],
         "page_min_seq": 1, "page_max_seq": 3, "max_seq": 3,
         "has_older": false, "has_newer": false}
        """.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    for await event in await stream.events() {
        if event == .connection(false) { break }
    }

    #expect(queries.first == "limit=50")
    #expect(connector.requests.isEmpty)
}

@Test func stopCancelsTheAttachedWebSocketStream() async throws {
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"messages": [], "max_seq": 0, "has_older": false, "has_newer": false}"#.utf8))
    }
    let connector = FakeConnector([.frames([], thenError: false)])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    for await event in await stream.events() {
        if event == .connection(true) {
            await stream.stop()
            break
        }
    }
    try await Task.sleep(for: .milliseconds(10))

    #expect(connector.terminationCount == 1)
}

@Test func liveSequenceGapReconnectsWithoutAdvancingCursor() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        queries.append(request.url?.query ?? "")
        return (200, Data(#"{"messages": [], "max_seq": 0, "has_older": false, "has_newer": false}"#.utf8))
    }
    let connector = FakeConnector([
        .frames([wireMessage(seq: 2, text: "gap")], thenError: false)
    ])
    let stream = MessageStream(
        client: client(), sessionID: "s1", connector: connector,
        reconnectBaseDelay: .milliseconds(10)
    )

    var delivered: [Int] = []
    for await event in await stream.events() {
        switch event {
        case let .message(message): delivered.append(message.seq)
        case .connection(false): break
        case .history, .connection(true), .unauthorized: continue
        }
        break
    }

    #expect(delivered.isEmpty)
    #expect(queries.first == "limit=50")
}

}

}  // extension MockNetworkTests
