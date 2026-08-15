import Foundation
import Observation
import os

/// All chat logic for one structured-session conversation: ingesting the
/// resumable `MessageStream`, tracking the newest unanswered approval
/// request, and issuing turns/approvals/interrupt/terminate against
/// `DroverClient`. Kept free of SwiftUI so it's unit-testable without sockets
/// (see `ingest(_:)`) — `ChatView` and friends only render this state.
@MainActor
@Observable
public final class ChatModel {
    private let client: DroverClient
    private let sessionID: String
    private let stream: MessageStream
    public let runPreferences: HarnessModelCatalogState

    public private(set) var messages: [HarnessMessage]
    public private(set) var isConnected = false
    /// Latches true on the first successful connection — lets the UI
    /// suppress its "reconnecting" indicator during the initial connect
    /// (when there is nothing to *re*-connect to yet).
    public private(set) var hasConnectedOnce = false
    public private(set) var hasOlderHistory = false
    public private(set) var isLoadingOlderHistory = false
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
    /// The server-generated recap when available, otherwise the session's
    /// stable preview. It is intentionally independent of the message stream:
    /// snapshot refreshes must never manufacture transcript events or reuse
    /// their sequence numbers.
    public private(set) var recap: String?
    public private(set) var recapSourceSeq: Int?
    /// Bumped once per `messages` mutation. Every derived value below reads
    /// this — and only this — so SwiftUI's dependency is "the transcript
    /// changed" rather than "something on the model changed". A scroll-phase
    /// change or a keystroke re-runs the view body without recomputing a
    /// single fold.
    public private(set) var messagesVersion = 0

    /// Derived transcript state, cached against `messagesVersion`.
    ///
    /// These used to be plain computed properties, which meant a full O(n)
    /// pass *per read*: the view body reads `items`, the visual tail,
    /// `artifacts` (twice) and `contextGauge`, so one render walked a
    /// 3,000-message transcript five times. Sessions here really do reach
    /// 3,316 messages, and the body re-runs on every scroll phase change.
    ///
    /// The caches are `@ObservationIgnored` deliberately: writing them during
    /// a read must not register as a mutation, or every render would
    /// invalidate the view that just rendered.
    @ObservationIgnored private var itemsCache: (version: Int, items: [TranscriptItem])?
    @ObservationIgnored private var rowIDCache: (version: Int, id: String?)?
    @ObservationIgnored private var artifactsCache: (version: Int, artifacts: [SessionArtifact])?
    @ObservationIgnored private var gaugeCache: (version: Int, gauge: ContextGauge?)?
    @ObservationIgnored private(set) var historyPagesMerged = 0
    @ObservationIgnored private(set) var lastHistoryMergeDuration: Duration?

    private static let historySignposter = OSSignposter(
        subsystem: "com.arniesaha.drover",
        category: "SessionHistory"
    )

    /// The folded transcript: thinking runs, status runs and tool-step runs
    /// collapsed into render rows.
    public var items: [TranscriptItem] {
        if let itemsCache, itemsCache.version == messagesVersion { return itemsCache.items }
        let folded = TranscriptItem.group(messages)
        itemsCache = (messagesVersion, folded)
        return folded
    }

    /// The row updated by the newest raw message. This may sit earlier than
    /// the visual tail when a tool result attaches to a prior step, so pinned
    /// scrolling must use `visualTailRowID` instead.
    public var latestRowID: String? {
        if let rowIDCache, rowIDCache.version == messagesVersion { return rowIDCache.id }
        let id = TranscriptItem.latestRowID(of: messages)
        rowIDCache = (messagesVersion, id)
        return id
    }

    /// The bottom-most row in visual transcript order. A newly-arrived raw
    /// event can update an earlier folded row, so pinned scrolling must use
    /// this rather than `latestRowID`.
    public var visualTailRowID: String? {
        items.last?.id
    }

    /// Prefer the current server summary for an open chat, but retain the
    /// harness title until neither a recap nor its preview fallback exists.
    public var headerTitle: String {
        recap ?? harnessPresentation.name
    }

