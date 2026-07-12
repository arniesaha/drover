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
        .task { model.start() }
        .onDisappear { model.stop() }
        // A handoff (`/continue`) always creates a PTY session seeded with the
        // summarized context, not a structured one — so it opens in the
        // terminal, where the harness CLI (and the typed-in context) is shown.
        .navigationDestination(item: $handoffSession) { handoff in
            TerminalScreen(client: client, sessionID: handoff.id)
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(model.messages) { message in
                        MessageBubble(message: message)
                            .id(message.id)
                    }
                }
                .padding()
            }
            .onChange(of: model.messages.last?.id) { _, newestID in
                guard let newestID else { return }
                withAnimation {
                    proxy.scrollTo(newestID, anchor: .bottom)
                }
            }
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
                Button {
                    Task {
                        if let newSessionID = await model.handOff() {
                            handoffSession = HandoffSession(id: newSessionID)
                        }
                    }
                } label: {
                    Label("Hand off to a terminal", systemImage: "arrow.triangle.branch")
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
}

/// Identifiable wrapper for `.navigationDestination(item:)` — the PTY session
/// a "Hand off to a terminal" action created.
private struct HandoffSession: Identifiable, Hashable {
    let id: String
}
