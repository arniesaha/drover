import Foundation
import Observation
import os

/// A locally rendered turn that has left the composer but has not yet been
/// echoed by the structured-session stream. The client owns the correlation
/// ID for the entire delivery lifetime, so a safe retry can never become a
/// second harness turn.
public struct ChatPendingTurn: Sendable, Equatable {
    public enum DeliveryState: Sendable, Equatable {
        /// The POST is in flight, or the hub accepted it and the stream has
        /// not yet confirmed it.
        case sending
        /// The response was lost or inconclusive. Retrying this exact ID is
        /// safe because the server treats it as an idempotency key.
        case awaitingConfirmation
        /// A model recreated after an interrupted delivery cannot establish
        /// whether the server's bounded idempotency cache still remembers the
        /// original request. It requires an explicit user decision instead of
        /// replaying the saved ID.
        case needsManualReview
    }

    public let text: String
    public let attachments: [TurnAttachment]
    public let clientTurnID: String
    public fileprivate(set) var deliveryState: DeliveryState

    public var statusText: String {
        switch deliveryState {
        case .sending:
            return "Sending…"
        case .awaitingConfirmation:
            return "Still confirming delivery"
        case .needsManualReview:
            return "Delivery needs review"
        }
    }

    public var canRetry: Bool {
        deliveryState == .awaitingConfirmation
    }

    /// Shown beside Retry. Carried here rather than in the shared `hint`
    /// because approve, interrupt, terminate and an unauthorized stream event
    /// all clear that field; a delivery the user still has to resolve must
    /// outlive them.
    public var retryMessage: String {
        "Couldn’t confirm delivery. Retry is safe."
    }

    public var manualReviewMessage: String {
        "This delivery is held for review. Check delivery, copy it to a draft, or discard it locally."
    }
}

/// One admitted recovery write. Its generation identifies the credential
/// namespace that was current when the write entered the store boundary.
public struct ChatRecoveryWriteAdmission: Sendable {
    fileprivate let generation: Int
}

/// App-owned invalidation boundary for chat-recovery writes. A credential
/// replacement or sign out first retires the active generation, then drains
/// writes that already entered the store boundary before it purges the old
/// namespace. This prevents an in-flight actor save from recreating a record
/// after cleanup has completed.
@MainActor
public final class ChatRecoveryWriteGate {
    public private(set) var generation = 0
    private var activeWrites: [Int: Int] = [:]
    private var drainWaiters: [Int: [CheckedContinuation<Void, Never>]] = [:]

    public init() {}

    /// Retires the active generation synchronously, so future writes cannot
    /// enter its namespace. The caller must then drain the returned value
    /// before erasing that namespace.
    @discardableResult
    public func invalidate() -> Int {
        let retiredGeneration = generation
        generation &+= 1
        return retiredGeneration
    }

    /// Enters the durable store boundary for the current credential. An
    /// admission must be released after the awaited store operation returns.
    public func admit(generation: Int) -> ChatRecoveryWriteAdmission? {
        guard generation == self.generation else { return nil }
        activeWrites[generation, default: 0] += 1
        return ChatRecoveryWriteAdmission(generation: generation)
    }

    public func release(_ admission: ChatRecoveryWriteAdmission) {
        let generation = admission.generation
        guard let active = activeWrites[generation] else { return }
        if active > 1 {
            activeWrites[generation] = active - 1
            return
        }
        activeWrites.removeValue(forKey: generation)
        let waiters = drainWaiters.removeValue(forKey: generation) ?? []
        waiters.forEach { $0.resume() }
    }

    /// Waits until every write admitted before `invalidate()` finished. New
    /// writes for that retired generation are already barred.
    public func drain(_ retiredGeneration: Int) async {
        guard activeWrites[retiredGeneration, default: 0] > 0 else { return }
        await withCheckedContinuation { continuation in
            drainWaiters[retiredGeneration, default: []].append(continuation)
        }
    }

    /// Drains every generation that is no longer current. Sign out erases the
    /// recovery root, so it must cover an older write that a credential
    /// replacement was already draining when sign out began.
    public func drainAllRetired() async {
        let retiredGenerations = activeWrites.keys.filter { $0 != generation }
        for retiredGeneration in retiredGenerations {
            await drain(retiredGeneration)
        }
    }
}

