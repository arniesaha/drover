import SwiftUI
import NexusKit

/// Fleet-first view over the live snapshot: one section per host (ordered
/// online→stale→offline, waiting sessions first within each), plus finished
/// sessions (collapsed). Polling starts as soon as the view appears and
/// follows `scenePhase` thereafter; pull-to-refresh does a single one-off
/// `refresh()`. Structured sessions navigate to the real `ChatView`
/// (Task 7); PTY sessions navigate to `TerminalScreen` (Task 9), a live
/// SwiftTerm view over the harness's terminal WebSocket.
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
            ForEach(store.hostGroups) { group in
                Section {
                    Group {
                        if group.sessions.isEmpty {
                            Text("No active sessions")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(group.sessions) { session in
                                row(for: session)
                            }
                        }
                    }
                    .opacity(group.host.presence == .online ? 1 : 0.55)
                } header: {
                    HostSectionHeader(host: group.host)
                }
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
        .opacity(store.hasLoadedOnce && !store.isReachable ? 0.5 : 1)
        .safeAreaInset(edge: .top, spacing: 0) {
            if store.hasLoadedOnce && !store.isReachable {
                UnreachableBanner(message: store.lastError ?? "Server unreachable") {
                    Task { await store.refresh() }
                }
            }
        }
        .overlay {
            if !store.hasLoadedOnce {
                if let error = store.lastError {
                    ContentUnavailableView {
                        Label("Can't reach the Drover server", systemImage: "wifi.exclamationmark")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Retry") {
                            Task { await store.refresh() }
                        }
                        .buttonStyle(.borderedProminent)
                    }
                } else {
                    ProgressView("Connecting…")
                }
            }
        }
        .refreshable { await store.refresh() }
        .task { store.startPolling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                store.startPolling()
            } else {
                store.stopPolling()
            }
        }
        // Foreground polling drives the same AttentionWatcher diff the
        // background refresh uses: newly-needy sessions (a response
        // completed → "your turn", or an approval appeared) fire a banner
        // near-real-time, the badge stays in sync, and the shared persisted
        // seen-set means the BGTask path never double-alerts for the same
        // transition.
        .onChange(of: store.needsYou) { _, _ in
            guard let snapshot = store.snapshot else { return }
            let watcher = AttentionWatcher(notifier: notifier)
            Task { await watcher.evaluate(snapshot) }
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
                LaunchView(client: client, snapshot: store.snapshot) { sessionID, isStructured, harness in
                    launchedSession = LaunchedSession(
                        id: sessionID,
                        isStructured: isStructured,
                        harness: harness
                    )
                }
            }
        }
        .navigationDestination(item: $launchedSession) { launched in
            if launched.isStructured {
                ChatView(client: client, sessionID: launched.id, harness: launched.harness)
            } else {
                TerminalScreen(client: client, sessionID: launched.id, harness: launched.harness)
            }
        }
    }

    private func row(for session: SessionSummary) -> some View {
        NavigationLink {
            if session.isStructured {
                ChatView(client: client, sessionID: session.id, harness: session.harness)
            } else {
                TerminalScreen(client: client, sessionID: session.id, harness: session.harness)
            }
        } label: {
            SessionRow(session: session)
        }
        .accessibilityIdentifier(session.id)
        .contextMenu {
            Button {
                Task { await continueSession(session) }
            } label: {
                Label("Continue session", systemImage: "arrow.triangle.branch")
            }
            // Cross-harness targets from the session's host (via the polled
            // snapshot). "shell" is excluded — the handoff seed gets typed
            // into the PTY, and a bare shell would execute it as commands.
            ForEach(crossHarnessTargets(for: session), id: \.self) { harness in
                let presentation = HarnessPresentation(harness)
                Button {
                    Task { await continueSession(session, targetHarness: harness) }
                } label: {
                    Label("Continue with \(presentation.name)", systemImage: presentation.symbolName)
                }
            }
        }
    }

    private func crossHarnessTargets(for session: SessionSummary) -> [String] {
        let harnesses = store.snapshot?.hosts
            .first { $0.id == session.hostID }?
            .harnesses ?? []
        return harnesses.filter { $0 != "shell" }
    }

    /// Server-side handoff: continues this session's context in a fresh one
    /// (structured chat for structured-capable targets, seeded PTY for
    /// shell), then navigates into whichever the server created. Works on
    /// finished sessions too — the real "resume a dead session" path.
    /// `targetHarness` picks the new session's harness (nil keeps the
    /// source's). Failures surface through the store's `lastError` banner at
    /// the top of the list.
    private func continueSession(_ session: SessionSummary, targetHarness: String? = nil) async {
        guard let continued = await store.continueSession(session.id, targetHarness: targetHarness) else {
            return
        }
        let harness = targetHarness ?? session.harness
        launchedSession = LaunchedSession(id: continued.sessionID,
                                          isStructured: continued.isStructured,
                                          harness: harness)
    }
}

/// Result of a successful `LaunchView` launch, carried through
/// `.navigationDestination(item:)` so `SessionsView` can push straight into
/// the new session once the sheet dismisses.
private struct LaunchedSession: Identifiable, Hashable {
    let id: String
    let isStructured: Bool
    let harness: String
}
