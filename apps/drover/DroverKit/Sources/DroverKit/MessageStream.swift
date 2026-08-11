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

public struct OlderHistoryPage: Sendable, Equatable {
    public let messages: [HarnessMessage]
    public let hasOlder: Bool
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

    /// One REST request's worth of history. Deliberately small: it is the
    /// unit of work that a flaky link has to carry in one piece, and the unit
    /// of progress that survives a failure.
    public static let defaultPageSize = 50

    /// How much history a cold open assembles before attaching the socket.
    /// Unchanged from when it was fetched as a single page — only the number
    /// of requests it takes to gather changed.
    public static let defaultColdWindowSize = 200

    private let client: DroverClient
    private let sessionID: String
    private let connector: WebSocketConnecting
    private let reconnectBaseDelay: Duration
    private let pageSize: Int
    private let coldWindowSize: Int

    private var lastSeq = 0
    private var coldHistoryComplete = false
    private var olderBeforeSeq: Int?
    private var hasOlderHistory = false
    private var isLoadingOlderHistory = false
    private var coldSnapshotMaxSeq = 0
    private var pumpTask: Task<Void, Never>?

    /// Partially assembled cold window, retained across reconnect attempts.
    /// `coldWindow` is contiguous and ascending; `coldWindowMaxSeq` is the
    /// snapshot bound captured by the first page and the cursor the socket
    /// will attach at. Nil means no page of the window has landed yet.
    private var coldWindow: [HarnessMessage] = []
    private var coldWindowMaxSeq: Int?
    private var coldWindowHasOlder = false

