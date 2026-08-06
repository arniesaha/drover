import Foundation

// MARK: - WebSocketConnecting

/// Seam for tests: something that can open a WebSocket connection and hand
/// back its frames (text or converted-to-text data) as an async stream that
/// throws when the connection drops.
public protocol WebSocketConnecting: Sendable {
    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error>
}

// MARK: - URLSessionWebSocketConnector

/// Wraps a real `URLSessionWebSocketTask`'s receive loop into the
/// `WebSocketConnecting` seam.
public struct URLSessionWebSocketConnector: WebSocketConnecting {
    public init() {}

    public func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let session = URLSession(configuration: .default)
            let task = session.webSocketTask(with: request)
            task.resume()

            let pump = Task {
                do {
                    while !Task.isCancelled {
                        let message = try await task.receive()
                        switch message {
                        case let .string(text):
                            continuation.yield(text)
                        case let .data(data):
                            if let text = String(data: data, encoding: .utf8) {
                                continuation.yield(text)
                            }
                        @unknown default:
                            break
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }

            continuation.onTermination = { _ in
                pump.cancel()
                task.cancel(with: .goingAway, reason: nil)
            }
        }
    }
}

// MARK: - StreamEvent

public enum StreamEvent: Sendable, Equatable {
    case message(HarnessMessage)
    case connection(Bool)   // false while reconnecting
    /// Terminal: the token was rejected (401) by either the REST catch-up or
    /// the WebSocket handshake. Unlike a transient drop, this is never
    /// recoverable by retrying with the same token, so the pump stops
    /// permanently after emitting this once — no more `.connection(false)`
    /// reconnect cycling. The consumer (`ChatModel`) should surface a
    /// "check Settings" hint rather than spinning forever.
    case unauthorized
}

// MARK: - MessageStream

/// Resumable message stream for a single harness session: replays REST
/// history from the last-seen sequence number, then live WebSocket frames,
/// deduped and delivered strictly in ascending `seq` order. On WebSocket
/// failure it reconnects with doubling backoff (capped at 30s), always
/// catching up via REST from `lastSeq` first so no message is missed or
/// re-delivered.
public actor MessageStream {
    private let client: DroverClient
    private let sessionID: String
    private let connector: WebSocketConnecting
    private let reconnectBaseDelay: Duration

    private var lastSeq = 0
    private var pumpTask: Task<Void, Never>?

    public init(
        client: DroverClient,
        sessionID: String,
        connector: WebSocketConnecting = URLSessionWebSocketConnector(),
        reconnectBaseDelay: Duration = .seconds(1)
    ) {
        self.client = client
        self.sessionID = sessionID
        self.connector = connector
        self.reconnectBaseDelay = reconnectBaseDelay
    }

    /// Single-consumer: each `MessageStream` supports one `events()` stream
    /// at a time. Termination is driven by `stop()` or the consumer breaking
    /// out of its `for await` loop (which fires `onTermination`). Calling
    /// `events()` again cancels any previous pump rather than leaking it.
    public func events() -> AsyncStream<StreamEvent> {
        pumpTask?.cancel()
        return AsyncStream { continuation in
            let task = Task {
                await self.run(continuation: continuation)
            }
            self.pumpTask = task
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    public func stop() {
        pumpTask?.cancel()
        pumpTask = nil
    }

    // MARK: - Pump

    private func run(continuation: AsyncStream<StreamEvent>.Continuation) async {
        var backoff = reconnectBaseDelay
        var firstAttempt = true

        while !Task.isCancelled {
            if !firstAttempt {
                continuation.yield(.connection(false))
                try? await Task.sleep(for: backoff)
                backoff = min(backoff * 2, .seconds(30))
            }
            firstAttempt = false

            // REST catch-up failure is a connection failure: emit
            // .connection(false), back off, and retry the whole loop (which
            // re-runs catch-up) — never proceed to WS with a history gap.
            // A 401 is the one exception: the token is rejected, retrying
            // with the same credentials can never succeed, so stop for good
            // instead of reconnecting forever behind a silent "Reconnecting…"
            // pill.
            let batch: MessageBatch
            do {
                batch = try await client.messages(sessionID: sessionID, afterSeq: lastSeq)
            } catch DroverError.unauthorized {
                if !Task.isCancelled { continuation.yield(.unauthorized) }
                break
            } catch {
                if Task.isCancelled { break }
                continue
            }
            if Task.isCancelled { break }

            // Catch-up succeeded: the connection is proven healthy. Tell
            // consumers before delivering the caught-up messages (they start
            // in a known state on the first pass and clear any reconnecting
            // indicator on later passes), and reset the backoff so an early
            // transient drop doesn't permanently degrade reconnect latency.
            continuation.yield(.connection(true))
            backoff = reconnectBaseDelay

            for message in batch.messages.sorted(by: { $0.seq < $1.seq }) {
                deliver(message, continuation: continuation)
            }

            let request = client.streamRequest(sessionID: sessionID, afterSeq: lastSeq)
            let frames = connector.connect(request)

            do {
                for try await frame in frames {
                    if Task.isCancelled { break }
                    guard let message = decode(frame: frame) else { continue }
                    deliver(message, continuation: continuation)
                }
                // Stream finished without error: server closed cleanly.
                // Treat as a disconnect and reconnect, same as an error.
                if Task.isCancelled { break }
            } catch DroverError.unauthorized {
                // Same terminal treatment as a 401 on REST catch-up, in case
                // a connector surfaces the WS handshake's rejection as a
                // typed DroverError rather than a generic transport error.
                if !Task.isCancelled { continuation.yield(.unauthorized) }
                break
            } catch {
                if Task.isCancelled { break }
                // fall through to reconnect loop
            }
        }

        continuation.finish()
    }

    /// Dedup relies on the server contract that `seq` is strictly monotonic
    /// per session: anything at or below `lastSeq` has, by definition, already
    /// been delivered (via REST history or an earlier frame). If the server
    /// ever regressed a session's seq counter, those messages would be
    /// silently dropped here — by design, since re-delivering would duplicate.
    private func deliver(_ message: HarnessMessage, continuation: AsyncStream<StreamEvent>.Continuation) {
        guard message.seq > lastSeq else { return }
        lastSeq = message.seq
        continuation.yield(.message(message))
    }

    private func decode(frame: String) -> HarnessMessage? {
        guard let data = frame.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(HarnessMessage.self, from: data)
    }
}
