import SwiftUI
import DroverKit

typealias ChatModelFactory = @MainActor (DroverClient, String, String?) -> ChatModel

/// A view-only ID namespace keeps the post-clearance destination impossible
/// to confuse with a raw or folded transcript row ID (both are strings).
enum ChatTranscriptScrollTarget: Hashable {
    case visualTail
    case pendingTurn

    static func bottomDestination(
        for items: [TranscriptItem], hasPendingTurn: Bool = false
    ) -> AnyHashable? {
        if hasPendingTurn { return AnyHashable(Self.pendingTurn) }
        return items.isEmpty ? nil : AnyHashable(Self.visualTail)
    }
}

struct ChatHeaderContent: View {
    let title: String
    let metadata: String

    var body: some View {
        VStack(spacing: 1) {
            Text(title)
                .font(.headline)
                .lineLimit(1)
                .accessibilityIdentifier("chat-recap-title")
            Text(metadata)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .accessibilityIdentifier("chat-header-metadata")
        }
    }
}

/// The structured-session chat screen: a scrolling transcript (auto-scrolls
/// to the newest message), a "reconnecting…" pill while the stream is down,
/// a pinned decision block when the harness is blocked on you, and a
/// composer. Interrupt/terminate live in the toolbar. All state and network
/// calls are delegated to `ChatModel` — this view only renders it.
struct ChatView: View {
    private let client: DroverClient
    private let recoveryStore: (any ChatRecoveryPersisting)?
    private let recoveryWriteGate: ChatRecoveryWriteGate
    private let recoveryGeneration: Int
    private let chatModelFactory: ChatModelFactory?
    @State private var model: ChatModel
    @State private var showTerminateConfirm = false
    @State private var showDiscardPendingConfirm = false
    @State private var handoffSession: HandoffSession?
    @State private var pendingScroll: Task<Void, Never>?
    @State private var scrollGeneration = 0
    @State private var pendingPrependScroll: Task<Void, Never>?
    @State private var prependScrollGeneration = 0
    @State private var followState = TranscriptFollowState()
    @State private var scrollPosition = ScrollPosition(y: 0)
    @State private var rowFrames: [String: CGRect] = [:]
    @State private var transcriptOffset: CGFloat = 0
    @State private var scrollPhase: ScrollPhase = .idle
    /// Flipped by a timer once the cold open has lasted long enough to be
    /// worth acknowledging. A local open beats it and the screen stays quiet.
    @State private var coldOpenIsSlow = false
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    init(
        client: DroverClient,
        sessionID: String,
        harness: String? = nil,
        recap: String? = nil,
        recapSourceSeq: Int? = nil,
        recoveryStore: (any ChatRecoveryPersisting)?,
        recoveryWriteGate: ChatRecoveryWriteGate,
        recoveryGeneration: Int,
        chatModelFactory: ChatModelFactory? = nil
    ) {
        self.client = client
        self.recoveryStore = recoveryStore
        self.recoveryWriteGate = recoveryWriteGate
        self.recoveryGeneration = recoveryGeneration
        self.chatModelFactory = chatModelFactory
        _model = State(initialValue: chatModelFactory?(client, sessionID, harness) ?? ChatModel(
            client: client,
            sessionID: sessionID,
            harness: harness,
            recap: recap,
            recapSourceSeq: recapSourceSeq,
            recoveryStore: recoveryStore,
            recoveryWriteGate: recoveryWriteGate,
            recoveryGeneration: recoveryGeneration
        ))
    }