/// All chat logic for one structured-session conversation: ingesting the
/// resumable `MessageStream`, tracking the newest unanswered approval
/// request, and issuing turns/approvals/interrupt/terminate against
/// `DroverClient`. Kept free of SwiftUI so it's unit-testable without sockets
/// (see `ingest(_:)`) — `ChatView` and friends only render this state.
@MainActor
@Observable
public final class ChatModel {
    private enum RecoveryFailureOrigin {
        case save
        case load
        case unavailable
    }

    private let client: DroverClient
    private let sessionID: String
    private let stream: MessageStream
    private let recoveryStore: (any ChatRecoveryPersisting)?
    private let recoveryKey: ChatRecoveryKey?
    private let recoveryWriteGate: ChatRecoveryWriteGate?
    private let recoveryGeneration: Int
    public let runPreferences: HarnessModelCatalogState

    public private(set) var messages: [HarnessMessage]
    public private(set) var isConnected = false
    /// Latches true on the first successful connection — lets the UI
    /// suppress its "reconnecting" indicator during the initial connect
    /// (when there is nothing to *re*-connect to yet).
    public private(set) var hasConnectedOnce = false
    /// Failed first connects, and the reason for the newest one. Counting
    /// stops the moment a session attaches: from then on a dropped socket is
    /// the reconnecting pill's business, over a transcript already on screen.
    private var coldOpen = ColdOpenTracker()
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
    /// A turn that has been removed from the composer and is waiting for its
    /// exact `user_input` stream echo. It is the model-level send gate: while
    /// present, normal sends cannot create an accidental duplicate.
    public private(set) var pendingTurn: ChatPendingTurn?
    /// A storage failure means a new unresolved turn cannot be protected
    /// locally. The composer remains editable, but sending is disabled until
    /// protected storage is usable again.
    public private(set) var recoveryStatusMessage: String?
    /// A save retry is available only for a failed write or removal from an
    /// inspected in-memory composition. A failed read must never be turned
    /// into a blank save or removal of a record the model could not inspect.
    public var canRetryRecoverySave: Bool {
        recoveryFailureOrigin == .save && canPersistRecovery
    }
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
    /// the final rendered row when a tool result attaches to a prior step.
    public var latestRowID: String? {
        if let rowIDCache, rowIDCache.version == messagesVersion { return rowIDCache.id }
        let id = TranscriptItem.latestRowID(of: messages)
        rowIDCache = (messagesVersion, id)
        return id
    }

    /// The bottom-most row in visual transcript order. A newly-arrived raw
    /// event can update an earlier folded row, so callers that need the final
    /// rendered row use this rather than `latestRowID`.
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
    public var composerText = "" {
        didSet {
            parkDeferredTurnForManualReviewIfDraftIsPresent()
            scheduleRecoveryCheckpoint()
        }
    }
    /// Images picked in the composer, waiting to ride the next turn.
    public var pendingAttachments: [TurnAttachment] = [] {
        didSet {
            parkDeferredTurnForManualReviewIfDraftIsPresent()
            scheduleRecoveryCheckpoint()
        }
    }
    /// Whether a new composer submission is allowed. A typed next message is
    /// preserved while the previous one confirms, but must not leapfrog or
    /// duplicate that delivery.
    public var canSendTurn: Bool {
        !isSending && pendingTurn == nil && recoveryStatusMessage == nil && canPersistRecovery
    }
    /// Text the user sent while the harness was mid-turn (codex/gemini
    /// reject overlapping turns with 409 "turn already in flight"). Held
    /// here and auto-dispatched when the turn-complete status arrives.
    /// Claude never 409s on overlap (mid-turn input is steering), so this
    /// only ever fills for harnesses that actually reject.
    public private(set) var queuedTurn: String?
    /// Attachments that were on a turn deferred by the same 409.
    private var queuedAttachments: [TurnAttachment] = []
    /// The client-generated ID from the rejected request. Deferred-only
    /// recovery does not persist it, but it lets a later editable composition
    /// hold this exact delivery for review instead of replacing either turn.
    private var queuedClientTurnID: String?
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
    /// How long an accepted turn may sit unechoed before the UI offers Retry.
    private let deliveryConfirmationTimeout: Duration
    private nonisolated(unsafe) var deliveryTimeoutTask: Task<Void, Never>?
    private nonisolated(unsafe) var recoveryCheckpointTask: Task<Void, Never>?
    private var recoveryCheckpointGeneration = 0
    private var isApplyingRecoveredState = false
    private var recoveryFailureOrigin: RecoveryFailureOrigin?

