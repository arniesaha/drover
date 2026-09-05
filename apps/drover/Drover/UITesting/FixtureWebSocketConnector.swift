import Foundation
import DroverKit

#if DEBUG
/// A receive-only stream that deliberately never creates a socket. History is
/// supplied by `FixtureHubURLProtocol`; keeping this stream open makes the
/// real `MessageStream` report a connected chat without live network traffic.
struct FixtureWebSocketConnector: WebSocketConnecting {
    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            guard request.url?.host == "fixture.drover.invalid",
                  request.url?.scheme == "wss" else {
                continuation.finish(throwing: URLError(.unsupportedURL))
                return
            }
            continuation.onTermination = { _ in }
        }
    }
}
#endif
