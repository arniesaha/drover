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
    case history([HarnessMessage], decodeIssues: [MessageDecodeIssue])
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
    private enum CatchUpError: Error {
        case sequenceGap
        case malformedPage
        case snapshotChanged
    }

    private static let historyPageSize = 200
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
            let fixedMaxSeq: Int
            do {
                fixedMaxSeq = try await catchUp(continuation: continuation)
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

            let request = client.streamRequest(
                sessionID: sessionID, afterSeq: fixedMaxSeq
            )
            let frames = connector.connect(request)

            do {
                for try await frame in frames {
                    if Task.isCancelled { break }
                    guard let message = decode(frame: frame) else { continue }
                    try deliver(message, continuation: continuation)
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

    private func catchUp(
        continuation: AsyncStream<StreamEvent>.Continuation
    ) async throws -> Int {
        var cursor = lastSeq
        var fixedMaxSeq: Int?

        while !Task.isCancelled {
            let page = try await client.messagePage(
                sessionID: sessionID,
                request: .newer(
                    afterSeq: cursor,
                    throughSeq: fixedMaxSeq,
                    limit: Self.historyPageSize
                )
            )
            try Task.checkCancellation()

            if let fixedMaxSeq {
                guard page.maxSeq == fixedMaxSeq else {
                    throw CatchUpError.snapshotChanged
                }
            } else {
                guard page.maxSeq >= cursor else {
                    throw CatchUpError.snapshotChanged
                }
                fixedMaxSeq = page.maxSeq
            }
            guard page.decodeIssues.isEmpty else {
                throw CatchUpError.malformedPage
            }

            let fresh = page.messages.filter { $0.seq > cursor }
            var expectedSeq = cursor + 1
            for message in fresh {
                guard message.seq == expectedSeq else {
                    throw CatchUpError.sequenceGap
                }
                expectedSeq += 1
            }

            if !fresh.isEmpty {
                cursor = fresh.last!.seq
                lastSeq = cursor
                continuation.yield(.history(fresh, decodeIssues: page.decodeIssues))
            }

            guard let bound = fixedMaxSeq else {
                throw CatchUpError.snapshotChanged
            }
            if cursor == bound {
                return bound
            }
            guard cursor < bound, !fresh.isEmpty, page.hasNewer else {
                throw CatchUpError.sequenceGap
            }
        }

        throw CancellationError()
    }

    /// Duplicate replay at or below `lastSeq` is harmless. A jump above the
    /// next contiguous sequence is not: reconnect through REST from the last
    /// safe cursor rather than advancing past a message the client never saw.
    private func deliver(
        _ message: HarnessMessage,
        continuation: AsyncStream<StreamEvent>.Continuation
    ) throws {
        guard message.seq > lastSeq else { return }
        guard message.seq == lastSeq + 1 else {
            throw CatchUpError.sequenceGap
        }
        lastSeq = message.seq
        continuation.yield(.message(message))
    }

    private func decode(frame: String) -> HarnessMessage? {
        guard let data = frame.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(HarnessMessage.self, from: data)
    }
}
