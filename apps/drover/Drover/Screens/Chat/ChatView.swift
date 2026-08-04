import SwiftUI
import NexusKit

/// The structured-session chat screen: a scrolling transcript (auto-scrolls
/// to the newest message), a "reconnecting…" pill while the stream is down,
/// a pinned approval banner when the harness is waiting on a decision, and a
/// composer. Interrupt/terminate live in the toolbar. All state and network
/// calls are delegated to `ChatModel` — this view only renders it.
struct ChatView: View {
    private let client: NexusClient
    @State private var model: ChatModel
    @State private var showTerminateConfirm = false
    @State private var handoffSession: HandoffSession?
    @State private var pendingScroll: Task<Void, Never>?
    /// True while the user is at (or within ~80pt of) the transcript's end.
    /// Auto-scroll only runs while pinned; scrolling up unpins (so reading
    /// is never yanked back down) and shows the scroll-to-bottom button.
    @State private var isPinnedToBottom = true
    /// Current scroll phase — only user-driven phases may unpin (content
    /// growth pushing the bottom away must not; that was the stuck-button
    /// race: a tall new row unpinned before the coalesced scroll fired).
    @State private var scrollPhase: ScrollPhase = .idle

    init(client: NexusClient, sessionID: String, harness: String? = nil) {
        self.client = client
        _model = State(initialValue: ChatModel(client: client, sessionID: sessionID, harness: harness))
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

            if let approval = model.pendingApproval {
                ApprovalBanner(
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

            Composer(text: $model.composerText) {
                Task { await model.sendTurn() }
            }
        }
        .navigationTitle(model.harnessPresentation.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { toolbarContent }
        .confirmationDialog("Terminate this session?", isPresented: $showTerminateConfirm,
                            titleVisibility: .visible) {
            Button("Terminate", role: .destructive) {
                Task { await model.terminate() }
            }
        }
        .task {
            model.start()
            await model.loadHandoffTargets()
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
            // Consecutive thinking messages fold into one ThinkingBlock row
            // (TranscriptItem.group); everything else renders 1:1.
            let items = TranscriptItem.group(model.messages)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(items) { item in
                        row(for: item, isNewest: item.id == items.last?.id)
                            .id(item.id)
                    }
                }
                .padding()
            }
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
            }
            .overlay(alignment: .bottomTrailing) {
                if !isPinnedToBottom {
                    scrollToBottomButton(proxy)
                }
            }
            // Auto-scroll is coalesced and unanimated on purpose: firing an
            // animated scrollTo per appended message piles up overlapping
            // animations faster than they can finish, and a LazyVStack under
            // that load de-materializes the visible rows — the screen goes
            // blank until the subtree is rebuilt (the "leave and come back"
            // workaround). One unanimated scroll per ~120ms window, always
            // to whatever is newest by the time it fires, keeps the
            // transcript following the stream without the animation storm.
            // Gated on pinning so it never fights the user's finger.
            .onChange(of: model.messages.last?.id) { _, newestID in
                guard newestID != nil, isPinnedToBottom else { return }
                scheduleScroll(with: proxy)
            }
            .defaultScrollAnchor(.bottom)
            .onDisappear { pendingScroll?.cancel() }
        }
    }

    @ViewBuilder
    private func row(for item: TranscriptItem, isNewest: Bool) -> some View {
        switch item {
        case .message(let message):
            MessageBubble(message: message)
        case .thinkingRun(let run):
            ThinkingBlock(
                run: run,
                isStreaming: isNewest && (model.messages.last?.isThinking ?? false)
            )
        case .step(let action, let result):
            StepCard(action: action, result: result)
        }
    }

    private func scrollToBottomButton(_ proxy: ScrollViewProxy) -> some View {
        Button {
            guard let rowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            withAnimation(.snappy) {
                isPinnedToBottom = true
                proxy.scrollTo(rowID, anchor: .bottom)
            }
            // Late-measuring lazy rows (tall diffs) can land the animated
            // scroll short; one unanimated follow-up after layout settles
            // closes the gap so pinning actually holds.
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
                  let rowID = TranscriptItem.latestRowID(of: model.messages) else {
                pendingScroll = nil
                return
            }
            proxy.scrollTo(rowID, anchor: .bottom)
            // Settle pass: rows that finish measuring after the first scroll
            // (LazyVStack + tall code/diff blocks) grow the content under us;
            // one more unanimated scroll pins the real bottom.
            try? await Task.sleep(for: .milliseconds(200))
            pendingScroll = nil
            guard !Task.isCancelled, isPinnedToBottom,
                  let settledRowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            proxy.scrollTo(settledRowID, anchor: .bottom)
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Label(model.harnessPresentation.name,
                  systemImage: model.harnessPresentation.symbolName)
                .labelStyle(.titleAndIcon)
                .font(.headline)
                .accessibilityIdentifier("chat-harness-title")
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
