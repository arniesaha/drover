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

    init(client: NexusClient, sessionID: String) {
        self.client = client
        _model = State(initialValue: ChatModel(client: client, sessionID: sessionID))
    }

    var body: some View {
        @Bindable var model = model

        VStack(spacing: 0) {
            // Only once a connection has existed is a disconnect worth a
            // "Reconnecting…" pill; during the initial connect it would just
            // flash misleading chrome.
            if model.hasConnectedOnce && !model.isConnected {
                reconnectingPill
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
        .navigationTitle("Chat")
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
                ChatView(client: client, sessionID: handoff.id)
            } else {
                TerminalScreen(client: client, sessionID: handoff.id)
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
                withAnimation(.snappy(duration: 0.2)) { isPinnedToBottom = isNearBottom }
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
        }
    }

    private func scrollToBottomButton(_ proxy: ScrollViewProxy) -> some View {
        Button {
            guard let rowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            // A single user-initiated scroll may animate — the storm problem
            // above only applies to per-message auto-scrolls.
            withAnimation(.snappy) { proxy.scrollTo(rowID, anchor: .bottom) }
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
            pendingScroll = nil
            // Re-check pinning at fire time — the user may have started
            // scrolling up during the coalescing window. The scroll target
            // is the newest *row* (a trailing thinking run's row id is its
            // first message, not the newest message).
            guard !Task.isCancelled, isPinnedToBottom,
                  let rowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            proxy.scrollTo(rowID, anchor: .bottom)
        }
    }

    private var reconnectingPill: some View {
        HStack(spacing: 6) {
            ProgressView().scaleEffect(0.7)
            Text("Reconnecting…")
        }
        .font(.caption)
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(.secondary.opacity(0.15), in: Capsule())
        .padding(.top, 6)
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
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
                            Button {
                                Task { await handOff(to: harness) }
                            } label: {
                                Label(harness, systemImage: "arrow.triangle.branch")
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
            handoffSession = HandoffSession(id: continued.sessionID,
                                            isStructured: continued.isStructured)
        }
    }
}

/// Identifiable wrapper for `.navigationDestination(item:)` — the session a
/// handoff created, and which screen it belongs on.
private struct HandoffSession: Identifiable, Hashable {
    let id: String
    let isStructured: Bool
}
