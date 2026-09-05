import Foundation
import Testing
@testable import DroverKit

/// The test mutates the process-global `MockURLProtocol.handler`, so it stays
/// under the root serialized suite shared by the other network tests.
extension MockNetworkTests {
@Suite(.serialized)
struct SessionLoadPerformanceTests {
    @Test @MainActor func productionShapedTranscriptMergesInBoundedPages() {
        let messages = productionShapedMessages(count: 3_316)
        let pages = messages.chunked(maxCount: 200)
        let model = ChatModel.fixture()
        let startVersion = model.messagesVersion
        let clock = ContinuousClock()

        let duration = clock.measure {
            for page in pages {
                model.ingest(.history(page, decodeIssues: []))
            }
        }

        #expect(model.messages.count == 3_316)
        #expect(model.messagesVersion == startVersion + pages.count)
        #expect(model.historyPagesMerged == pages.count)
        print("Merged 3,316 production-shaped messages in \(pages.count) pages: \(duration)")
    }

    @Test(.timeLimit(.minutes(1))) func productionShapedColdOpenLoadsOnlyTheTailAndOneExplicitOlderPage() async throws {
        let transcript = ScriptedTranscript(count: 3_316)
        let requests = MessagePageRequestLog()
        let connector = HoldingConnector()

        MockURLProtocol.handler = { request in
            transcript.respond(to: request, recordingIn: requests)
        }
        defer {
            MockURLProtocol.handler = nil
            connector.finish()
        }

        let stream = MessageStream(
            client: client(),
            sessionID: "production-shaped-session",
            connector: connector,
            reconnectBaseDelay: .milliseconds(10)
        )
        let events = await stream.events()
        var iterator = events.makeAsyncIterator()
        var coldBatches: [[Int]] = []

        eventLoop: while let event = await iterator.next() {
            switch event {
            case let .history(messages, issues):
                #expect(issues.isEmpty)
                coldBatches.append(messages.map(\.seq))
            case .connection(true):
                break eventLoop
            case .message, .connection(false), .connectFailed, .unauthorized:
                break
            }
        }

        #expect(coldBatches == [Array(3_117...3_316)])
        #expect(requests.records == [
            .init(query: "limit=50", returned: 3_267...3_316),
            .init(query: "before_seq=3267&limit=50", returned: 3_217...3_266),
            .init(query: "before_seq=3217&limit=50", returned: 3_167...3_216),
            .init(query: "before_seq=3167&limit=50", returned: 3_117...3_166),
        ])

        let older = try await stream.loadOlderHistory()
        #expect(older?.messages.map(\.seq) == Array(3_067...3_116))
        #expect(older?.hasOlder == true)
        #expect(requests.records == [
            .init(query: "limit=50", returned: 3_267...3_316),
            .init(query: "before_seq=3267&limit=50", returned: 3_217...3_266),
            .init(query: "before_seq=3217&limit=50", returned: 3_167...3_216),
            .init(query: "before_seq=3167&limit=50", returned: 3_117...3_166),
            .init(query: "before_seq=3117&limit=50", returned: 3_067...3_116),
        ])

        let requestsBeforeLiveUpdate = requests.records
        connector.send(transcript.liveUpdate(seq: 3_317))

        var liveSequences: [Int] = []
        while let event = await iterator.next() {
            if case let .message(message) = event {
                liveSequences.append(message.seq)
                break
            }
        }
        await stream.stop()

        #expect(liveSequences == [3_317])
        #expect(requests.records == requestsBeforeLiveUpdate)
    }
}
}

private struct ScriptedTranscript: Sendable {
    let count: Int