    public convenience init(
        client: DroverClient,
        sessionID: String,
        harness: String? = nil,
        store: HarnessModelCatalogStore = HarnessModelCatalogStore(),
        recap: String? = nil,
        recapSourceSeq: Int? = nil,
        recapPollInterval: Duration = .seconds(1),
        recapPollAttempts: Int = 30,
        deliveryConfirmationTimeout: Duration = .seconds(20),
        recoveryStore: (any ChatRecoveryPersisting)? = nil,
        recoveryWriteGate: ChatRecoveryWriteGate? = nil,
        recoveryGeneration: Int = 0,
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
            deliveryConfirmationTimeout: deliveryConfirmationTimeout,
            recoveryStore: recoveryStore,
            recoveryWriteGate: recoveryWriteGate,
            recoveryGeneration: recoveryGeneration,
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
        deliveryConfirmationTimeout: Duration = .seconds(20),
        recoveryStore: (any ChatRecoveryPersisting)? = nil,
        recoveryWriteGate: ChatRecoveryWriteGate? = nil,
        recoveryGeneration: Int = 0,
        streamFactory: ((DroverClient, String) -> MessageStream)? = nil
    ) {
        self.client = client
        self.sessionID = sessionID
        self.recoveryStore = recoveryStore
        self.recoveryWriteGate = recoveryWriteGate
        self.recoveryGeneration = recoveryGeneration
        if let credentialBindingID = client.credentialBindingID {
            self.recoveryKey = ChatRecoveryKey(
                serverURL: client.config.baseURL,
                credentialBindingID: credentialBindingID,
                sessionID: sessionID
            )
        } else {
            self.recoveryKey = nil
        }
        self.runPreferences = HarnessModelCatalogState(client: client, store: store)
        self.messages = initialMessages
        self.harnessPresentation = HarnessPresentation(harness ?? "")
        self.recap = recap
        self.recapSourceSeq = recapSourceSeq
        self.recapPollInterval = recapPollInterval
        self.recapPollAttempts = recapPollAttempts
        self.deliveryConfirmationTimeout = deliveryConfirmationTimeout
        let factory = streamFactory ?? { c, s in MessageStream(client: c, sessionID: s) }
        self.stream = factory(client, sessionID)
        if !canPersistRecovery {
            recoveryStatusMessage = "Chat recovery is unavailable. Check local storage in Settings."
            recoveryFailureOrigin = .unavailable
        }
        rebuildApprovals()
    }

