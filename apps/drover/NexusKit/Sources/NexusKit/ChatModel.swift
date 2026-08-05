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
    /// True while a turn POST is in flight. The composer disables on this,
    /// so a slow network cannot be mistaken for a dead send button --
    /// which is what produced nine duplicate turns from one message.
    public private(set) var isSending = false
    public private(set) var harnessPresentation: HarnessPresentation
    /// Live context pressure for the header gauge; nil when the harness
    /// reports no per-call usage.
    public var contextGauge: ContextGauge? { ContextGauge(messages: messages) }
    public var composerText = ""
    /// Images picked in the composer, waiting to ride the next turn.
    public var pendingAttachments: [TurnAttachment] = []
    /// Text the user sent while the harness was mid-turn (codex/gemini
    /// reject overlapping turns with 409 "turn already in flight"). Held
    /// here and auto-dispatched when the turn-complete status arrives.
    /// Claude never 409s on overlap (mid-turn input is steering), so this
    /// only ever fills for harnesses that actually reject.
    public private(set) var queuedTurn: String?
    /// Attachments that were on a turn deferred by the same 409.
    private var queuedAttachments: [TurnAttachment] = []

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
        harness: String? = nil,
        streamFactory: ((NexusClient, String) -> MessageStream)? = nil
    ) {
        self.init(
            client: client,
            sessionID: sessionID,
            harness: harness,
            initialMessages: [],
            streamFactory: streamFactory
        )
    }

    /// Test-only designated init: seeds `messages` directly. Not `public` —
    /// reachable only via `@testable import NexusKit` from this package's own
    /// test targets (see `ChatModel.fixture()` in test support).
    init(
        client: NexusClient,
        sessionID: String,
        harness: String? = nil,
        initialMessages: [HarnessMessage],
        streamFactory: ((NexusClient, String) -> MessageStream)? = nil
    ) {
        self.client = client
        self.sessionID = sessionID
        self.messages = initialMessages
        self.harnessPresentation = HarnessPresentation(harness ?? "")
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
            dispatchQueuedTurnIfComplete(message)
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
        guard !isSending else { return }
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = pendingAttachments
        guard !text.isEmpty || !images.isEmpty else { return }
        isSending = true
        defer { isSending = false }
        do {
            _ = try await client.sendTurn(sessionID: sessionID, text: text, images: images)
            composerText = ""
            pendingAttachments = []
            hint = nil
        } catch NexusError.conflict(let message) where message == "turn already in flight" {
            // The harness rejects overlapping turns — queue instead of
            // erroring, and dispatch when the turn-complete status arrives.
            if !text.isEmpty {
                queuedTurn = queuedTurn.map { "\($0)\n\(text)" } ?? text
            }
            queuedAttachments.append(contentsOf: images)
            composerText = ""
            pendingAttachments = []
            hint = "Queued — sends when the current response finishes."
        } catch {
            // Preserve composerText/attachments on failure (other 409s or
            // transport) so the user can retry without retyping.
            applyHint(for: error, action: "send")
        }
    }

    /// Every harness driver marks end-of-turn with a `status` message whose
    /// payload carries `turn_complete: true`. If a turn is queued, this is
    /// the moment it can be accepted — dispatch it.
    private func dispatchQueuedTurnIfComplete(_ message: HarnessMessage) {
        guard message.type == .status,
              message.payload["turn_complete"]?.boolValue == true,
              queuedTurn != nil || !queuedAttachments.isEmpty
        else { return }
        let text = queuedTurn ?? ""
        let images = queuedAttachments
        queuedTurn = nil
        queuedAttachments = []
        Task { await sendQueued(text, images: images) }
    }

    private func sendQueued(_ text: String, images: [TurnAttachment]) async {
        do {
            _ = try await client.sendTurn(sessionID: sessionID, text: text, images: images)
            hint = nil
        } catch NexusError.conflict(let message) where message == "turn already in flight" {
            // Raced a new turn (e.g. an approval resumed it) — keep waiting
            // for the next turn-complete.
            if !text.isEmpty {
                queuedTurn = queuedTurn.map { "\(text)\n\($0)" } ?? text
            }
            queuedAttachments = images + queuedAttachments
        } catch {
            // Anything else: hand the text and images back to the composer
            // for a manual retry rather than dropping them silently.
            composerText = composerText.isEmpty ? text : "\(text)\n\(composerText)"
            pendingAttachments = images + pendingAttachments
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
    /// handoff context, optionally retargeting a different harness (nil
    /// keeps the source session's own). Returns the new session (id plus
    /// whether it's structured, for navigation), or nil on failure (with
    /// the server's explanation surfaced as a hint).
    public func handOff(targetHarness: String? = nil) async -> ContinuedSession? {
        do {
            let continued = try await client.continueSession(sessionID: sessionID,
                                                             targetHarness: targetHarness)
            hint = nil
            return continued
        } catch {
            applyHint(for: error, action: "hand off")
            return nil
        }
    }

    /// Enabled harnesses on this session's host, for the handoff target
    /// picker. Loaded on demand by `loadHandoffTargets()`; empty until then
    /// (the UI falls back to the plain same-harness handoff).
    public private(set) var handoffHarnesses: [String] = []

    /// Resolves this session's host from the snapshot and publishes its
    /// enabled harness list. Failures (or an unknown session) leave the
    /// list as-is — the picker just doesn't gain per-harness options.
    public func loadHandoffTargets() async {
        guard let snapshot = try? await client.snapshot(),
              let session = snapshot.sessions.first(where: { $0.id == sessionID })
        else { return }
        harnessPresentation = HarnessPresentation(session.harness)
        guard let host = snapshot.hosts.first(where: { $0.id == session.hostID }) else { return }
        handoffHarnesses = host.harnesses
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
