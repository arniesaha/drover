import SwiftUI
import NexusKit

/// Three buckets over the live snapshot: sessions needing the user, sessions
/// working, and finished sessions (collapsed). Polling starts as soon as the
/// view appears and follows `scenePhase` thereafter; pull-to-refresh does a
/// single one-off `refresh()`. Structured sessions navigate to the real
/// `ChatView` (Task 7); PTY sessions navigate to `TerminalScreen` (Task 9),
/// a live SwiftTerm view over the harness's terminal WebSocket.
struct SessionsView: View {
    @State private var store: SessionStore
    private let client: NexusClient
    private let notifier: Notifying
    @Environment(\.scenePhase) private var scenePhase
    @State private var showLaunch = false
    @State private var launchedSession: LaunchedSession?

    init(client: NexusClient, notifier: Notifying = LocalNotifier()) {
        self.client = client
        self.notifier = notifier
        _store = State(initialValue: SessionStore(client: client))
    }

    var body: some View {
        List {
            if let lastError = store.lastError {
                Section {
                    Label(lastError, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
            }

            Section("Needs you") {
                bucket(store.needsYou, empty: "Nothing needs you right now.")
            }

            Section("Working") {
                bucket(store.working, empty: "No sessions in progress.")
            }

            if !store.finished.isEmpty {
                DisclosureGroup("Finished (\(store.finished.count))") {
                    ForEach(store.finished) { session in
                        row(for: session)
                    }
                }
            }
        }
        .navigationTitle("Sessions")
        .refreshable { await store.refresh() }
        .task { store.startPolling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                store.startPolling()
            } else {
                store.stopPolling()
            }
        }
        // Foreground polling keeps the badge in sync through the same
        // `Notifying` instance the background refresh uses — no new local
        // alerts here (the list itself is the up-to-date view), just the
        // app-icon badge count.
        .onChange(of: store.needsYou) { _, current in
            Task { await notifier.setBadge(current.count) }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showLaunch = true
                } label: {
                    Image(systemName: "plus")
                }
                .accessibilityIdentifier("launch-button")
            }
        }
        .sheet(isPresented: $showLaunch) {
            NavigationStack {
                LaunchView(client: client, snapshot: store.snapshot) { sessionID, isStructured in
                    launchedSession = LaunchedSession(id: sessionID, isStructured: isStructured)
                }
            }
        }
        .navigationDestination(item: $launchedSession) { launched in
            if launched.isStructured {
                ChatView(client: client, sessionID: launched.id)
            } else {
                TerminalScreen(client: client, sessionID: launched.id)
            }
        }
    }

    @ViewBuilder
    private func bucket(_ sessions: [SessionSummary], empty: String) -> some View {
        if sessions.isEmpty {
            Text(empty).foregroundStyle(.secondary)
        } else {
            ForEach(sessions) { session in
                row(for: session)
            }
        }
    }

    private func row(for session: SessionSummary) -> some View {
        NavigationLink {
            if session.isStructured {
                ChatView(client: client, sessionID: session.id)
            } else {
                TerminalScreen(client: client, sessionID: session.id)
            }
        } label: {
            SessionRow(session: session)
        }
        .accessibilityIdentifier(session.id)
        .contextMenu {
            Button {
                Task { await continueSession(session) }
            } label: {
                Label("Continue in a terminal", systemImage: "arrow.triangle.branch")
            }
        }
    }

    /// Server-side handoff: launches a fresh PTY session seeded with this
    /// one's transcript context (the `/continue` endpoint always creates a
    /// terminal session), then navigates into it. Works on finished sessions
    /// too — the real "resume a dead session" path. Failures surface through
    /// the store's `lastError` banner at the top of the list.
    private func continueSession(_ session: SessionSummary) async {
        guard let newSessionID = await store.continueSession(session.id) else {
            return
        }
        launchedSession = LaunchedSession(id: newSessionID, isStructured: false)
    }
}

/// Result of a successful `LaunchView` launch, carried through
/// `.navigationDestination(item:)` so `SessionsView` can push straight into
/// the new session once the sheet dismisses.
private struct LaunchedSession: Identifiable, Hashable {
    let id: String
    let isStructured: Bool
}
