import SwiftUI
import NexusKit

/// The fleet inbox: a count that says how much wants you, a host strip that
/// says where the herd is, then one list of live sessions ordered by activity.
///
/// Conversation and terminal sessions share that list — `SessionRow` gives
/// each species its own form, and the card's verb ("Answer", "Attach") carries
/// what tapping it does. Structured sessions navigate to `ChatView`, PTY
/// sessions to `TerminalScreen`, exactly as before.
///
/// Degraded states live in `FleetHeader` rather than in chrome of their own:
/// an unreachable hub turns the fleet line into the error and drops every host
/// dot to its offline form, which is why there is no longer a banner inset or
/// a whole-screen dim here.
struct SessionsView: View {
    @State private var store: SessionStore
    private let client: NexusClient
    private let notifier: Notifying
    @Environment(\.scenePhase) private var scenePhase
    @State private var showLaunch = false
    @State private var launchedSession: LaunchedSession?
    @State private var showFinished = false

    init(client: NexusClient, notifier: Notifying = LocalNotifier()) {
        self.client = client
        self.notifier = notifier
        _store = State(initialValue: SessionStore(client: client))
    }

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if store.hasLoadedOnce {
                        FleetHeader(
                            summary: summary,
                            hostGroups: store.hostGroups,
                            onRetry: { Task { await store.refresh() } }
                        )
                        .padding(.bottom, 4)
                    }

                    // Action errors (e.g. a failed continueSession) land here.
                    // They are distinct from an unreachable hub: connected, but
                    // the last thing you asked for didn't happen. Refresh
                    // failures flip `isReachable` and are reported by the fleet
                    // line instead, so the two can never both be showing.
                    if store.hasLoadedOnce, store.isReachable, let lastError = store.lastError {
                        actionFailedRow(lastError)
                    }

                    if activeSessions.isEmpty, store.hasLoadedOnce {
                        ContentUnavailableView("Nothing running",
                                               systemImage: "rectangle.stack.badge.plus",
                                               description: Text("Send one out when you're ready."))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 40)
                    } else {
                        ForEach(activeSessions) { session in
                            row(for: session)
                        }
                    }

                    if !store.finished.isEmpty {
                        finishedSection
                    }
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
                .padding(.bottom, 98)
            }
            .refreshable { await store.refresh() }

            if store.hasLoadedOnce {
                launchButton
            }
        }
        .background(DroverColor.bg)
        .navigationTitle("")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(DroverColor.bg, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
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
                        .buttonStyle(.bordered)
                    }
                } else {
                    ProgressView("Connecting…")
                }
            }
        }
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
            .presentationDetents([.large])
            .presentationCornerRadius(24)
        }
        .navigationDestination(item: $launchedSession) { launched in
            if launched.isStructured {
                ChatView(client: client, sessionID: launched.id, harness: launched.harness)
            } else {
                TerminalScreen(client: client, sessionID: launched.id, harness: launched.harness)
            }
        }
    }

    private var summary: FleetSummaryPresentation {
        FleetSummaryPresentation(
            snapshot: store.snapshot,
            isReachable: store.isReachable,
            error: store.lastError
        )
    }

    private var activeSessions: [SessionSummary] {
        store.activeSessions
    }

    // MARK: - Pieces

    private func actionFailedRow(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle")
                .font(.system(size: 12, weight: .medium))
            Text(message)
                .lineLimit(2)
        }
        .droverText(.subtitle)
        .foregroundStyle(DroverColor.accentHi)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(DroverColor.accentTint, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .accessibilityIdentifier("action-failed")
    }

    private var finishedSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            FadingRule()
                .padding(.vertical, 6)

            DisclosureGroup(isExpanded: $showFinished) {
                VStack(spacing: 10) {
                    ForEach(store.finished) { session in
                        row(for: session)
                    }
                }
                .padding(.top, 10)
            } label: {
                HStack(spacing: 8) {
                    Text("Finished").droverText(.h3)
                    Text("\(store.finished.count)").droverText(.marker)
                }
            }
            .tint(DroverColor.muted.color(for: colorScheme))
        }
    }

    private var launchButton: some View {
        Button {
            showLaunch = true
        } label: {
            Label("Send one out", systemImage: "plus")
                .font(.system(.subheadline, design: .default, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)
                .padding(.horizontal, 15)
                .padding(.vertical, 11)
                // Outlined on the ground tone, never a filled pill — the
                // system guide reserves fills for nothing at this scale.
                .background(DroverColor.bg, in: Capsule())
                .overlay { Capsule().strokeBorder(DroverColor.accent, lineWidth: 1) }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("launch-button")
        .padding(.leading, 18)
        .padding(.bottom, 18)
    }

    @Environment(\.colorScheme) private var colorScheme

    private func row(for session: SessionSummary) -> some View {
        NavigationLink {
            if session.isStructured {
                ChatView(client: client, sessionID: session.id, harness: session.harness)
            } else {
                TerminalScreen(client: client, sessionID: session.id, harness: session.harness)
            }
        } label: {
            SessionRow(session: session, hostTitle: hostTitle(for: session))
        }
        .buttonStyle(.plain)
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

    private func hostTitle(for session: SessionSummary) -> String {
        store.snapshot?.hosts.first { $0.id == session.hostID }?.title ?? session.hostID
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
    /// source's). Failures surface through the store's `lastError`, rendered
    /// as the inline action-failed row above the list (while connected — the
    /// fleet line takes over if the hub itself goes offline).
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
