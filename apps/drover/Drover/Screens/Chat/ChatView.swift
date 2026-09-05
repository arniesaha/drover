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
    @State private var pendingPrependScroll: Task<Void, Never>?
    @State private var prependScrollGeneration = 0
    /// True while the user is at (or within ~80pt of) the transcript's end.
    /// Auto-scroll only runs while pinned; scrolling up unpins (so reading
    /// is never yanked back down) and shows the scroll-to-bottom button.
    @State private var isPinnedToBottom = true
    /// Current scroll phase — only user-driven phases may unpin (content
    /// growth pushing the bottom away must not; that was the stuck-button
    /// race: a tall new row unpinned before the coalesced scroll fired).
    @State private var scrollPhase: ScrollPhase = .idle
    /// Flipped by a timer once the cold open has lasted long enough to be
    /// worth acknowledging. A local open beats it and the screen stays quiet.
    @State private var coldOpenIsSlow = false
    @Environment(\.scenePhase) private var scenePhase

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

            if let approval = model.pendingApproval {
                DecisionBlock(
                    approval: approval,
                    isBusy: model.isAnswering,
                    onApprove: { Task { await model.approve("allow") } },
                    onDeny: { Task { await model.approve("deny") } }
                )
            }

            // Deliberately not gated on `hint`: approve, interrupt, terminate
            // and an unauthorized stream event all clear it, and an
            // unconfirmed delivery must keep its Retry through any of them.
            if let pendingTurn = model.pendingTurn,
                      pendingTurn.canRetry,
                      model.recoveryStatusMessage == nil {
                ChatHintBanner(model.hint ?? pendingTurn.retryMessage, actionTitle: "Retry") {
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
            Composer(text: $model.composerText,
                     attachments: $model.pendingAttachments,
                     runPreferences: model.runPreferences,
                     harness: model.harnessPresentation.harness,
                     isSending: model.isSending,
                     canSend: model.canSendTurn,
                     canAddAttachments: !model.isCommittingPendingDeliveryAction,
                     onAddAttachment: { attachment in
                         await model.addAttachmentIfRecoverable(attachment)
                     }) {
                Task { await model.sendTurn() }
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
            HStack(spacing: 12) {
                Button("Check delivery") {
                    model.checkPendingDelivery()
                }
                .accessibilityIdentifier("chat-check-delivery")

                Button("Copy to draft") {
                    Task { await model.copyPendingTurnToDraft() }
                }
                .accessibilityIdentifier("chat-copy-pending-to-draft")

                Button("Discard locally", role: .destructive) {
                    showDiscardPendingConfirm = true
                }
                .accessibilityIdentifier("chat-discard-pending")
            }
            .disabled(model.isCommittingPendingDeliveryAction)
            .font(.caption.weight(.semibold))
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
                            pendingScroll?.cancel()
                            pendingScroll = nil
                            isPinnedToBottom = false
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
                    }

                    if let pendingTurn = model.pendingTurn {
                        PendingTurnBubble(pendingTurn: pendingTurn)
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
            // Pinned means "within 48pt of the end" — close enough that the
            // user is following the stream, far enough that the last row's
            // own growth doesn't flap the state.
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.containerSize.height
                    >= geometry.contentSize.height + geometry.contentInsets.bottom - 48
            } action: { _, isNearBottom in
                guard isNearBottom != isPinnedToBottom else { return }
                // Re-pin whenever the bottom is reached, by any means; unpin
                // only mid-gesture (tracking/interacting/decelerating), so
                // content growth can't silently disable auto-scroll.
                let isUserDriven = scrollPhase == .tracking
                    || scrollPhase == .interacting
                    || scrollPhase == .decelerating
                guard isNearBottom || isUserDriven else { return }
                withAnimation(.snappy(duration: 0.2)) { isPinnedToBottom = isNearBottom }
            }
            .onScrollPhaseChange { _, newPhase in
                scrollPhase = newPhase
                if newPhase == .tracking || newPhase == .interacting
                    || newPhase == .decelerating {
                    cancelPrependScroll()
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if !isPinnedToBottom {
                    scrollToBottomButton(proxy)
                }
            }
            // Auto-scroll is coalesced and unanimated on purpose: firing an
            // animated scrollTo per appended message piles up overlapping
            // animations faster than they can finish. One unanimated scroll
            // per ~120ms window, always to the visual tail at fire time,
            // keeps the transcript following the stream without an animation
            // storm. Gated on pinning so it never fights the user's finger.
            .onChange(of: model.messages.last?.id) { _, newestID in
                guard newestID != nil, isPinnedToBottom else { return }
                scheduleScroll(with: proxy)
            }
            .onChange(of: model.pendingTurn?.clientTurnID) { _, _ in
                guard isPinnedToBottom else { return }
                scheduleScroll(with: proxy)
            }
            .defaultScrollAnchor(.bottom)
            .onDisappear {
                pendingScroll?.cancel()
                pendingPrependScroll?.cancel()
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
                isStreaming: isNewest && (model.messages.last?.isThinking ?? false)
            )
        case .statusRun(let run):
            SessionEventsRow(run: run)
        case .stepRun(let steps):
            StepRunCard(steps: steps)
        }
    }

    private func scrollToBottomButton(_ proxy: ScrollViewProxy) -> some View {
        Button {
            cancelPrependScroll()
            guard let visualTailID = ChatTranscriptScrollTarget.bottomDestination(
                for: model.items, hasPendingTurn: model.pendingTurn != nil
            ) else { return }
            withAnimation(.snappy) {
                isPinnedToBottom = true
                proxy.scrollTo(visualTailID, anchor: .bottom)
            }
            // One unanimated follow-up after layout settles closes any gap
            // left by a tall row changing size during the animated scroll.
            scheduleScroll(with: proxy)
        } label: {
            Image(systemName: "arrow.down")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(DroverColor.accentHi)
                .padding(10)
                .background(DroverColor.surface, in: Circle())
                .overlay(Circle().strokeBorder(DroverColor.accent.opacity(0.4), lineWidth: 1))
                .shadow(color: .black.opacity(0.3), radius: 6, y: 3)
        }
        .padding(.trailing, 16)
        .padding(.bottom, 16)
        .transition(.opacity.combined(with: .scale(scale: 0.8)))
        .accessibilityLabel("Scroll to bottom")
        .accessibilityIdentifier("chat-scroll-to-bottom")
    }

    private func scheduleScroll(with proxy: ScrollViewProxy) {
        guard pendingScroll == nil else { return }
        pendingScroll = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, isPinnedToBottom,
                  let visualTailID = ChatTranscriptScrollTarget.bottomDestination(
                    for: model.items, hasPendingTurn: model.pendingTurn != nil
                  ) else {
                pendingScroll = nil
                return
            }
            proxy.scrollTo(visualTailID, anchor: .bottom)
            // A row can still grow after the first scroll (for example a
            // disclosure or tall diff), so pin once more after layout settles.
            try? await Task.sleep(for: .milliseconds(200))
            pendingScroll = nil
            guard !Task.isCancelled, isPinnedToBottom,
                  let settledVisualTailID = ChatTranscriptScrollTarget.bottomDestination(
                    for: model.items, hasPendingTurn: model.pendingTurn != nil
                  ) else { return }
            proxy.scrollTo(settledVisualTailID, anchor: .bottom)
        }
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
                Button {
                    Task { await model.interrupt() }
                } label: {
                    Label("Interrupt", systemImage: "stop.circle")
                }
                // nil target = same harness. Structured-capable harnesses now
                // continue into a fresh structured chat (the handoff context
                // becomes its first turn); only shell sources land in a
                // terminal.
                Button {
                    Task { await handOff(to: nil) }
                } label: {
                    Label("Continue in a new session", systemImage: "arrow.triangle.branch")
                }
                // Per-harness targets from the session's host. "shell" is
                // deliberately excluded: the seed gets typed into the PTY,
                // and a bare shell would execute the handoff summary as
                // commands. The nil-target button above already covers the
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
        model.handoffHarnesses.filter { $0 != "shell" }
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