    /// The title is the work summary; this keeps provenance and live context
    /// pressure visible without duplicating the summary text.
    public var headerMetadata: String {
        [harnessPresentation.name, contextGauge?.text]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    /// Live context pressure for the header gauge; nil when the harness
    /// reports no per-call usage.
    public var contextGauge: ContextGauge? {
        if let gaugeCache, gaugeCache.version == messagesVersion { return gaugeCache.gauge }
        let gauge = ContextGauge(messages: messages, harness: harnessPresentation.harness)
        gaugeCache = (messagesVersion, gauge)
        return gauge
    }

    /// Branches and pull requests this session produced. Derived rather than
    /// stored — the hub reports neither, so the transcript is the only place
    /// they exist.
    public var artifacts: [SessionArtifact] {
        if let artifactsCache, artifactsCache.version == messagesVersion { return artifactsCache.artifacts }
        let found = SessionArtifactExtractor.artifacts(in: messages)
        artifactsCache = (messagesVersion, found)
        return found
    }
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
    /// Text from a send whose outcome is genuinely unknown. A transport
    /// failure cannot distinguish "the hub never saw it" from "the hub
    /// accepted it and the response was lost", so the text stays in the
    /// composer for a manual retry. If the hub then streams the turn back,
    /// it demonstrably landed, and continuing to offer the same text is an
    /// invitation to send it twice.
    private var unconfirmedTurnText: String?
    /// Attachments submitted with `unconfirmedTurnText`. An echo proves these
    /// particular attachments landed, but not attachments the user picked
    /// while waiting for that proof.
    private var unconfirmedAttachments: [TurnAttachment] = []

    /// request_id -> the prompt still awaiting an answer. Tiny in practice:
    /// a harness blocks on one approval at a time.
    @ObservationIgnored private var openApprovals: [String: HarnessMessage] = [:]

    // `nonisolated(unsafe)` solely so `deinit` (nonisolated in Swift 6) can
    // cancel it; every other access is from `@MainActor` methods, and deinit
    // runs with exclusive access to the dying object, so there is no race.
    private nonisolated(unsafe) var pumpTask: Task<Void, Never>?
    /// Monotonic token identifying the current pump: a finishing pump only
    /// clears `pumpTask` if it hasn't been superseded by a newer `start()`.
    private var pumpGeneration = 0
    /// Bounded snapshot poll started after a terminal stream status. Kept
    /// separate from the stream task so snapshot timing cannot affect turn
    /// delivery, queueing, or message sequence handling.
    private nonisolated(unsafe) var recapRefreshTask: Task<Void, Never>?
    private var recapRefreshGeneration = 0
    private var hasInitializedRunPreferences = false
    private let recapPollInterval: Duration
    private let recapPollAttempts: Int

    public convenience init(
        client: DroverClient,
        sessionID: String,
        harness: String? = nil,
        store: HarnessModelCatalogStore = HarnessModelCatalogStore(),
        recap: String? = nil,
        recapSourceSeq: Int? = nil,
        recapPollInterval: Duration = .seconds(1),
        recapPollAttempts: Int = 30,
        streamFactory: ((DroverClient, String) -> MessageStream)? = nil
    ) {
        self.init(
            client: client,
            sessionID: sessionID,
            harness: harness,
            store: store,
            initialMessages: [],
            recap: recap,
            recapSourceSeq: recapSourceSeq,
            recapPollInterval: recapPollInterval,
            recapPollAttempts: recapPollAttempts,
            streamFactory: streamFactory
        )
    }

    /// Test-only designated init: seeds `messages` directly. Not `public` —
    /// reachable only via `@testable import DroverKit` from this package's own
    /// test targets (see `ChatModel.fixture()` in test support).
    init(
        client: DroverClient,
        sessionID: String,
        harness: String? = nil,
        store: HarnessModelCatalogStore = HarnessModelCatalogStore(),
        initialMessages: [HarnessMessage],
        recap: String? = nil,
        recapSourceSeq: Int? = nil,
        recapPollInterval: Duration = .seconds(1),
        recapPollAttempts: Int = 30,
        streamFactory: ((DroverClient, String) -> MessageStream)? = nil
    ) {
        self.client = client
        self.sessionID = sessionID
        self.runPreferences = HarnessModelCatalogState(client: client, store: store)
        self.messages = initialMessages
        self.harnessPresentation = HarnessPresentation(harness ?? "")
        self.recap = recap
        self.recapSourceSeq = recapSourceSeq
        self.recapPollInterval = recapPollInterval
        self.recapPollAttempts = recapPollAttempts
        let factory = streamFactory ?? { c, s in MessageStream(client: c, sessionID: s) }
        self.stream = factory(client, sessionID)
        rebuildApprovals()
    }

    deinit {
        // Belt one: whoever drops the model (e.g. SwiftUI discarding
        // ChatView's subtree on navigation pop) must not leak a
        // still-streaming socket even if `stop()` wasn't called explicitly.
        pumpTask?.cancel()
        recapRefreshTask?.cancel()
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
                if case .history = event {
                    self.hasOlderHistory = await stream.olderHistoryAvailable()
                }
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
        recapRefreshTask?.cancel()
        recapRefreshTask = nil
        recapRefreshGeneration &+= 1
    }

    @discardableResult
    public func loadOlderHistory() async -> Bool {
        guard hasOlderHistory, !isLoadingOlderHistory else { return false }
        isLoadingOlderHistory = true
        defer { isLoadingOlderHistory = false }
        do {
            guard let page = try await stream.loadOlderHistory() else { return false }
            mergeHistory(page.messages)
            hasOlderHistory = page.hasOlder
            if hint == "Could not load earlier messages — try again." {
                hint = nil
            }
            return true
        } catch is CancellationError {
            return false
        } catch {
            hint = "Could not load earlier messages — try again."
            return false
        }
    }

    /// Reducer driving both the live pump above and the unit tests — funnels
    /// a `StreamEvent` into `messages`/`isConnected`/`pendingApproval` with
    /// no socket involved.
    func ingest(_ event: StreamEvent) {
        switch event {
        case .message(let message):
            messages.append(message)
            messagesVersion &+= 1
            noteApproval(message)
            refreshRecapAfterTurnComplete(message)
            dispatchQueuedTurnIfComplete(message)
            confirmUnconfirmedTurn(message)
        case .history(let messages, _):
            mergeHistory(messages)
            // Losing a response usually means losing the socket too, so the
            // echo that resolves an unconfirmed send often arrives here
            // rather than as a live message.
            messages.forEach(confirmUnconfirmedTurn)
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

    private func mergeHistory(_ incoming: [HarnessMessage]) {
        guard !incoming.isEmpty else { return }
        let clock = ContinuousClock()
        let started = clock.now
        let signpostID = Self.historySignposter.makeSignpostID()
        let signpostState = Self.historySignposter.beginInterval(
            "MergeHistoryPage", id: signpostID
        )
        defer {
            Self.historySignposter.endInterval("MergeHistoryPage", signpostState)
            lastHistoryMergeDuration = started.duration(to: clock.now)
            historyPagesMerged &+= 1
        }

        let isStrictlyAscending = zip(incoming, incoming.dropFirst()).allSatisfy {
            $0.seq < $1.seq
        }
        let canAppend: Bool
        if let currentLast = messages.last?.seq {
            canAppend = isStrictlyAscending && incoming[0].seq > currentLast
        } else {
            canAppend = isStrictlyAscending
        }

        if canAppend {
            messages.append(contentsOf: incoming)
        } else {
            var bySequence: [Int: HarnessMessage] = [:]
            bySequence.reserveCapacity(messages.count + incoming.count)
            for message in messages {
                bySequence[message.seq] = message
            }
            for message in incoming {
                bySequence[message.seq] = message
            }
            messages = bySequence.values.sorted { $0.seq < $1.seq }
        }

        rebuildApprovals()
        messagesVersion &+= 1
        // Historical terminal statuses describe already-completed work. They
        // must never dispatch a turn queued against the current live run.
    }

    /// `pendingApproval` is the latest (highest-`seq`) `approval_prompt`
    /// whose `payload.request_id` has no later `approval_response` carrying
    /// the same `request_id`.
    ///
    /// Maintained incrementally. The previous version rescanned the whole
    /// transcript on *every appended message* — filter, sort, then a nested
    /// `contains` per prompt — so replaying a 3,316-message session cost
    /// O(n² x prompts) before a single frame was drawn. Open prompts are
    /// almost always zero or one, so tracking them directly makes the live
    /// path O(1) and the catch-up path linear.
    private func noteApproval(_ message: HarnessMessage) {
        guard let requestID = message.payload["request_id"]?.stringValue else { return }
        switch message.type {
        case .approvalPrompt:
            openApprovals[requestID] = message
        case .approvalResponse:
            // A response only answers a prompt that came before it; one
            // arriving first would be for a prompt this client never saw.
            if let prompt = openApprovals[requestID], message.seq > prompt.seq {
                openApprovals.removeValue(forKey: requestID)
            }
        default:
            return
        }
        pendingApproval = openApprovals.values.max { $0.seq < $1.seq }
    }

    /// One linear pass for the initial backlog, then `noteApproval` keeps it
    /// current.
    private func rebuildApprovals() {
        openApprovals.removeAll(keepingCapacity: true)
        for message in messages {
            guard let requestID = message.payload["request_id"]?.stringValue else {
                continue
            }
            switch message.type {
            case .approvalPrompt:
                openApprovals[requestID] = message
            case .approvalResponse:
                if let prompt = openApprovals[requestID], message.seq > prompt.seq {
                    openApprovals.removeValue(forKey: requestID)
                }
            default:
                continue
            }
        }
        pendingApproval = openApprovals.values.max { $0.seq < $1.seq }
    }

    // MARK: - Actions

    public func sendTurn() async {
        guard !isSending else { return }
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = pendingAttachments
        guard !text.isEmpty || !images.isEmpty else { return }
        isSending = true
        defer { isSending = false }
        let preferences = turnPreferences
        do {
            _ = try await client.sendTurn(
                sessionID: sessionID,
                text: text,
                images: images,
                model: preferences.model,
                thinkingEffort: preferences.thinking
            )
            composerText = ""
            pendingAttachments = []
            hint = nil
            unconfirmedTurnText = nil
            unconfirmedAttachments = []
        } catch DroverError.conflict(let message) where message == "turn already in flight" {
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
            // transport) so the user can retry without retyping. Whether the
            // hub saw this turn is unknown until it does or does not echo.
            unconfirmedTurnText = text
            unconfirmedAttachments = images
            applyHint(for: error, action: "send")
        }
    }

    /// The hub echoing a `user_input` that matches an unconfirmed send is
    /// proof the turn landed despite the client seeing a failure. Take the
    /// text back out of the composer: leaving it there reads as "not sent"
    /// and the obvious next action duplicates the turn.
    ///
    /// Only an exact match clears. If the user has typed since, the composer
    /// holds their words too, and silently discarding those to resolve our
    /// own ambiguity would be the worse trade.
    private func confirmUnconfirmedTurn(_ message: HarnessMessage) {
        guard message.type == .userInput,
              let unconfirmed = unconfirmedTurnText,
              message.text == unconfirmed
        else { return }
        unconfirmedTurnText = nil
        let landedAttachments = unconfirmedAttachments
        unconfirmedAttachments = []

        // A manual retry can be rejected and queued while the original,
        // ambiguously failed request is still in flight. Once its echo
        // arrives, that matching queue entry is a confirmed duplicate. Keep
        // any retry-only attachments available for the next turn instead of
        // silently dropping them with the cancelled retry.
        if queuedTurn == unconfirmed {
            queuedTurn = nil
            pendingAttachments = queuedAttachments + pendingAttachments
            queuedAttachments = []
        }

        // Remove the attachments proved to have landed one at a time so any
        // attachment selected after the failed send stays in the composer.
        for attachment in landedAttachments {
            if let index = pendingAttachments.firstIndex(of: attachment) {
                pendingAttachments.remove(at: index)
            }
        }
        if composerText.trimmingCharacters(in: .whitespacesAndNewlines) == unconfirmed {
            composerText = ""
        }
        hint = nil
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

    /// Starts a new bounded poll after a real-time completion. The server
    /// writes recaps asynchronously, so a snapshot may temporarily retain an
    /// older source sequence. We never turn those snapshots into stream
    /// messages: doing so would collide with the server's event ordering.
    private func refreshRecapAfterTurnComplete(_ message: HarnessMessage) {
        guard message.type == .status,
              message.payload["turn_complete"]?.boolValue == true
        else { return }

        recapRefreshTask?.cancel()
        recapRefreshTask = nil
        recapRefreshGeneration &+= 1
        let generation = recapRefreshGeneration
        let targetSequence = message.seq
        let attempts = recapPollAttempts
        let interval = recapPollInterval

        guard attempts > 0,
              (recapSourceSeq ?? -1) < targetSequence
        else { return }

        let client = client
        let sessionID = sessionID
        recapRefreshTask = Task { [weak self, client, sessionID] in
            defer {
                if let self, self.recapRefreshGeneration == generation {
                    self.recapRefreshTask = nil
                }
            }
            for attempt in 0..<attempts {
                guard !Task.isCancelled else { return }
                let metadata = await Self.fetchSessionMetadata(client: client, sessionID: sessionID)
                guard !Task.isCancelled,
                      let self,
                      self.recapRefreshGeneration == generation
                else { return }

                // A recap writer can lag the stream by several snapshots.
                // Keep the currently displayed successful recap until this
                // completion's source sequence is present; failed, missing,
                // and intermediate snapshots all consume one bounded attempt.
                if let metadata,
                   let recap = metadata.session.recap, !recap.isEmpty,
                   let sourceSequence = metadata.session.recapSourceSeq,
                   sourceSequence >= targetSequence {
                    self.applySessionMetadata(metadata.snapshot, session: metadata.session)
                    return
                }
                guard attempt + 1 < attempts else { return }
                do {
                    try await Task.sleep(for: interval)
                } catch is CancellationError {
                    return
                } catch {
                    return
                }
            }
        }
    }

    private func sendQueued(_ text: String, images: [TurnAttachment]) async {
        let preferences = turnPreferences
        do {
            _ = try await client.sendTurn(
                sessionID: sessionID,
                text: text,
                images: images,
                model: preferences.model,
                thinkingEffort: preferences.thinking
            )
            hint = nil
            unconfirmedTurnText = nil
            unconfirmedAttachments = []
        } catch DroverError.conflict(let message) where message == "turn already in flight" {
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
            unconfirmedTurnText = text
            unconfirmedAttachments = images
            applyHint(for: error, action: "send")
        }
    }

    private var turnPreferences: (model: String?, thinking: String?) {
        let harness = harnessPresentation.harness
        guard HarnessRunPreferences.canChangeInExistingSession(harness) else {
            return (nil, nil)
        }
        return (runPreferences.modelOverride, runPreferences.thinkingEffortOverride)
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
    /// picker. Loaded on demand by `loadSessionMetadata()`; empty until then
    /// (the UI falls back to the plain same-harness handoff).
    public private(set) var handoffHarnesses: [String] = []

    /// Applies all snapshot-backed state for this open session. A missing
    /// generated recap may seed the display from preview only when the chat
    /// has no good text yet, so a pending or failed recap generation cannot
    /// erase a summary that was already visible.
    public func loadSessionMetadata() async {
        guard let metadata = await Self.fetchSessionMetadata(client: client, sessionID: sessionID)
        else { return }
        applySessionMetadata(metadata.snapshot, session: metadata.session)
        if !hasInitializedRunPreferences {
            hasInitializedRunPreferences = true
            runPreferences.select(
                hostID: metadata.session.hostID,
                harness: metadata.session.harness,
                seedModel: Self.nonEmpty(metadata.session.model),
                seedThinkingEffort: Self.nonEmpty(metadata.session.thinkingEffort)
            )
        }
        await runPreferences.refresh()
    }

    /// Fetching is intentionally side-effect free so a recap-refresh task can
    /// discard a late snapshot after cancellation or supersession, before it
    /// changes any observable chat state.
    private nonisolated static func fetchSessionMetadata(
        client: DroverClient, sessionID: String
    ) async -> (snapshot: HarnessSnapshot, session: SessionSummary)? {
        guard let snapshot = try? await client.snapshot(),
              let session = snapshot.sessions.first(where: { $0.id == sessionID })
        else { return nil }
        return (snapshot, session)
    }

    private func applySessionMetadata(_ snapshot: HarnessSnapshot, session: SessionSummary) {
        harnessPresentation = HarnessPresentation(session.harness)
        if let generatedRecap = session.recap, !generatedRecap.isEmpty {
            let isOlderThanCurrent = {
                guard let incoming = session.recapSourceSeq,
                      let current = recapSourceSeq
                else { return recapSourceSeq != nil && session.recapSourceSeq == nil }
                return incoming < current
            }()
            if !isOlderThanCurrent {
                recap = generatedRecap
                recapSourceSeq = session.recapSourceSeq
            }
        } else if recap == nil, let preview = session.preview, !preview.isEmpty {
            recap = preview
            recapSourceSeq = nil
        }
        guard let host = snapshot.hosts.first(where: { $0.id == session.hostID }) else { return }
        handoffHarnesses = host.harnesses
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }

    /// Compatibility entry point for existing callers. Metadata loading grew
    /// beyond the handoff picker, but remains the same single snapshot read.
    public func loadHandoffTargets() async {
        await loadSessionMetadata()
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
        case DroverError.conflict(let message), DroverError.badRequest(let message):
            hint = message
        default:
            hint = "Could not \(action) — try again."
        }
    }
}
