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
        #expect(request.url!.query!.contains("after_seq=0"))
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

@Test func catchUpEmitsFixedBoundPagesBeforeLiveMessages() async throws {
    nonisolated(unsafe) var queries: [String] = []
    MockURLProtocol.handler = { request in
        let query = request.url?.query ?? ""
        queries.append(query)
        switch queries.count {
        case 1:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 1, text: "one")), \(wireMessage(seq: 2, text: "two"))],
             "page_min_seq": 1, "page_max_seq": 2, "max_seq": 5,
             "has_older": false, "has_newer": true}
            """.utf8))
        case 2:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 3, text: "three")), \(wireMessage(seq: 4, text: "four"))],
             "page_min_seq": 3, "page_max_seq": 4, "max_seq": 5,
             "has_older": true, "has_newer": true}
            """.utf8))
        default:
            return (200, Data("""
            {"messages": [\(wireMessage(seq: 5, text: "five"))],
             "page_min_seq": 5, "page_max_seq": 5, "max_seq": 5,
             "has_older": true, "has_newer": false}
            """.utf8))
        }
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
    for await event in await stream.events() {
        switch event {
        case let .history(messages, issues):
            #expect(issues.isEmpty)
            batches.append(messages.map(\.seq))
        case let .message(message): live.append(message.seq)
        case .connection, .unauthorized: break
        }
        if live == [6] { break }
    }

    #expect(batches == [[1, 2], [3, 4], [5]])
    #expect(live == [6])
    #expect(queries == [
        "after_seq=0&limit=200",
        "after_seq=2&through_seq=5&limit=200",
        "after_seq=4&through_seq=5&limit=200",
    ])
    #expect(connector.requests.first?.url?.query == "after_seq=5")
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

    #expect(queries.first == "after_seq=0&limit=200")
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

    #expect(queries.first == "after_seq=0&limit=200")
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
    #expect(queries.first == "after_seq=0&limit=200")
}

}

}  // extension MockNetworkTests