    var body: some View {
        @Bindable var model = model

        VStack(spacing: 0) {
            // Only once a connection has existed is a disconnect worth a
            // "Reconnecting…" pill; during the initial connect it would just
            // flash misleading chrome.
            if model.hasConnectedOnce && !model.isConnected {
                ReconnectingPill(accessibilityID: "chat-reconnecting")
            }

            // A cold open assembles its window over four serialized round
            // trips, so the transcript area is genuinely empty until the
            // first of them lands. Overlaid rather than swapped in: the
            // transcript keeps its geometry, so nothing jumps when the
            // messages arrive underneath.
            transcript
                .overlay {
                    // The fade belongs to the indicator, not to the
                    // transcript. Applied one level up it wrapped the whole
                    // message list in an implicit animation keyed on
                    // `hasConnectedOnce` — which flips at exactly the moment
                    // the first rows land, so the insertion animated
                    // underneath the initial scroll-to-bottom and parked the
                    // ScrollView past its own content. The transcript was
                    // fully populated (189 messages, 63 rows, confirmed on
                    // device) and the screen was blank.
                    switch ColdOpenTracker.state(
                        hasConnectedOnce: model.hasConnectedOnce,
                        failure: model.coldOpenFailure,
                        elapsed: coldOpenIsSlow ? ColdOpenTracker.appearAfter : 0
                    ) {
                    case .quiet:
                        EmptyView()
                    case .connecting:
                        DroverLoadingMarkView()
                            .transition(.opacity)
                            .animation(.easeIn(duration: 0.2), value: coldOpenIsSlow)
                    case .unreachable(let detail):
                        // The spinner is replaced, not joined: leaving it up
                        // beside the message would go on claiming progress
                        // that is not happening.
                        ColdOpenFailureView(detail: detail,
                                            accessibilityID: "chat-cold-open-failed") {
                            model.retryConnect()
                        }
                    }
                }

            // Read once: `artifacts` is cached, but two reads still cost two
            // dictionary lookups and obscure that this is one value.
            let artifacts = model.artifacts
            if !artifacts.isEmpty {
                ArtifactRows(artifacts: artifacts)
            }

            // Allow/Deny only when the host advertises approvals. Otherwise the
            // request is still shown as pending, with why iOS can't answer it.
            if let approval = model.pendingApproval {
                if model.controls.showsApprovals {
                    DecisionBlock(
                        approval: approval,
                        isBusy: model.isAnswering,
                        onApprove: { Task { await model.approve("allow") } },
                        onDeny: { Task { await model.approve("deny") } }
                    )
                } else if let reason = model.controls.approvalsUnavailableReason {
                    ChatHintBanner(reason)
                        .accessibilityIdentifier("approval-unavailable")
                }
            }

            // Deliberately not gated on `hint`: approve, interrupt, terminate
            // and an unauthorized stream event all clear it, and an
            // unconfirmed delivery must keep its Retry through any of them.
            if let pendingTurn = model.pendingTurn,
                      pendingTurn.canRetry,
                      model.recoveryStatusMessage == nil {
                ChatHintBanner(pendingTurn.retryMessage, actionTitle: "Retry") {
                    Task { await model.retryPendingTurn() }
                }
            } else if let pendingTurn = model.pendingTurn,
                      pendingTurn.deliveryState == .needsManualReview {
                pendingDeliveryReview(pendingTurn)
            } else if let recoveryStatusMessage = model.recoveryStatusMessage {
                recoveryStatusBanner(recoveryStatusMessage)
            } else if let hint = model.hint {
                ChatHintBanner(hint)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                if model.hasConnectedOnce, model.isConnected,
                   model.pendingTurn == nil, model.pendingApproval == nil,
                   model.activity.phase != .ready {
                    SessionActivityView(activity: model.activity)
                }
                Composer(text: $model.composerText,
                     attachments: $model.pendingAttachments,
                     runPreferences: model.runPreferences,
                     controls: model.controls,
                     isSending: model.isSending,
                     canSend: model.canSendTurn,
                     canAddAttachments: !model.isCommittingPendingDeliveryAction,
                     onAddAttachment: { attachment in
                         await model.addAttachmentIfRecoverable(attachment)
                     }) {
                    Task { await model.sendTurn() }
                }
            }
        }
        .background(DroverColor.bg)
        .navigationTitle(model.harnessPresentation.name)
        .navigationBarTitleDisplayMode(.inline)
        // Without an explicit bar background the transcript scrolls under a
        // transparent bar and ghosts through the title.
        .toolbarBackground(DroverColor.bg, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar { toolbarContent }
        .confirmationDialog("Terminate this session?", isPresented: $showTerminateConfirm,
                            titleVisibility: .visible) {
            Button("Terminate", role: .destructive) {
                Task { await model.terminate() }
            }
        }
        .confirmationDialog(
            "Discard this local delivery?",
            isPresented: $showDiscardPendingConfirm,
            titleVisibility: .visible
        ) {
            Button("Discard locally", role: .destructive) {
                Task { await model.discardPendingTurn() }
            }
            .accessibilityIdentifier("chat-discard-pending-confirm")
        } message: {
            Text("This removes only the saved local delivery record.")
        }
        .task {
            await model.restoreRecovery()
            model.start()
            await model.loadSessionMetadata()
        }
        // Separate task so the delay races the connect rather than waiting
        // behind it: `loadSessionMetadata` above suspends, and a timer sharing
        // that task would not tick until it returned.
        // Native resume candidates load only once the host has advertised
        // native resume for this harness, and reload if that changes.
        .task(id: model.canResumeNatively) {
            await model.loadNativeResumeCandidates()
        }
        .task {
            try? await Task.sleep(for: .seconds(ColdOpenTracker.appearAfter))
            guard !Task.isCancelled else { return }
            coldOpenIsSlow = true
        }
        .onDisappear {
            Task { await model.prepareForDeparture() }
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .background else { return }
            Task { await model.flushRecoveryCheckpoint() }
        }
        // A handoff (`/continue`) creates a structured session for
        // structured-capable targets (chat UI, handoff context as the first
        // turn) and a seeded PTY for shell/native-resume — navigate to
        // whichever the server actually created.
        .navigationDestination(item: $handoffSession) { handoff in
            if handoff.isStructured {
                ChatView(
                    client: client,
                    sessionID: handoff.id,
                    harness: handoff.harness,
                    recoveryStore: recoveryStore,
                    recoveryWriteGate: recoveryWriteGate,
                    recoveryGeneration: recoveryGeneration,
                    chatModelFactory: chatModelFactory
                )
            } else {
                TerminalScreen(client: client, sessionID: handoff.id, harness: handoff.harness)
            }
        }
    }

    private func pendingDeliveryReview(_ pendingTurn: ChatPendingTurn) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let hint = model.hint {
                ChatHintBanner(hint)
            }
            ChatHintBanner(pendingTurn.manualReviewMessage)
            if let recoveryStatusMessage = model.recoveryStatusMessage {
                recoveryStatusBanner(recoveryStatusMessage)
            }
            if model.isCommittingPendingDeliveryAction {
                ChatHintBanner("Saving the local delivery update…")
            }
            let layout = dynamicTypeSize.isAccessibilitySize
                ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8))
                : AnyLayout(HStackLayout(spacing: 12))
            layout {
                Button {
                    model.checkPendingDelivery()
                } label: {
                    Label("Check delivery", systemImage: "arrow.triangle.2.circlepath")
                        .frame(minHeight: 44)
                }
                .accessibilityIdentifier("chat-check-delivery")

                Button {
                    Task { await model.copyPendingTurnToDraft() }
                } label: {
                    Label("Copy to draft", systemImage: "doc.on.doc")
                        .frame(minHeight: 44)
                }
                .accessibilityIdentifier("chat-copy-pending-to-draft")

                Button(role: .destructive) {
                    showDiscardPendingConfirm = true
                } label: {
                    Label("Discard locally", systemImage: "trash")
                        .frame(minHeight: 44)
                }
                .accessibilityIdentifier("chat-discard-pending")
            }
            .disabled(model.isCommittingPendingDeliveryAction)
            .font(.caption.weight(.semibold))
            .buttonStyle(.bordered)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 16)
        }
    }

    @ViewBuilder
    private func recoveryStatusBanner(_ message: String) -> some View {
        if model.canRetryRecoverySave {
            ChatHintBanner(message, actionTitle: "Retry saving") {
                Task { await model.retryRecoverySave() }
            }
        } else {
            ChatHintBanner(message)
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 0) {
                // Folded once per transcript change on the model and cached
                // there — re-folding here meant a full pass over every message
                // on each scroll-phase change.
                let items = model.items
                let visualTailID = ChatTranscriptScrollTarget.bottomDestination(
                    for: items, hasPendingTurn: model.pendingTurn != nil
                )
                ScrollView {
                    // Cold open is bounded to the newest 200 raw messages, which
                    // fold to substantially fewer rows. Keep that bounded tail
                    // materialized so keyboard/composer geometry changes cannot
                    // briefly evict the visible transcript.
                    VStack(alignment: .leading, spacing: 8) {
                        if model.hasOlderHistory {
                            Button {
                                let anchorMessageID = items.first?.anchorMessageID
                                cancelPrependScroll()
                                let generation = prependScrollGeneration
                                cancelFollowingScroll()
                                followState.detach()
                                pendingPrependScroll = Task { @MainActor in
                                    defer {
                                        if prependScrollGeneration == generation {
                                            pendingPrependScroll = nil
                                        }
                                    }
                                    let didLoad = await model.loadOlderHistory()
                                    guard !Task.isCancelled,
                                          prependScrollGeneration == generation,
                                          didLoad, let anchorMessageID,
                                          let anchorRowID = TranscriptItem.rowID(
                                            containing: anchorMessageID,
                                            in: model.messages
                                          ) else { return }
                                    // Prepending must not move the row the user
                                    // was reading. A folded run's rendered ID can
                                    // change at the page boundary, so follow one
                                    // of its raw messages into the regrouped row.
                                    try? await Task.sleep(for: .milliseconds(50))
                                    guard !Task.isCancelled else { return }
                                    proxy.scrollTo(anchorRowID, anchor: .top)
                                    // Settle once more against the raw anchor's
                                    // current rendered row after layout completes.
                                    try? await Task.sleep(for: .milliseconds(200))
                                    guard !Task.isCancelled,
                                          prependScrollGeneration == generation,
                                          let settledRowID = TranscriptItem.rowID(
                                            containing: anchorMessageID,
                                            in: model.messages
                                          ) else { return }
                                    proxy.scrollTo(settledRowID, anchor: .top)
                                }
                            } label: {
                                HStack(spacing: 8) {
                                    if model.isLoadingOlderHistory {
                                        ProgressView()
                                            .controlSize(.small)
                                    }
                                    Text(model.isLoadingOlderHistory
                                         ? "Loading earlier messages…"
                                         : "Load earlier messages")
                                        .font(.caption.weight(.medium))
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 8)
                            }
                            .buttonStyle(.plain)
                            .disabled(model.isLoadingOlderHistory)
                            .accessibilityIdentifier("chat-load-earlier")
                        }

                        ForEach(items) { item in
                            row(for: item, isNewest: item.id == items.last?.id)
                                .id(item.id)
                                .background {
                                    GeometryReader { geometry in
                                        Color.clear.preference(
                                            key: TranscriptRowFrames.self,
                                            value: [item.anchorMessageID: geometry.frame(in: .named("transcript-content"))]
                                        )
                                    }
                                }
                        }

                        if let pendingTurn = model.pendingTurn {
                            PendingTurnBubble(pendingTurn: pendingTurn)
                                .background {
                                    GeometryReader { geometry in
                                        Color.clear.preference(
                                            key: TranscriptRowFrames.self,
                                            value: ["pending-turn:\(pendingTurn.clientTurnID)": geometry.frame(in: .named("transcript-content"))]
                                        )
                                    }
                                }
                        }

                        // The ID belongs to the bottom of the clearance, not the
                        // final transcript row. `scrollTo(..., anchor: .bottom)`
                        // therefore keeps this 24pt gap visible above the composer.
                        // VStack contributes 8pt before this final 16pt tail.
                        if let visualTailID {
                            Color.clear
                                .frame(height: 16)
                                .id(visualTailID)
                        }
                    }
                    .coordinateSpace(name: "transcript-content")
                    .padding(.horizontal, 14)
                    .padding(.top, 12)
                    // An empty transcript has no row to take the width, so this
                    // stack sized to its padding and the ScrollView sized to the
                    // stack. Nothing showed it while the only thing overlaid on a
                    // cold open was a spinner, which is small and centred either
                    // way. The failure state added in #170 is text, and inherited
                    // a container about one character wide: "Can't reach the
                    // Drover server" rendered one letter per line, down the
                    // screen and past the composer.
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                // Scrolling back to read is the other moment you want the
                // keyboard gone, and dragging it away is cheaper than reaching
                // for the accessory bar's dismiss button.
                .scrollDismissesKeyboard(.interactively)
                .scrollPosition($scrollPosition)
                .onPreferenceChange(TranscriptRowFrames.self) { frames in
                    rowFrames = frames
                    if scrollPhase == .idle || scrollPhase == .animating,
                       let offset = followState.preservedOffset(in: frames),
                       abs(offset - transcriptOffset) > 0.5 {
                        scrollPosition.scrollTo(y: offset)
                    }
                }
                .onScrollGeometryChange(for: TranscriptScrollGeometry.self) { geometry in
                    TranscriptScrollGeometry(geometry)
                } action: { old, geometry in
                    transcriptOffset = geometry.offset
                    let isUserDriven = scrollPhase == .interacting || scrollPhase == .decelerating
                    // A layout callback during a gesture is still a layout change.
                    // Streaming/composer changes cannot detach a following reader.
                    let isMovement = isUserDriven
                        && old.contentHeight == geometry.contentHeight
                        && old.viewportHeight == geometry.viewportHeight
                        && old.bottomInset == geometry.bottomInset
                    if isUserDriven && !isMovement {
                        followState.layoutChanged(
                            bottomDistanceChange: geometry.contentHeight - old.contentHeight
                                + old.viewportHeight - geometry.viewportHeight
                        )
                    }
                    followState.positionChanged(
                        bottomDistance: geometry.bottomDistance,
                        isUserDriven: isMovement,
                        isDecelerating: scrollPhase == .decelerating
                    )
                    if isUserDriven {
                        followState.captureAnchor(in: rowFrames, offset: geometry.offset)
                        if !followState.isFollowing {
                            cancelFollowingScroll()
                        }
                    } else if scrollPhase == .idle || scrollPhase == .animating,
                              let offset = followState.preservedOffset(in: rowFrames),
                              abs(offset - geometry.offset) > 0.5 {
                        // Keyboard avoidance can change the offset without changing any
                        // row frames. Restore the reading anchor for those changes too.
                        scrollPosition.scrollTo(y: offset)
                    }
                    if followState.isFollowing,
                       old.contentHeight != geometry.contentHeight
                        || old.viewportHeight != geometry.viewportHeight
                        || old.bottomInset != geometry.bottomInset {
                        scheduleScroll(with: proxy)
                    }
                }
                .onScrollPhaseChange { oldPhase, newPhase, context in
                    scrollPhase = newPhase
                    if newPhase == .tracking
                        || (newPhase == .interacting && oldPhase != .tracking) {
                        followState.scrollingBegan()
                    }
                    if newPhase == .tracking || newPhase == .interacting || newPhase == .decelerating {
                        cancelPrependScroll()
                        cancelFollowingScroll()
                    }
                    if newPhase == .idle,
                       oldPhase == .interacting || oldPhase == .decelerating || oldPhase == .tracking {
                        // The last geometry callback can precede the final deceleration
                        // position. Re-check the real inset/offset at the phase transition.
                        let geometry = TranscriptScrollGeometry(context.geometry)
                        transcriptOffset = geometry.offset
                        followState.scrollingEnded(bottomDistance: geometry.bottomDistance)
                        followState.captureAnchor(in: rowFrames, offset: geometry.offset)
                        if followState.isFollowing { scheduleScroll(with: proxy) }
                    }
                }
                // Auto-scroll is coalesced and unanimated on purpose: firing an
                // animated scrollTo per appended message piles up overlapping
                // animations faster than they can finish. One unanimated scroll
                // per ~120ms window, always to the visual tail at fire time,
                // keeps the transcript following the stream without an animation
                // storm. Gated on pinning so it never fights the user's finger.
                .onChange(of: model.messagesVersion) { _, _ in
                    // Also observes in-place streaming updates, whose last ID may be unchanged.
                    if followState.contentChanged() { scheduleScroll(with: proxy) }
                }
                .onChange(of: model.messages.last?.seq) { previous, newest in
                    guard let previous, let newest, newest > previous else { return }
                    followState.recordNewEvents(model.messages.filter { $0.seq > previous }.count)
                }
                .onChange(of: model.pendingTurn?.clientTurnID) { _, _ in
                    if followState.contentChanged() { scheduleScroll(with: proxy) }
                }
                // Bottom anchoring is only an opening policy. Applying it to size changes
                // moves a detached reader whenever streaming text or the keyboard changes layout.
                .defaultScrollAnchor(.bottom, for: .initialOffset)
                .defaultScrollAnchor(.top, for: .sizeChanges)
                .onDisappear {
                    cancelFollowingScroll()
                    cancelPrependScroll()
                }
                if !followState.isFollowing {
                    // A sibling outside the scroll viewport cannot cover transcript text.
                    HStack {
                        Spacer()
                        scrollToBottomButton(proxy)
                    }
                    .padding(.top, 8)
                }
            }
        }
    }

    @ViewBuilder
    private func row(for item: TranscriptItem, isNewest: Bool) -> some View {
        switch item {
        case .message(let message):
            MessageBubble(message: message)
        case .thinkingRun(let run, let estimatedTokens):
            ThinkingBlock(
                run: run,
                estimatedTokens: estimatedTokens,
                isStreaming: isNewest && model.activity.isActive && (model.messages.last?.isThinking ?? false)
            )
        case .statusRun(let run):
            SessionEventsRow(run: run)
        case .stepRun(let steps):
            StepRunCard(steps: steps, activeToolIDs: model.activity.activeToolIDs)
        }
    }

    private func scrollToBottomButton(_ proxy: ScrollViewProxy) -> some View {
        Button {
            cancelPrependScroll()
            guard let visualTailID = ChatTranscriptScrollTarget.bottomDestination(
                for: model.items, hasPendingTurn: model.pendingTurn != nil
            ) else { return }
            withAnimation(.snappy) {
                followState.jumpToLatest()
                proxy.scrollTo(visualTailID, anchor: .bottom)
            }
            // One unanimated follow-up after layout settles closes any gap
            // left by a tall row changing size during the animated scroll.
            scheduleScroll(with: proxy)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "arrow.down")
                Text("Jump to latest")
                if followState.newEventCount > 0 {
                    Text("\(followState.newEventCount)").monospacedDigit()
                } else if followState.hasUnseenContent {
                    Circle().frame(width: 6, height: 6).accessibilityHidden(true)
                }
            }
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(DroverColor.accentHi)
                .padding(10)
                .background(DroverColor.surface, in: Capsule())
                .overlay(Capsule().strokeBorder(DroverColor.accent.opacity(0.4), lineWidth: 1))
                .shadow(color: .black.opacity(0.3), radius: 6, y: 3)
        }
        .padding(.trailing, 16)
        .padding(.bottom, 16)
        .transition(.opacity.combined(with: .scale(scale: 0.8)))
        .accessibilityLabel("Jump to latest")
        .accessibilityValue(followState.newEventCount > 0 ? "\(followState.newEventCount) new events" : "")
        .accessibilityIdentifier("chat-scroll-to-bottom")
    }

    private func scheduleScroll(with proxy: ScrollViewProxy) {
        guard pendingScroll == nil, followState.isFollowing,
              scrollPhase == .idle || scrollPhase == .animating else { return }
        let generation = scrollGeneration
        pendingScroll = Task { @MainActor in
            defer {
                if scrollGeneration == generation { pendingScroll = nil }
            }
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, followState.isFollowing,
                  let visualTailID = ChatTranscriptScrollTarget.bottomDestination(
                    for: model.items, hasPendingTurn: model.pendingTurn != nil
                  ) else { return }
            proxy.scrollTo(visualTailID, anchor: .bottom)
        }
    }

    private func cancelFollowingScroll() {
        scrollGeneration &+= 1
        pendingScroll?.cancel()
        pendingScroll = nil
    }

    private func cancelPrependScroll() {
        prependScrollGeneration &+= 1
        pendingPrependScroll?.cancel()
        pendingPrependScroll = nil
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            ChatHeaderContent(
                title: model.headerTitle,
                metadata: model.headerMetadata
            )
        }

        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                // Disabled rather than hidden, so the menu says why: the
                // host does not advertise interrupt for this harness (or has
                // not been heard from yet).
                Button {
                    Task { await model.interrupt() }
                } label: {
                    // A trailing Text is the menu item's subtitle, which the
                    // system also speaks with the title.
                    Label("Interrupt", systemImage: "stop.circle")
                    if let reason = model.controls.interruptUnavailableReason {
                        Text(reason)
                    }
                }
                .disabled(!model.controls.canInterrupt)
                .accessibilityIdentifier("chat-interrupt")
                // Same-harness handoff also needs an advertised structured target.
                Button {
                    Task { await handOff(to: nil) }
                } label: {
                    Label("Continue in a new session", systemImage: "arrow.triangle.branch")
                }
                .disabled(!model.handoffHarnesses.contains(model.harnessPresentation.harness))
                // Native sessions this harness's adapter found on the host.
                // Shown only when the host advertises native resume for it.
                if model.canResumeNatively && !model.nativeResumeCandidates.isEmpty {
                    Menu {
                        ForEach(model.nativeResumeCandidates) { candidate in
                            Button {
                                Task { await resumeNatively(candidate) }
                            } label: {
                                Label(candidate.label, systemImage: "arrow.uturn.backward")
                                if let cwd = candidate.cwd {
                                    Text(cwd)
                                }
                            }
                        }
                    } label: {
                        Label("Resume a native session", systemImage: "arrow.uturn.backward.circle")
                    }
                    .accessibilityIdentifier("chat-native-resume")
                }
                // Per-harness targets from the session's host: only those it
                // advertises a structured launch for. A PTY-only target would
                // have the seed typed into a terminal and run as commands.
                // The nil-target button above already covers the
                // same-harness case.
                if !crossHarnessTargets.isEmpty {
                    Menu {
                        ForEach(crossHarnessTargets, id: \.self) { harness in
                            let presentation = HarnessPresentation(harness)
                            Button {
                                Task { await handOff(to: harness) }
                            } label: {
                                Label(presentation.name, systemImage: presentation.symbolName)
                            }
                        }
                    } label: {
                        Label("Hand off to another harness", systemImage: "arrow.triangle.swap")
                    }
                }
                Button(role: .destructive) {
                    showTerminateConfirm = true
                } label: {
                    Label("Terminate", systemImage: "xmark.octagon")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .accessibilityLabel("Session actions")
            .accessibilityIdentifier("chat-menu")
        }
    }

    private var crossHarnessTargets: [String] {
        model.handoffHarnesses
    }

    private func resumeNatively(_ candidate: NativeResumeCandidate) async {
        if let continued = await model.resumeNatively(candidate) {
            handoffSession = HandoffSession(id: continued.sessionID,
                                            isStructured: continued.isStructured,
                                            harness: candidate.harness)
        }
    }

    private func handOff(to targetHarness: String?) async {
        if let continued = await model.handOff(targetHarness: targetHarness) {
            let harness = targetHarness ?? model.harnessPresentation.harness
            handoffSession = HandoffSession(id: continued.sessionID,
                                            isStructured: continued.isStructured,
                                            harness: harness)
        }
    }
}

/// Identifiable wrapper for `.navigationDestination(item:)` — the session a
/// handoff created, and which screen it belongs on.
private struct HandoffSession: Identifiable, Hashable {
    let id: String
    let isStructured: Bool
    let harness: String
}
