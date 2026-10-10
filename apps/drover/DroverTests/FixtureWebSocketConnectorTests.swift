import Foundation
import Testing
@testable import Drover

#if DEBUG
@MainActor
struct FixtureWebSocketConnectorTests {
    @Test func longStreamingModeWinsAndResumesAfterRequestedSequence() async throws {
        let connector = FixtureWebSocketConnector(streamsLongTranscript: true, streamsReadingTranscript: true)
        let frame = try #require(try await firstFrame(connector, sessionID: FixtureScenarioData.primarySessionID, after: 282))
        let event = try #require(JSONSerialization.jsonObject(with: Data(frame.utf8)) as? [String: Any])
        #expect(event["seq"] as? Int == 283)
        #expect(event["event_id"] as? String == "sample-stream-283")
        #expect((event["text"] as? String)?.contains("chunk 2 of 80") == true)
    }

    @Test func readingMarkerModeStillProducesItsOwnStream() async throws {
        let connector = FixtureWebSocketConnector(streamsLongTranscript: false, streamsReadingTranscript: true)
        let frame = try #require(try await firstFrame(connector, sessionID: FixtureScenarioData.primarySessionID, after: 40))
        let event = try #require(JSONSerialization.jsonObject(with: Data(frame.utf8)) as? [String: Any])
        #expect(event["seq"] as? Int == 41)
        #expect(event["event_id"] as? String == "reading-41")
        #expect(event["text"] as? String == "Reading marker 41. New transcript content.")
    }

    @Test func longStreamingModeNeverFallsBackForAnotherSession() async throws {
        let connector = FixtureWebSocketConnector(streamsLongTranscript: true, streamsReadingTranscript: true)
        let frame = try await firstFrame(connector, sessionID: FixtureScenarioData.labSessionID,
                                        after: 0, timeout: .milliseconds(1500))
        #expect(frame == nil)
    }

    private func firstFrame(_ connector: FixtureWebSocketConnector, sessionID: String,
                            after: Int, timeout: Duration = .seconds(3)) async throws -> String? {
        let url = try #require(URL(string: "wss://fixture.drover.invalid/harness/sessions/\(sessionID)/stream?after_seq=\(after)"))
        let request = URLRequest(url: url)
        return try await withThrowingTaskGroup(of: String?.self) { group in
            group.addTask {
                var iterator = connector.connect(request).makeAsyncIterator()
                return try await iterator.next()
            }
            group.addTask {
                try await Task.sleep(for: timeout)
                return nil
            }
            defer { group.cancelAll() }
            return try await group.next() ?? nil
        }
    }
}
#endif
