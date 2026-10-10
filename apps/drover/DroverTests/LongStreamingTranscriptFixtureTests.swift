import Foundation
import XCTest
import DroverKit
@testable import Drover

#if DEBUG
@MainActor
final class LongStreamingTranscriptFixtureTests: XCTestCase {
    func testFixturePagesAndStreamsThroughRealMessageStream() async throws {
        let chunks = LongStreamingTranscriptFixture.streamingEvents
        XCTAssertEqual(chunks.count, 80)
        XCTAssertGreaterThan((chunks.last?["text"] as? String)?.count ?? 0, 19_000)
        XCTAssertEqual(chunks.last?["seq"] as? Int, 361)
        let scenario = UITestScenarioTransport(environment: [
            "DROVER_UI_TEST_SCENARIO": "long-streaming",
            "DROVER_UI_TEST_RUN_ID": UUID().uuidString,
        ])!
        let stream = MessageStream(client: scenario.client, sessionID: FixtureScenarioData.primarySessionID,
                                   connector: FixtureWebSocketConnector(streamsLongTranscript: true))
        var history: [HarnessMessage] = []
        var live: [HarnessMessage] = []
        for await event in await stream.events() {
            switch event {
            case .history(let messages, let issues):
                XCTAssertTrue(issues.isEmpty)
                history.append(contentsOf: messages)
            case .message(let message):
                live.append(message)
                if live.count == 80 { await stream.stop(); break }
            default: break
            }
            if live.count == 80 { break }
        }
        XCTAssertEqual(history.count, 200)
        XCTAssertEqual(history.last?.seq, 281)
        XCTAssertEqual(history.last?.type, .approvalPrompt)
        XCTAssertEqual(live.map(\.seq), Array(282...361))
        XCTAssertGreaterThan(live.last?.text.count ?? 0, 19_000)
        XCTAssertTrue(live.allSatisfy { $0.type == .assistantOutput && $0.turnID == "sample-stream-turn" })
        let olderPage = try await stream.loadOlderHistory()
        let older = try XCTUnwrap(olderPage)
        XCTAssertEqual(older.messages.count, 50)
        XCTAssertTrue(older.hasOlder)
        XCTAssertTrue(TranscriptItem.group(history).contains {
            if case .stepRun(let steps) = $0 { return steps.count == 3 }
            return false
        })
    }
}
#endif
