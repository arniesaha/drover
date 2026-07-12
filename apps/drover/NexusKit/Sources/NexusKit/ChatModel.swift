import Foundation
import Observation

/// All chat logic for one structured-session conversation: ingesting the
/// resumable `MessageStream`, tracking the newest unanswered approval
/// request, and issuing turns/approvals/interrupt/terminate against
/// `NexusClient`. Kept free of SwiftUI so it's unit-testable without sockets
/// (see `ingest(_:)`) — `ChatView` and friends only render this state.
@MainActor
@Observable
public final class ChatModel {
    private let client: NexusClient
    private let sessionID: String
    private let stream: MessageStream

    public private(set) var messages: [HarnessMessage]
    public private(set) var isConnected = false
    /// Latches true on the first successful connection — lets the UI
    /// suppress its "reconnecting" indicator during the initial connect
    /// (when there is nothing to *re*-connect to yet).
    public private(set) var hasConnectedOnce = false
    /// True while an `approve(_:)` network call is in flight; the UI should
    /// disable the Approve/Deny controls to prevent double-submission.
    public private(set) var isAnswering = false
    public private(set) var pendingApproval: HarnessMessage?
    public private(set) var hint: String?
    public var composerText = ""

    // `nonisolated(unsafe)` solely so `deinit` (nonisolated in Swift 6) can
    // cancel it; every other access is from `@MainActor` methods, and deinit
    // runs with exclusive access to the dying object, so there is no race.
    private nonisolated(unsafe) var pumpTask: Task<Void, Never>?
    /// Monotonic token identifying the current pump: a finishing pump only
    /// clears `pumpTask` if it hasn't been superseded by a newer `start()`.
    private var pumpGeneration = 0

    public convenience init(
        client: NexusClient,
        sessionID: String,
        streamFactory: ((NexusClient, String) -> MessageStream)? = nil
    ) {
        self.init(client: client, sessionID: sessionID, initialMessages: [], streamFactory: streamFactory)
    }

    /// Test-only designated init: seeds `messages` directly. Not `public` —
    /// reachable only via `@testable import NexusKit` from this package's own
    /// test targets (see `ChatModel.fixture()` in test support).
    init(
        client: NexusClient,
        sessionID: String,
        initialMessages: [HarnessMessage],
        streamFactory: ((NexusClient, String) -> MessageStream)? = nil
    ) {
        self.client = client
        self.sessionID = sessionID
        self.messages = initialMessages
        let factory = streamFactory ?? { c, s in MessageStream(client: c, sessionID: s) }
        self.stream = factory(client, sessionID)
        recomputePendingApproval()
    }

    deinit {
        // Belt one: whoever drops the model (e.g. SwiftUI discarding
        // ChatView's subtree on navigation pop) must not leak a
        // still-streaming socket even if `stop()` wasn't called explicitly.
        pumpTask?.cancel()
    }

    // MARK: - Streaming

    public func start() {
        guard pumpTask == nil else { return }
        pumpGeneration += 1
        let generation = pumpGeneration
        pumpTask = Task { [weak self] in
            // Runs on the main actor (this Task inherits the model's
            // isolation), so the defer's property access is safe. Clearing
            // the slot on exit lets a later start() restart the stream; the
            // generation check keeps a stale pump (superseded by a
            // stop()+start() pair while it was winding down) from clobbering
            // its replacement's slot.
            defer {
                if let self, self.pumpGeneration == generation {
                    self.pumpTask = nil
                }
            }
            // If we were cancelled before ever running (stop() in the same
            // run-loop tick), don't touch the stream at all — calling
            // events() here would needlessly cancel a successor's pump.
            guard let stream = self?.stream, !Task.isCancelled else { return }
            for await event in await stream.events() {
                guard !Task.isCancelled else { break }
                // Belt two: re-fetch `self` weakly each iteration rather than
                // holding a strong reference across the `for await`
                // suspension point, so a dropped model can still deinit
                // (and hit belt one) while this loop is between events.
                guard let self else { break }
                self.ingest(event)
            }
        }
    }

