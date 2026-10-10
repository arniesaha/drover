import Foundation
import DroverKit

#if DEBUG
/// A receive-only stream that deliberately never creates a socket. History is
/// supplied by `FixtureHubURLProtocol`; keeping this stream open makes the
/// real `MessageStream` report a connected chat without live network traffic.
struct FixtureWebSocketConnector: WebSocketConnecting {
    var streamsLongTranscript = false

    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            guard request.url?.host == "fixture.drover.invalid",
                  request.url?.scheme == "wss" else {
                continuation.finish(throwing: URLError(.unsupportedURL))
                return
            }
            guard streamsLongTranscript,
                  request.url?.path.contains(FixtureScenarioData.primarySessionID) == true else {
                continuation.onTermination = { _ in }
                return
            }
            let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "after_seq" }?.value.flatMap(Int.init) ?? 0
            let pump = Task {
                do {
                    let events = LongStreamingTranscriptFixture.streamingEvents
                    for event in events {
                        try await Task.sleep(for: LongStreamingTranscriptFixture.chunkInterval)
                        if (event["seq"] as! Int) > after {
                            continuation.yield(LongStreamingTranscriptFixture.frame(event))
                        }
                    }
                    // Keep the synthetic connection open after the last chunk.
                } catch { continuation.finish() }
            }
            continuation.onTermination = { _ in pump.cancel() }
        }
    }
}
#endif