    deinit {
        // Belt one: whoever drops the model (e.g. SwiftUI discarding
        // ChatView's subtree on navigation pop) must not leak a
        // still-streaming socket even if `stop()` wasn't called explicitly.
        pumpTask?.cancel()
        recapRefreshTask?.cancel()
        deliveryTimeoutTask?.cancel()
        recoveryCheckpointTask?.cancel()
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

    /// Why a first connect has not landed, once it has failed often enough to
    /// owe an explanation — the cold open's third state (#170). Nil during a
    /// normal first connect however slow, and nil for good once a session has
    /// attached.
    public var coldOpenFailure: String? { coldOpen.detail }

    /// Reopens a cold open that gave up.
    ///
    /// A full stop/start rather than clearing the message and waiting:
    /// `MessageStream` keeps its own doubling backoff capped at 30s, so by the
    /// time a failure has been read and tapped, the next scheduled attempt can
    /// be most of a minute away. Tearing the pump down and starting a fresh
    /// one puts the attempt on the user's schedule instead of the backoff's.
    public func retryConnect() {
        stop()
        coldOpen.reset()
        start()
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
            confirmPendingTurn(message)
        case .history(let messages, _):
            mergeHistory(messages)
            // Losing a response usually means losing the socket too, so the
            // echo that confirms a pending delivery often arrives here
            // rather than as a live message.
            messages.forEach(confirmPendingTurn)
        case .connection(let connected):
            isConnected = connected
            if connected {
                hasConnectedOnce = true
                coldOpen.reset()
            }
        case .connectFailed(let reason):
            // Only a cold open needs this. After the first attach the pill
            // already covers a drop, and escalating to a full unreachable
            // state would hide history the user can still read.
            guard !hasConnectedOnce else { break }
            coldOpen.noteFailure(reason)
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
        guard canSendTurn else { return }
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = pendingAttachments
        guard !text.isEmpty || !images.isEmpty else { return }
        let clientTurnID = UUID().uuidString
        let turn = ChatPendingTurn(
            text: text,
            attachments: images,
            clientTurnID: clientTurnID,
            deliveryState: .sending
        )
        // This happens before the first await. The composer never continues
        // to advertise the words as unsent while the request is slow, and a
        // second tap hits the model-level pending-turn gate above.
        composerText = ""
        pendingAttachments = []
        pendingTurn = turn
        hint = nil
        do {
            try await persistRecoveryState()
        } catch {
            if hasEditableComposition {
                holdForManualReview(
                    turn,
                    message: "Saving the earlier delivery failed. It is held for review; this draft is preserved."
                )
            } else {
                pendingTurn = nil
                composerText = text
                pendingAttachments = images
            }
            recordRecoveryFailure(.save)
            return
        }
        // A sign out or replacement can retire this credential while its
        // admitted save is suspended in the recovery actor. It is durable
        // enough to drain safely, but no longer authorized to POST.
        guard canPersistRecovery,
              pendingTurn?.clientTurnID == turn.clientTurnID
        else { return }
        await submitPendingTurn(turn)
    }

    /// Admits an attachment only after the complete next recovery snapshot is
    /// durable. Picker selection is otherwise rejected before it changes the
    /// editable composition, which keeps the composer honest about what can
    /// survive a recreation.
    public func addAttachmentIfRecoverable(_ attachment: TurnAttachment) async -> Bool {
        guard pendingAttachments.count < 4, recoveryStatusMessage == nil else { return false }
        let updatedAttachments = pendingAttachments + [attachment]
        do {
            try await persistRecoveryState(
                recoverySnapshot(
                    draftAttachments: updatedAttachments,
                    parkingDeferredTurnForManualReview: true
                )
            )
            guard canPersistRecovery else { return false }
            pendingAttachments = updatedAttachments
            return true
        } catch {
            recordRecoveryFailure(.save)
            return false
        }
    }

    /// Commits the latest debounced composition at a navigation or scene
    /// boundary. Normal edits still coalesce for 150ms; leaving the chat must
    /// not cancel their final checkpoint before it reaches protected storage.
    public func flushRecoveryCheckpoint() async {
        guard !isApplyingRecoveredState,
              recoveryStatusMessage == nil,
              canPersistRecovery
        else { return }
        do {
            try await persistRecoveryState()
        } catch {
            recordRecoveryFailure(.save)
        }
    }

    /// Stops the stream immediately, then keeps this model alive long enough
    /// to commit its final editable state before SwiftUI releases the view.
    public func prepareForDeparture() async {
        stop()
        await flushRecoveryCheckpoint()
    }

    /// Retries only a failed write for the current inspected composition. It
    /// never calls a turn submission path, and it leaves unavailable or
    /// unread recovery records alone.
    public func retryRecoverySave() async {
        guard canRetryRecoverySave else { return }
        do {
            try await persistRecoveryState()
            guard canPersistRecovery else {
                throw ChatRecoveryError.storageUnavailable
            }
            recoveryFailureOrigin = nil
            recoveryStatusMessage = nil
            if pendingTurn?.deliveryState != .needsManualReview {
                hint = nil
            }
            // An edit made while this retry was suspended could not schedule
            // its usual checkpoint while the save error was latched. Take a
            // fresh snapshot after clearing that latch so it is not stranded.
            scheduleRecoveryCheckpoint()
        } catch {
            recordRecoveryFailure(.save)
        }
    }

    /// Replays the same client turn ID after a transport-level ambiguity.
    /// The hub returns the original accepted turn without dispatching it
    /// again, so recovery is safe even if the first response was lost.
    public func retryPendingTurn() async {
        guard var pendingTurn, pendingTurn.canRetry, !isSending else { return }
        pendingTurn.deliveryState = .sending
        self.pendingTurn = pendingTurn
        cancelDeliveryConfirmationTimeout()
        hint = nil
        do {
            try await persistRecoveryState()
        } catch {
            pendingTurn.deliveryState = .awaitingConfirmation
            self.pendingTurn = pendingTurn
            recordRecoveryFailure(.save)
            return
        }
        guard canPersistRecovery,
              self.pendingTurn?.clientTurnID == pendingTurn.clientTurnID
        else { return }
        await submitPendingTurn(pendingTurn)
    }

    /// Rehydrates the exact authenticated session record before the stream
    /// starts its normal history catch-up. A persisted send is never replayed:
    /// only a later exact echo can clear this manual-review state.
    public func restoreRecovery() async {
        guard let recoveryStore, let recoveryKey else { return }
        do {
            guard let snapshot = try await recoveryStore.load(for: recoveryKey) else { return }
            isApplyingRecoveredState = true
            defer { isApplyingRecoveredState = false }
            if composerText.isEmpty, pendingAttachments.isEmpty {
                if let deferredTurn = snapshot.deferredTurn {
                    composerText = deferredTurn.text
                    pendingAttachments = deferredTurn.attachments.map(turnAttachment)
                    hint = "A deferred draft was recovered. Review it before sending."
                } else {
                    composerText = snapshot.draftText
                    pendingAttachments = snapshot.draftAttachments.map(turnAttachment)
                }
            }
            if let recoveredPending = snapshot.pendingTurn, pendingTurn == nil {
                pendingTurn = ChatPendingTurn(
                    text: recoveredPending.text,
                    attachments: recoveredPending.attachments.map(turnAttachment),
                    clientTurnID: recoveredPending.clientTurnID.uuidString,
                    deliveryState: .needsManualReview
                )
            }
        } catch {
            recordRecoveryFailure(.load)
        }
    }

    /// Starts the ordinary catch-up path only. It intentionally does not call
    /// `sendTurn` or `retryPendingTurn`, because a restored ID has no durable
    /// server receipt proving a replay remains safe.
    public func checkPendingDelivery() {
        guard pendingTurn?.deliveryState == .needsManualReview else { return }
        // ChatView already starts its normal catch-up after restoration. An
        // explicit check must therefore replace an active pump rather than
        // relying on `start()`'s intentional idempotence. This touches only
        // the stream; it never calls either turn-submission path.
        stop()
        start()
    }

    /// Moves a restored, unconfirmed delivery into the editable composer. It
    /// never submits the saved ID, and refuses to replace words the user is
    /// already editing without an explicit choice outside this action.
    public func copyPendingTurnToDraft() async {
        guard let pendingTurn,
              pendingTurn.deliveryState == .needsManualReview
        else { return }
        guard composerText.isEmpty, pendingAttachments.isEmpty else {
            hint = "Keep or clear the current draft before copying this delivery."
            return
        }
        let copiedTurn = pendingTurn
        isApplyingRecoveredState = true
        composerText = copiedTurn.text
        pendingAttachments = copiedTurn.attachments
        self.pendingTurn = nil
        isApplyingRecoveredState = false
        do {
            try await persistRecoveryState()
            hint = "Copied to draft. Review it before sending."
        } catch {
            isApplyingRecoveredState = true
            composerText = ""
            pendingAttachments = []
            self.pendingTurn = copiedTurn
            isApplyingRecoveredState = false
            recordRecoveryFailure(.save)
        }
    }

    /// Removes only the local unresolved-delivery record. The caller is
    /// responsible for the destructive UI confirmation.
    public func discardPendingTurn() async {
        guard let pendingTurn,
              pendingTurn.deliveryState == .needsManualReview
        else { return }
        self.pendingTurn = nil
        do {
            try await persistRecoveryState()
            hint = nil
        } catch {
            self.pendingTurn = pendingTurn
            recordRecoveryFailure(.save)
        }
    }

    /// The hub echoing a `user_input` with this client's turn ID is the sole
    /// confirmation that removes the local pending row. Text matches are not
    /// enough: another device may have sent identical words.
    private func confirmPendingTurn(_ message: HarnessMessage) {
        guard message.type == .userInput,
              let pendingTurn,
              message.turnID == pendingTurn.clientTurnID
        else { return }
        self.pendingTurn = nil
        cancelDeliveryConfirmationTimeout()
        hint = nil
        scheduleRecoveryCheckpoint()
    }

    /// The hub accepting the POST is not delivery: the echo that clears the
    /// pending row travels harnessd to hub to socket, and every hop can drop
    /// it. Without this the composer stays gated on a turn nothing will ever
    /// resolve, and the only recovery is killing the app. Degrading to
    /// `.awaitingConfirmation` costs at worst one replayed submission, which
    /// the server treats as an idempotent no-op.
    private func startDeliveryConfirmationTimeout(for clientTurnID: String) {
        cancelDeliveryConfirmationTimeout()
        let timeout = deliveryConfirmationTimeout
        deliveryTimeoutTask = Task { [weak self] in
            try? await Task.sleep(for: timeout)
            guard !Task.isCancelled else { return }
            await self?.markDeliveryUnconfirmed(clientTurnID)
        }
    }

    private func cancelDeliveryConfirmationTimeout() {
        deliveryTimeoutTask?.cancel()
        deliveryTimeoutTask = nil
    }

    private func markDeliveryUnconfirmed(_ clientTurnID: String) {
        guard var turn = pendingTurn,
              turn.clientTurnID == clientTurnID,
              turn.deliveryState == .sending,
              !isSending
        else { return }
        turn.deliveryState = .awaitingConfirmation
        pendingTurn = turn
        scheduleRecoveryCheckpoint()
    }

    private func submitPendingTurn(_ turn: ChatPendingTurn) async {
        guard pendingTurn?.clientTurnID == turn.clientTurnID else { return }
        isSending = true
        defer { isSending = false }
        let preferences = turnPreferences
        do {
            _ = try await client.sendTurn(
                sessionID: sessionID,
                text: turn.text,
                images: turn.attachments,
                model: preferences.model,
                thinkingEffort: preferences.thinking,
                clientTurnID: turn.clientTurnID
            )
            // A stream echo can arrive before this HTTP response. Do not
            // restore the already-confirmed pending row in that race.
            if pendingTurn?.clientTurnID == turn.clientTurnID {
                hint = nil
                startDeliveryConfirmationTimeout(for: turn.clientTurnID)
            }
        } catch DroverError.conflict(let message) where message == "turn already in flight" {
            guard pendingTurn?.clientTurnID == turn.clientTurnID else { return }
            cancelDeliveryConfirmationTimeout()
            if hasEditableComposition {
                holdForManualReview(
                    turn,
                    message: "The server was busy. The earlier delivery is held for review; this draft is preserved."
                )
            } else {
                pendingTurn = nil
                queue(turn)
            }
            do {
                try await persistRecoveryState()
            } catch {
                recordRecoveryFailure(.save)
            }
        } catch {
            guard pendingTurn?.clientTurnID == turn.clientTurnID else { return }
            if isAmbiguousSendFailure(error) {
                pendingTurn?.deliveryState = .awaitingConfirmation
                hint = "Couldn’t confirm delivery. Retry is safe."
                scheduleRecoveryCheckpoint()
            } else {
                pendingTurn = nil
                cancelDeliveryConfirmationTimeout()
                restoreRejectedTurn(turn)
                applyHint(for: error, action: "send")
            }
        }
    }

    private func queue(_ turn: ChatPendingTurn) {
        if queuedTurn == nil, queuedAttachments.isEmpty {
            queuedClientTurnID = turn.clientTurnID
        }
        if !turn.text.isEmpty {
            queuedTurn = queuedTurn.map { "\($0)\n\(turn.text)" } ?? turn.text
        }
        queuedAttachments.append(contentsOf: turn.attachments)
        hint = "Queued — sends when the current response finishes."
        scheduleRecoveryCheckpoint()
    }

    /// A 409 means this delivery was not accepted while the harness was busy.
    /// If the user is already composing a distinct next turn, preserve both
    /// bounded compositions: the existing pending/manual-review slot keeps the
    /// original ID and the composer remains the newer draft. Neither is merged
    /// or sent automatically.
    private func holdForManualReview(_ turn: ChatPendingTurn, message: String) {
        var heldTurn = turn
        heldTurn.deliveryState = .needsManualReview
        pendingTurn = heldTurn
        queuedTurn = nil
        queuedAttachments = []
        queuedClientTurnID = nil
        hint = message
    }

    /// An earlier 409 may have already entered the deferred queue before the
    /// user begins a new draft. Move that older composition into manual review
    /// before the next checkpoint so the snapshot can retain both records.
    private func parkDeferredTurnForManualReviewIfDraftIsPresent() {
        guard !isApplyingRecoveredState,
              hasEditableComposition,
              let turn = deferredTurnForManualReview()
        else { return }
        holdForManualReview(
            turn,
            message: "The server was busy. The earlier delivery is held for review; this draft is preserved."
        )
    }

    private func deferredTurnForManualReview() -> ChatPendingTurn? {
        guard let clientTurnID = queuedClientTurnID,
              queuedTurn != nil || !queuedAttachments.isEmpty
        else { return nil }
        return ChatPendingTurn(
            text: queuedTurn ?? "",
            attachments: queuedAttachments,
            clientTurnID: clientTurnID,
            deliveryState: .needsManualReview
        )
    }

    private var hasEditableComposition: Bool {
        !composerText.isEmpty || !pendingAttachments.isEmpty
    }

    private func restoreRejectedTurn(_ turn: ChatPendingTurn) {
        composerText = composerText.isEmpty ? turn.text : "\(turn.text)\n\(composerText)"
        pendingAttachments = turn.attachments + pendingAttachments
    }

    private func isAmbiguousSendFailure(_ error: Error) -> Bool {
        guard let error = error as? DroverError else { return true }
        switch error {
        case .transport, .decoding:
            return true
        case .httpStatus(let status, _):
            return status >= 500
        case .unauthorized, .conflict, .badRequest, .unavailable:
            return false
        }
    }

    private func persistRecoveryState(
        _ snapshot: ChatRecoverySnapshot? = nil
    ) async throws {
        guard let recoveryStore,
              let recoveryKey,
              let recoveryWriteGate,
              canPersistRecovery
        else {
            throw ChatRecoveryError.storageUnavailable
        }
        recoveryCheckpointGeneration &+= 1
        recoveryCheckpointTask?.cancel()
        recoveryCheckpointTask = nil
        try await Self.persist(
            snapshot ?? recoverySnapshot(),
            to: recoveryStore,
            for: recoveryKey,
            recoveryWriteGate: recoveryWriteGate,
            recoveryGeneration: recoveryGeneration
        )
    }

    private func recoverySnapshot(
        draftText: String? = nil,
        draftAttachments: [TurnAttachment]? = nil,
        parkingDeferredTurnForManualReview: Bool = false
    ) -> ChatRecoverySnapshot {
        let draftText = draftText ?? composerText
        let draftAttachments = draftAttachments ?? pendingAttachments
        let parkedTurn = parkingDeferredTurnForManualReview
            ? deferredTurnForManualReview()
            : nil
        let deferredTurn = parkedTurn == nil && (queuedTurn != nil || !queuedAttachments.isEmpty)
            ? RecoveredDeferredTurn(
                text: queuedTurn ?? "",
                attachments: queuedAttachments.map(recoveredAttachment)
            )
            : nil
        return ChatRecoverySnapshot(
            draftText: deferredTurn == nil ? draftText : "",
            draftAttachments: deferredTurn == nil ? draftAttachments.map(recoveredAttachment) : [],
            deferredTurn: deferredTurn,
            pendingTurn: (parkedTurn ?? pendingTurn).flatMap(recoveredPendingTurn)
        )
    }

    private func scheduleRecoveryCheckpoint() {
        guard !isApplyingRecoveredState,
              recoveryStatusMessage == nil,
              let recoveryStore,
              let recoveryKey,
              canPersistRecovery
        else { return }
        recoveryCheckpointGeneration &+= 1
        let generation = recoveryCheckpointGeneration
        recoveryCheckpointTask?.cancel()
        let snapshot = recoverySnapshot()
        recoveryCheckpointTask = Task { [weak self, recoveryStore, recoveryKey] in
            do {
                try await Task.sleep(for: .milliseconds(150))
                guard !Task.isCancelled,
                      let self,
                      self.recoveryCheckpointGeneration == generation,
                      self.canPersistRecovery
                else { return }
                guard let recoveryWriteGate = self.recoveryWriteGate else { return }
                try await Self.persist(
                    snapshot,
                    to: recoveryStore,
                    for: recoveryKey,
                    recoveryWriteGate: recoveryWriteGate,
                    recoveryGeneration: self.recoveryGeneration
                )
                guard !Task.isCancelled,
                      self.recoveryCheckpointGeneration == generation
                else { return }
                self.recoveryCheckpointTask = nil
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled,
                      let self,
                      self.recoveryCheckpointGeneration == generation
                else { return }
                self.recoveryCheckpointTask = nil
                self.recordRecoveryFailure(.save)
            }
        }
    }

    private static func persist(
        _ snapshot: ChatRecoverySnapshot,
        to recoveryStore: any ChatRecoveryPersisting,
        for recoveryKey: ChatRecoveryKey,
        recoveryWriteGate: ChatRecoveryWriteGate,
        recoveryGeneration: Int
    ) async throws {
        guard let admission = recoveryWriteGate.admit(generation: recoveryGeneration) else {
            throw ChatRecoveryError.storageUnavailable
        }
        defer { recoveryWriteGate.release(admission) }
        if snapshot.draftText.isEmpty,
           snapshot.draftAttachments.isEmpty,
           snapshot.deferredTurn == nil,
           snapshot.pendingTurn == nil {
            try await recoveryStore.remove(for: recoveryKey)
        } else {
            try await recoveryStore.save(snapshot, for: recoveryKey)
        }
    }

    private func recordRecoveryFailure(_ origin: RecoveryFailureOrigin) {
        let message: String
        switch origin {
        case .save:
            message = "Chat recovery could not protect this draft. Check local storage in Settings."
        case .load:
            message = "Chat recovery could not be read. Existing saved drafts were left unchanged."
        case .unavailable:
            message = "Chat recovery is unavailable. Check local storage in Settings."
        }
        recoveryFailureOrigin = origin
        recoveryStatusMessage = message
        if pendingTurn?.deliveryState != .needsManualReview {
            hint = message
        }
    }

    private var canPersistRecovery: Bool {
        guard recoveryStore != nil, recoveryKey != nil, let recoveryWriteGate else { return false }
        return recoveryWriteGate.generation == recoveryGeneration
    }

    private func recoveredAttachment(_ attachment: TurnAttachment) -> RecoveredTurnAttachment {
        RecoveredTurnAttachment(mediaType: attachment.mediaType, data: attachment.data)
    }

    private func recoveredPendingTurn(_ turn: ChatPendingTurn) -> RecoveredPendingTurn? {
        guard let clientTurnID = UUID(uuidString: turn.clientTurnID) else { return nil }
        return RecoveredPendingTurn(
            clientTurnID: clientTurnID,
            text: turn.text,
            attachments: turn.attachments.map(recoveredAttachment)
        )
    }

    private func turnAttachment(_ attachment: RecoveredTurnAttachment) -> TurnAttachment {
        TurnAttachment(mediaType: attachment.mediaType, data: attachment.data)
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
        queuedClientTurnID = nil
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
        guard pendingTurn == nil else {
            queuedTurn = queuedTurn.map { "\(text)\n\($0)" } ?? text
            queuedAttachments = images + queuedAttachments
            return
        }
        let turn = ChatPendingTurn(
            text: text,
            attachments: images,
            clientTurnID: UUID().uuidString,
            deliveryState: .sending
        )
        pendingTurn = turn
        do {
            try await persistRecoveryState()
        } catch {
            if hasEditableComposition {
                holdForManualReview(
                    turn,
                    message: "Saving the earlier delivery failed. It is held for review; this draft is preserved."
                )
            } else {
                pendingTurn = nil
                queuedTurn = text
                queuedAttachments = images
                queuedClientTurnID = turn.clientTurnID
            }
            recordRecoveryFailure(.save)
            return
        }
        guard canPersistRecovery,
              pendingTurn?.clientTurnID == turn.clientTurnID
        else { return }
        await submitPendingTurn(turn)
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
        case DroverError.httpStatus(504, _):
            // The hub stopped waiting; the host did not stop working. A
            // create can outlive the hub's budget while it cuts a per-session
            // worktree, and the session may then exist with only the hub's
            // copy missing. "Try again" is the one instruction that turns
            // that into two sessions, so this says the opposite.
            hint = "The host is still working on it — check your sessions before trying again."
        default:
            hint = "Could not \(action) — try again."
        }
    }
}
