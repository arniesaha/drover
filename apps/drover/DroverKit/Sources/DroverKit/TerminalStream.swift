import Foundation

// MARK: - TerminalConnecting

/// A live terminal WebSocket connection: incoming frames plus an outgoing
/// send. Unlike `WebSocketConnecting` (the chat stream's receive-only seam),
/// the terminal needs both directions — keystrokes and resizes go out on the
/// same socket the output comes in on.
public struct TerminalConnection: Sendable {
    public let frames: AsyncThrowingStream<String, Error>
    public let send: @Sendable (String) -> Void

    public init(frames: AsyncThrowingStream<String, Error>,
                send: @escaping @Sendable (String) -> Void) {
        self.frames = frames
        self.send = send
    }
}

/// Seam for tests: something that can open a terminal WebSocket.
public protocol TerminalConnecting: Sendable {
    func connect(_ request: URLRequest) -> TerminalConnection
}

// MARK: - URLSessionTerminalConnector

/// Wraps a real `URLSessionWebSocketTask` into the `TerminalConnecting` seam.
public struct URLSessionTerminalConnector: TerminalConnecting {
    public init() {}

    public func connect(_ request: URLRequest) -> TerminalConnection {
        let session = URLSession(configuration: .default)
        let task = session.webSocketTask(with: request)
        task.resume()

        let frames = AsyncThrowingStream<String, Error> { continuation in
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

        return TerminalConnection(frames: frames) { frame in
            task.send(.string(frame)) { _ in }
        }
    }
}

// MARK: - TerminalStreamEvent

public enum TerminalStreamEvent: Sendable, Equatable {
    case output(String)
    /// The remote process exited (daemon's `{"type": "exit"}` frame). This is
    /// the only *terminal* event: the stream finishes after emitting it —
    /// there is nothing left to reconnect to.
    case exited
    /// false while reconnecting after a socket drop; true once the daemon's
    /// first frame arrives on a (re)connection (it always sends `attached`
    /// the instant it accepts the upgrade).
    case connection(Bool)
    /// A connection attempt that ended without ever attaching, with the
    /// reason. Distinct from `.connection(false)`, which also fires for a drop
    /// after the terminal was live: only an attempt that never saw a frame
    /// tells you the session has *never* been reachable, which is the one
    /// case a black screen cannot explain on its own (#170).
    case connectFailed(String)
}

// MARK: - TerminalStream

/// Resumable terminal socket for a single PTY session: decodes wire frames
/// via `TerminalWire`, and on socket failure reconnects with doubling backoff
/// (capped at 30s) — the daemon keeps the PTY alive across client drops, so
/// a transient disconnect (network blip, iOS suspending the socket in the
/// background) must never be presented as "session ended". Only the daemon's
/// explicit `exit` frame ends the stream.
///
/// The last-known terminal size is re-sent on every (re)connection: it tells
/// the daemon the client's real dimensions again, and the SIGWINCH it causes
/// makes full-screen TUIs repaint — the closest thing to a scrollback replay
/// the wire offers from the client side.
public actor TerminalStream {
    private let request: URLRequest
    private let connector: TerminalConnecting
    private let reconnectBaseDelay: Duration

    private var pumpTask: Task<Void, Never>?
    private var backoffSleeper: Task<Void, Never>?

    /// Outgoing-path state lives outside actor isolation, behind a lock, so
    /// `send`/`sendResize` can be nonisolated and *synchronous*: SwiftTerm's
    /// delegate calls them per keystroke, and hopping each keystroke through
    /// an unstructured Task would not preserve typing order.
    private final class OutgoingState: @unchecked Sendable {
        private let lock = NSLock()
        private var send: (@Sendable (String) -> Void)?
        private var lastResize: (rows: Int, cols: Int)?

        func setSend(_ newValue: (@Sendable (String) -> Void)?) {
            lock.lock(); defer { lock.unlock() }
            send = newValue
        }

        func currentSend() -> (@Sendable (String) -> Void)? {
            lock.lock(); defer { lock.unlock() }
            return send
        }

        func rememberResize(rows: Int, cols: Int) {
            lock.lock(); defer { lock.unlock() }
            lastResize = (rows, cols)
        }

        func recallResize() -> (rows: Int, cols: Int)? {
            lock.lock(); defer { lock.unlock() }
            return lastResize
        }
    }

    private let outgoing = OutgoingState()

    public init(
        request: URLRequest,
        connector: TerminalConnecting = URLSessionTerminalConnector(),
        reconnectBaseDelay: Duration = .seconds(1)
    ) {
        self.request = request
        self.connector = connector
        self.reconnectBaseDelay = reconnectBaseDelay
    }

    /// Single-consumer, mirroring `MessageStream.events()`: calling again
    /// cancels any previous pump rather than leaking it.
    public func events() -> AsyncStream<TerminalStreamEvent> {
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
        backoffSleeper?.cancel()
    }

    /// Sends a pre-encoded wire frame (see `TerminalWire.inputFrame`) on the
    /// current connection. Synchronous and nonisolated so per-keystroke
    /// calls preserve typing order. Dropped silently while disconnected —
    /// keystrokes during an outage have no socket to go to, same as before
    /// reconnects existed.
    public nonisolated func send(_ frame: String) {
        outgoing.currentSend()?(frame)
    }

    /// Sends a resize now (if connected) and remembers it for automatic
    /// re-send on every subsequent (re)connection.
    public nonisolated func sendResize(rows: Int, cols: Int) {
        outgoing.rememberResize(rows: rows, cols: cols)
        outgoing.currentSend()?(TerminalWire.resizeFrame(rows: rows, cols: cols))
    }

    /// Wakes a pending reconnect backoff so the next attempt happens now.
    /// Called when the app returns to the foreground — without it, coming
    /// back to a suspended terminal could wait out a full 30s backoff — and
    /// by the Retry button on the cold-open failure state.
    ///
    /// Cancelling the sleeper is only half of it. A nudge arriving while an
    /// attempt is in flight has no sleeper to cancel and used to do nothing
    /// at all, which made Retry a dead control exactly when it was pressed:
    /// the attempt it landed in then failed into the full backoff the tap was
    /// meant to avoid. The request is remembered and spent on the next
    /// backoff instead.
    ///
    /// Spent once, deliberately. A standing "do not back off" would turn one
    /// tap into a reconnect spin against a fleet that is still down.
    public func nudge() {
        retryRequested = true
        backoffSleeper?.cancel()
    }

    /// A retry asked for while no backoff was running, waiting to be spent.
    private var retryRequested = false

    private func consumeRetryRequest() -> Bool {
        defer { retryRequested = false }
        return retryRequested
    }

    // MARK: - Pump

    private func run(continuation: AsyncStream<TerminalStreamEvent>.Continuation) async {
        var backoff = reconnectBaseDelay
        var firstAttempt = true

        while !Task.isCancelled {
            if !firstAttempt {
                continuation.yield(.connection(false))
                if consumeRetryRequest() {
                    // Asked for by hand, so the schedule starts over rather
                    // than resuming a doubling that was already 30s deep.
                    backoff = reconnectBaseDelay
                } else {
                    await backoffSleep(backoff)
                    backoff = min(backoff * 2, .seconds(30))
                }
                if Task.isCancelled { break }
            }
            firstAttempt = false

            let connection = connector.connect(request)
            outgoing.setSend(connection.send)
            if let lastResize = outgoing.recallResize() {
                connection.send(TerminalWire.resizeFrame(rows: lastResize.rows,
                                                         cols: lastResize.cols))
            }

            var sawFrame = false
            do {
                for try await frame in connection.frames {
                    if Task.isCancelled { break }
                    if !sawFrame {
                        sawFrame = true
                        continuation.yield(.connection(true))
                        backoff = reconnectBaseDelay
                    }
                    switch TerminalWire.decodeOutput(frame) {
                    case .output(let text):
                        continuation.yield(.output(text))
                    case .exited:
                        continuation.yield(.exited)
                        continuation.finish()
                        outgoing.setSend(nil)
                        return
                    case .detached, .other, nil:
                        break   // control chatter; a detach arrives as a close
                    }
                }
                // Stream finished without error: server closed cleanly.
                // Treat as a disconnect and reconnect, same as an error.
                if Task.isCancelled { break }
                // A clean close before the daemon's `attached` frame is still
                // an attempt that never attached — there is no error to name,
                // so it takes the same line a dropped connection would.
                if !sawFrame {
                    continuation.yield(.connectFailed(DroverError.unreachableDescription))
                }
            } catch {
                if Task.isCancelled { break }
                // An attempt that never saw a frame never attached, so the
                // screen has nothing on it to explain the wait. Reported so a
                // cold open can eventually say so; the reconnect below is
                // unchanged (#170).
                if !sawFrame {
                    continuation.yield(
                        .connectFailed(DroverError.connectionFailureReason(error))
                    )
                }
            }
            outgoing.setSend(nil)
        }

        outgoing.setSend(nil)
        continuation.finish()
    }

    /// A backoff sleep that both `nudge()` and pump cancellation can cut
    /// short. Awaiting a child Task's value does not forward cancellation,
    /// so the cancellation handler propagates it explicitly.
    private func backoffSleep(_ duration: Duration) async {
        let sleeper = Task { try? await Task.sleep(for: duration); return () }
        backoffSleeper = sleeper
        await withTaskCancellationHandler {
            await sleeper.value
        } onCancel: {
            sleeper.cancel()
        }
        backoffSleeper = nil
    }
}
