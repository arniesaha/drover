import SwiftUI
import DroverKit

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
    @State private var model: ChatModel
    @State private var showTerminateConfirm = false
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

    init(
        client: DroverClient,
        sessionID: String,
        harness: String? = nil,
        recap: String? = nil,
        recapSourceSeq: Int? = nil
    ) {
        self.client = client
        _model = State(initialValue: ChatModel(
            client: client,
            sessionID: sessionID,
            harness: harness,
            recap: recap,
            recapSourceSeq: recapSourceSeq
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

            transcript

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

            if let hint = model.hint {
                Text(hint)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .padding(.horizontal)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Composer(text: $model.composerText,
                     attachments: $model.pendingAttachments,
                     selectedModel: $model.selectedModel,
                     thinkingEffort: $model.thinkingEffort,
                     harness: model.harnessPresentation.harness,
                     isSending: model.isSending) {
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
        .task {
            model.start()
            await model.loadSessionMetadata()
        }
        .onDisappear { model.stop() }
        // A handoff (`/continue`) creates a structured session for
        // structured-capable targets (chat UI, handoff context as the first
        // turn) and a seeded PTY for shell/native-resume — navigate to
        // whichever the server actually created.
        .navigationDestination(item: $handoffSession) { handoff in
            if handoff.isStructured {
                ChatView(client: client, sessionID: handoff.id, harness: handoff.harness)
            } else {
                TerminalScreen(client: client, sessionID: handoff.id, harness: handoff.harness)
            }
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            // Folded once per transcript change on the model and cached
            // there — re-folding here meant a full pass over every message
            // on each scroll-phase change.
            let items = model.items
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
                }
                .padding()
            }
            // Scrolling back to read is the other moment you want the
            // keyboard gone, and dragging it away is cheaper than reaching
            // for the accessory bar's dismiss button.
            .scrollDismissesKeyboard(.interactively)
            // Pinned means "within 80pt of the end" — close enough that the
            // user is following the stream, far enough that the last row's
            // own growth doesn't flap the state.
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.containerSize.height
                    >= geometry.contentSize.height + geometry.contentInsets.bottom - 80
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
            guard let rowID = model.visualTailRowID else { return }
            withAnimation(.snappy) {
                isPinnedToBottom = true
                proxy.scrollTo(rowID, anchor: .bottom)
            }
            // One unanimated follow-up after layout settles closes any gap
            // left by a tall row changing size during the animated scroll.
            scheduleScroll(with: proxy)
        } label: {
            Image(systemName: "arrow.down")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(12)
                .background(.regularMaterial, in: Circle())
                .overlay(Circle().strokeBorder(.secondary.opacity(0.2)))
                .shadow(color: .black.opacity(0.15), radius: 4, y: 2)
        }
        .padding(.trailing, 16)
        .padding(.bottom, 12)
        .transition(.opacity.combined(with: .scale(scale: 0.8)))
        .accessibilityLabel("Scroll to bottom")
        .accessibilityIdentifier("chat-scroll-to-bottom")
    }

    private func scheduleScroll(with proxy: ScrollViewProxy) {
        guard pendingScroll == nil else { return }
        pendingScroll = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, isPinnedToBottom,
                  let rowID = model.visualTailRowID else {
                pendingScroll = nil
                return
            }
            proxy.scrollTo(rowID, anchor: .bottom)
            // A row can still grow after the first scroll (for example a
            // disclosure or tall diff), so pin once more after layout settles.
            try? await Task.sleep(for: .milliseconds(200))
            pendingScroll = nil
            guard !Task.isCancelled, isPinnedToBottom,
                  let settledRowID = model.visualTailRowID else { return }
            proxy.scrollTo(settledRowID, anchor: .bottom)
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