    public init(
        client: DroverClient,
        sessionID: String,
        connector: WebSocketConnecting = URLSessionWebSocketConnector(),
        reconnectBaseDelay: Duration = .seconds(1),
        pageSize: Int = MessageStream.defaultPageSize,
        coldWindowSize: Int = MessageStream.defaultColdWindowSize
    ) {
        self.client = client
        self.sessionID = sessionID
        self.connector = connector
        self.reconnectBaseDelay = reconnectBaseDelay
        self.pageSize = max(1, pageSize)
        self.coldWindowSize = max(1, coldWindowSize)
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

    public func olderHistoryAvailable() -> Bool {
        hasOlderHistory
    }

    /// Fetches exactly one page above the currently loaded tail. The cursor
    /// advances only after a fully validated response, so a failed request is
    /// safely retryable and never makes older history disappear.
    public func loadOlderHistory() async throws -> OlderHistoryPage? {
        guard coldHistoryComplete, hasOlderHistory,
              let beforeSeq = olderBeforeSeq,
              !isLoadingOlderHistory else { return nil }
        isLoadingOlderHistory = true
        defer { isLoadingOlderHistory = false }

        let page = try await client.messagePage(
            sessionID: sessionID,
            request: .older(beforeSeq: beforeSeq, limit: pageSize)
        )
        try Task.checkCancellation()
        try validate(page: page)
        guard page.maxSeq >= coldSnapshotMaxSeq,
              let pageFirst = page.messages.first?.seq,
              page.messages.last?.seq == beforeSeq - 1,
              page.hasNewer else {
            throw CatchUpError.sequenceGap
        }

        let visible = page.messages.filter { $0.seq > 0 }
        guard page.hasOlder || visible.first?.seq == 1 else {
            throw CatchUpError.sequenceGap
        }
        olderBeforeSeq = pageFirst
        hasOlderHistory = page.hasOlder
        return OlderHistoryPage(messages: visible, hasOlder: page.hasOlder)
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
        if !coldHistoryComplete {
            return try await coldCatchUp(continuation: continuation)
        }
        return try await forwardCatchUp(continuation: continuation)
    }

    /// A cold open publishes only the newest bounded window, then attaches the
    /// live socket immediately. Older history beyond that window stays behind
    /// an explicit request so opening a session never shifts the viewport with
    /// automatic prepends.
    ///
    /// The window is assembled from `pageSize` chunks rather than one large
    /// page, and each landed chunk is *retained* on the actor. A dropped
    /// request therefore costs one chunk, not the whole window: the reconnect
    /// loop re-enters here and continues from the retained cursor. Restarting
    /// from zero every time was issue #79 — on a link that could not carry the
    /// whole page in one piece, the session never attached at all.
    ///
    /// A logically inconsistent page is different from a dropped one: it can
    /// never be resumed past, so it discards the retained window and the retry
    /// re-establishes the snapshot from scratch.
    private func coldCatchUp(
        continuation: AsyncStream<StreamEvent>.Continuation
    ) async throws -> Int {
        do {
            return try await assembleColdWindow(continuation: continuation)
        } catch let error as CatchUpError {
            discardColdWindow()
            throw error
        }
    }

    private func assembleColdWindow(
        continuation: AsyncStream<StreamEvent>.Continuation
    ) async throws -> Int {
        if coldWindowMaxSeq == nil {
            let newest = try await client.messagePage(
                sessionID: sessionID, request: .newest(limit: pageSize)
            )
            try Task.checkCancellation()
            try validate(page: newest)

            let fixedMaxSeq = newest.maxSeq
            guard fixedMaxSeq >= 0, !newest.hasNewer else {
                throw CatchUpError.snapshotChanged
            }
            if newest.messages.isEmpty {
                guard fixedMaxSeq == 0, !newest.hasOlder else {
                    throw CatchUpError.snapshotChanged
                }
                coldHistoryComplete = true
                return fixedMaxSeq
            }
            guard newest.messages.last?.seq == fixedMaxSeq else {
                throw CatchUpError.snapshotChanged
            }
            coldWindowMaxSeq = fixedMaxSeq
            coldWindow = newest.messages
            coldWindowHasOlder = newest.hasOlder
        }

        // Extend oldest-ward one chunk at a time. Every successful page is
        // committed to `coldWindow` before the next request is issued, so a
        // failure anywhere in here leaves real progress behind.
        while let fixedMaxSeq = coldWindowMaxSeq,
              coldWindowHasOlder,
              coldWindow.count < coldWindowSize,
              let beforeSeq = coldWindow.first?.seq {
            let page = try await client.messagePage(
                sessionID: sessionID,
                request: .older(
                    beforeSeq: beforeSeq,
                    limit: min(pageSize, coldWindowSize - coldWindow.count)
                )
            )
            try Task.checkCancellation()
            try validate(page: page)
            // The snapshot may have grown underneath us — that is fine, the
            // socket attaches at the bound captured above and replays the
            // rest. It must never have shrunk, and the page must abut what we
            // already hold.
            guard page.maxSeq >= fixedMaxSeq,
                  !page.messages.isEmpty,
                  page.messages.last?.seq == beforeSeq - 1,
                  page.hasNewer else {
                throw CatchUpError.sequenceGap
            }
            coldWindow = page.messages + coldWindow
            coldWindowHasOlder = page.hasOlder
        }

        guard let fixedMaxSeq = coldWindowMaxSeq else {
            throw CatchUpError.snapshotChanged
        }
        let visible = coldWindow.filter { $0.seq > 0 }
        guard coldWindowHasOlder || fixedMaxSeq == 0 || visible.first?.seq == 1 else {
            throw CatchUpError.sequenceGap
        }
        olderBeforeSeq = coldWindow.first?.seq
        hasOlderHistory = coldWindowHasOlder
        coldSnapshotMaxSeq = fixedMaxSeq
        lastSeq = fixedMaxSeq
        coldHistoryComplete = true
        // One batch for the whole window: assembling it in chunks must not
        // become visible as a series of prepends.
        if !visible.isEmpty {
            continuation.yield(.history(visible, decodeIssues: []))
        }
        discardColdWindow()
        return fixedMaxSeq
    }

    private func discardColdWindow() {
        coldWindow = []
        coldWindowMaxSeq = nil
        coldWindowHasOlder = false
    }

    /// Reconnects retain the forward, fixed-bound path: only events newer
    /// than the last delivered live sequence are eligible for replay.
    private func forwardCatchUp(
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
                    limit: pageSize
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

    private func validate(page: MessagePage) throws {
        guard page.decodeIssues.isEmpty else {
            throw CatchUpError.malformedPage
        }
        guard !page.messages.isEmpty else { return }
        guard page.pageMinSeq == nil || page.pageMinSeq == page.messages.first?.seq,
              page.pageMaxSeq == nil || page.pageMaxSeq == page.messages.last?.seq else {
            throw CatchUpError.snapshotChanged
        }
        for (previous, next) in zip(page.messages, page.messages.dropFirst()) {
            guard next.seq == previous.seq + 1 else {
                throw CatchUpError.sequenceGap
            }
        }
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