    func respond(to request: URLRequest, recordingIn log: MessagePageRequestLog) -> (Int, Data) {
        guard request.httpMethod == "GET",
              request.url?.path == "/harness/sessions/production-shaped-session/messages",
              let components = request.url.flatMap({ URLComponents(url: $0, resolvingAgainstBaseURL: false) })
        else {
            return (404, Data(#"{"error":"unexpected request"}"#.utf8))
        }

        let values = Dictionary(
            uniqueKeysWithValues: (components.queryItems ?? []).compactMap { item in
                item.value.map { (item.name, $0) }
            }
        )
        let limit = Int(values["limit"] ?? "") ?? 0
        let first: Int
        let last: Int
        let hasOlder: Bool
        let hasNewer: Bool

        if let beforeSeq = Int(values["before_seq"] ?? "") {
            last = beforeSeq - 1
            first = max(1, last - limit + 1)
            hasOlder = first > 1
            hasNewer = true
        } else if let afterSeq = Int(values["after_seq"] ?? "") {
            first = afterSeq + 1
            last = min(count, afterSeq + limit)
            hasOlder = first > 1
            hasNewer = last < count
        } else {
            last = count
            first = max(1, count - limit + 1)
            hasOlder = first > 1
            hasNewer = false
        }

        let returned = first...last
        log.append(.init(query: components.percentEncodedQuery ?? "", returned: returned))
        return (200, messagePage(
            returned,
            maxSeq: count,
            hasOlder: hasOlder,
            hasNewer: hasNewer
        ))
    }

    func liveUpdate(seq: Int) -> String {
        assistantOutputWireMessage(seq: seq, text: "live update")
    }

    private func messagePage(
        _ range: ClosedRange<Int>,
        maxSeq: Int,
        hasOlder: Bool,
        hasNewer: Bool
    ) -> Data {
        let messages = range.map(productionWireMessage).joined(separator: ",")
        return Data("""
        {"messages":[\(messages)],"page_min_seq":\(range.lowerBound),
         "page_max_seq":\(range.upperBound),"max_seq":\(maxSeq),
         "has_older":\(hasOlder),"has_newer":\(hasNewer)}
        """.utf8)
    }

    private func productionWireMessage(seq: Int) -> String {
        switch seq % 4 {
        case 0:
            return """
            {"event_id":"event-\(seq)","seq":\(seq),"type":"status","role":"system",
             "text":"Working through session history page \(seq)","payload":{"awaiting":"input"}}
            """.replacingOccurrences(of: "\n", with: "")
        case 1:
            return assistantOutputWireMessage(
                seq: seq, text: "Representative prose for bounded cold-open coverage."
            )
        case 2:
            return """
            {"event_id":"event-\(seq)","seq":\(seq),"type":"tool_action","role":"assistant",
             "text":"Bash","payload":{"tool":"Bash","tool_use_id":"tool-\(seq)",
             "input":{"command":"git status --short"}}}
            """.replacingOccurrences(of: "\n", with: "")
        default:
            return """
            {"event_id":"event-\(seq)","seq":\(seq),"type":"assistant_output","role":"assistant",
             "text":"thinking thinking thinking","payload":{"thinking":true}}
            """.replacingOccurrences(of: "\n", with: "")
        }
    }

    private func assistantOutputWireMessage(seq: Int, text: String) -> String {
        #"{"event_id":"event-\#(seq)","seq":\#(seq),"type":"assistant_output","role":"assistant","text":"\#(text)","payload":{}}"#
    }
}

@MainActor
private func productionShapedMessages(count: Int) -> [HarnessMessage] {
    (1...count).map { seq in
        let type: MessageType
        let text: String
        let payload: [String: JSONValue]
        switch seq % 4 {
        case 0:
            type = .status
            text = "Working through session history page \(seq)"
            payload = ["awaiting": .string("input")]
        case 1:
            type = .assistantOutput
            text = String(repeating: "Representative prose for cold-load measurement. ", count: 8)
            payload = [:]
        case 2:
            type = .toolAction
            text = "Bash"
            payload = [
                "tool": .string("Bash"),
                "tool_use_id": .string("tool-\(seq)"),
                "input": .object(["command": .string("git status --short")]),
            ]
        default:
            type = .assistantOutput
            text = String(repeating: "thinking ", count: 32)
            payload = ["thinking": .bool(true)]
        }
        return HarnessMessage.fixture(seq: seq, type: type, text: text, payload: payload)
    }
}

private extension Array {
    func chunked(maxCount: Int) -> [[Element]] {
        stride(from: 0, to: count, by: maxCount).map { start in
            Array(self[start..<Swift.min(start + maxCount, count)])
        }
    }
}

private struct RecordedMessagePageRequest: Equatable {
    let query: String
    let returned: ClosedRange<Int>
}

private final class MessagePageRequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [RecordedMessagePageRequest] = []

    var records: [RecordedMessagePageRequest] {
        lock.withLock { values }
    }

    func append(_ record: RecordedMessagePageRequest) {
        lock.withLock { values.append(record) }
    }
}

private final class HoldingConnector: WebSocketConnecting, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<String, Error>.Continuation?

    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            lock.withLock { self.continuation = continuation }
        }
    }

    func send(_ frame: String) {
        let continuation = lock.withLock { self.continuation }
        continuation?.yield(frame)
    }

    func finish() {
        let continuation = lock.withLock { () -> AsyncThrowingStream<String, Error>.Continuation? in
            defer { self.continuation = nil }
            return self.continuation
        }
        continuation?.finish()
    }
}