    public func stop() {
        // Cancelling the pump ends its `for await`, which fires the
        // AsyncStream's onTermination and cancels MessageStream's internal
        // pump — no explicit stream.stop() needed. (A fire-and-forget
        // `Task { await stream.stop() }` here used to race a subsequent
        // start(): landing on the actor late, it could cancel the *new*
        // events() pump and wedge the stream.)
        pumpTask?.cancel()
        pumpTask = nil
    }

    /// Reducer driving both the live pump above and the unit tests — funnels
    /// a `StreamEvent` into `messages`/`isConnected`/`pendingApproval` with
    /// no socket involved.
    func ingest(_ event: StreamEvent) {
        switch event {
        case .message(let message):
            messages.append(message)
            recomputePendingApproval()
        case .connection(let connected):
            isConnected = connected
            if connected { hasConnectedOnce = true }
        case .unauthorized:
            // Terminal: MessageStream has already stopped reconnecting (see
            // its doc comment on `.unauthorized`). Surface a hint instead of
            // the generic "Reconnecting…" pill so the user knows to fix the
            // token rather than wait forever.
            isConnected = false
            hint = "Token rejected — check Settings."
        }
    }

    /// `pendingApproval` is the latest (highest-`seq`) `approval_prompt`
    /// whose `payload.request_id` has no later `approval_response` carrying
    /// the same `request_id`.
    private func recomputePendingApproval() {
        let prompts = messages.filter { $0.type == .approvalPrompt }.sorted { $0.seq > $1.seq }
        for prompt in prompts {
            guard let requestID = prompt.payload["request_id"]?.stringValue else { continue }
            let answered = messages.contains { message in
                message.type == .approvalResponse
                    && message.seq > prompt.seq
                    && message.payload["request_id"]?.stringValue == requestID
            }
            if !answered {
                pendingApproval = prompt
                return
            }
        }
        pendingApproval = nil
    }

    // MARK: - Actions

    public func sendTurn() async {
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        do {
            _ = try await client.sendTurn(sessionID: sessionID, text: text)
            composerText = ""
            hint = nil
        } catch {
            // Preserve composerText on failure (409 or otherwise) so the
            // user can retry without retyping.
            applyHint(for: error, action: "send")
        }
    }

    public func approve(_ decision: String) async {
        guard !isAnswering else { return }
        guard let requestID = pendingApproval?.payload["request_id"]?.stringValue else { return }
        isAnswering = true
        defer { isAnswering = false }
        do {
            try await client.answerPermission(sessionID: sessionID, requestID: requestID,
                                              decision: decision, note: nil)
            hint = nil
        } catch {
            applyHint(for: error, action: "answer")
        }
    }

    /// Hands this session off to a fresh one seeded with the server-built
    /// handoff context. Returns the new session's id, or nil on failure
    /// (with the server's explanation surfaced as a hint).
    public func handOff() async -> String? {
        do {
            let newSessionID = try await client.continueSession(sessionID: sessionID)
            hint = nil
            return newSessionID
        } catch {
            applyHint(for: error, action: "hand off")
            return nil
        }
    }

    public func interrupt() async {
        do {
            try await client.interrupt(sessionID: sessionID)
            hint = nil
        } catch {
            applyHint(for: error, action: "interrupt")
        }
    }

    public func terminate() async {
        do {
            try await client.terminate(sessionID: sessionID)
            hint = nil
        } catch {
            applyHint(for: error, action: "terminate")
        }
    }

    /// 409/400 responses carry a server-authored explanation meant for the
    /// user (e.g. "turn already in flight") — surfaced verbatim as a hint,
    /// never as a hard error. Anything else (transport/decoding failures)
    /// gets a generic retry hint.
    private func applyHint(for error: Error, action: String) {
        switch error {
        case NexusError.conflict(let message), NexusError.badRequest(let message):
            hint = message
        default:
            hint = "Could not \(action) — try again."
        }
    }
}
