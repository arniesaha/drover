import SwiftUI
import DroverKit

/// The fleet inbox: a wordmark row carrying the two app-level controls, a
/// pinned status header (the count that says how much wants you, the host
/// strip that says where the herd is, and the provider capacity strip), then
/// one list of live sessions, over a pinned "New Session" footer.
///
/// Only the list scrolls. The header above it and the action bar below it stay
/// put, so capacity is always in the first viewport instead of being scrolled
/// past — and, more importantly, the list is one uninterrupted run. It used to
/// be two runs with four analytics sections wedged between them, which read as
/// a sort bug (#80); the analytics that are not capacity now sit *below* the
/// list, where they can no longer split it.
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
    @State private var cockpitStore: CockpitStore
    private let client: DroverClient
    private let notifier: Notifying
    private let onOpenSettings: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @Environment(AppearanceStore.self) private var appearance
    @State private var showLaunch = false
    @State private var launchedSession: LaunchedSession?
    @State private var showFinished = false
    @State private var showAnalytics = false
    @State private var showInsights = false

    init(
        client: DroverClient,
        notifier: Notifying = LocalNotifier(),
        onOpenSettings: @escaping () -> Void = {}
    ) {
        self.client = client
        self.notifier = notifier
        self.onOpenSettings = onOpenSettings
        _store = State(initialValue: SessionStore(client: client))
        _cockpitStore = State(initialValue: CockpitStore(client: client))
    }

    var body: some View {
        VStack(spacing: 0) {
            chromeRow

            if store.hasLoadedOnce {
                InboxStatusHeader(
                    summary: summary,
                    hostGroups: store.hostGroups,
                    onRetry: { Task { await store.refresh() } }
                ) {
                    providerCapacity
                }
            }

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    // Action errors (e.g. a failed continueSession) land here.
                    // They are distinct from an unreachable hub: connected, but
                    // the last thing you asked for didn't happen. Refresh
                    // failures flip `isReachable` and are reported by the fleet
                    // line instead, so the two can never both be showing.
                    if store.hasLoadedOnce, store.isReachable, let lastError = store.lastError {
                        actionFailedRow(lastError)
                    }

                    // One list, in one run. Work that needs a human is still
                    // first and running sessions still follow it, but nothing
                    // is allowed between them any more: the analytics that used
                    // to sit in the middle are below the list now (#80).
                    if inboxSessions.isEmpty, store.hasLoadedOnce {
                        ContentUnavailableView("Nothing running",
                                               systemImage: "rectangle.stack.badge.plus",
                                               description: Text("Start a new session when you're ready."))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 40)
                    } else {
                        ForEach(inboxSessions) { session in
                            row(for: session)
                        }
                    }

                    if !store.finished.isEmpty {
                        finishedSection
                    }

                    if cockpitStore.isCockpitAvailable {
                        VStack(alignment: .leading, spacing: 10) {
                            analyticsSections
                        }
                        .padding(.top, 6)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
                .padding(.bottom, 16)
            }
            .refreshable {
                await store.refresh()
                if let snapshot = store.snapshot {
                    await cockpitStore.refresh(for: snapshot)
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
                            .buttonStyle(.bordered)
                        }
                    } else {
                        // The spinner alone cannot distinguish a hub that is
                        // unreachable from a first load repeatedly torn down
                        // before it lands — which is exactly why a recurring
                        // stuck "Connecting…" could not be diagnosed from the
                        // phone (#85). After the second failure it says which.
                        VStack(spacing: 8) {
                            ProgressView("Connecting…")
                            if let detail = store.connectingDetail {
                                Text(detail)
                                    .droverText(.subtitle)
                                    .multilineTextAlignment(.center)
                                    .accessibilityIdentifier("connecting-detail")
                            }
                        }
                    }
                }
            }

            if store.hasLoadedOnce {
                actionBar
            }
        }
        .background(DroverColor.bg)
        // The design's chrome row *is* this screen's header, so the navigation
        // bar it would otherwise sit under has nothing left to carry. Pushed
        // screens (chat, terminal, the launch sheet) declare their own bars
        // and are unaffected.
        .toolbar(.hidden, for: .navigationBar)
        // Both this and the scene-phase change below ask the store to poll —
        // the screen appearing and the app becoming active are separate
        // events and either can happen first. That overlap used to cancel the
        // first load, because starting tore the running loop down (#85);
        // `startPolling()` now leaves a live loop alone, so the two calls can
        // stay independent and neither has to know about the other.
        .task { store.startPolling() }
        .task(id: store.snapshot?.cockpitAPIVersion) {
            guard let snapshot = store.snapshot else {
                cockpitStore.updateCapability(from: nil)
                return
            }
            cockpitStore.startForegroundPolling(for: snapshot)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                store.startPolling()
                if let snapshot = store.snapshot {
                    cockpitStore.startForegroundPolling(for: snapshot)
                }
            } else {
                store.stopPolling()
                cockpitStore.stopForegroundPolling()
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
        .navigationDestination(isPresented: $showAnalytics) {
            AnalyticsView(store: cockpitStore)
        }
        .navigationDestination(isPresented: $showInsights) {
            InsightsView(client: client, store: cockpitStore)
        }
    }

    private var summary: FleetSummaryPresentation {
        FleetSummaryPresentation(
            snapshot: store.snapshot,
            isReachable: store.isReachable,
            error: store.lastError
        )
    }

    /// The whole live list, in one piece: needs-you first, then working, each
    /// newest-first. The store owns the order (`SessionStore.inboxSessions`) so
    /// it can be tested without a view.
    private var inboxSessions: [SessionSummary] {
        store.inboxSessions
    }

    /// How far behind the snapshot every card below is drawn from.
    ///
    /// Recomputed on each render, which is what keeps the age honest: a failed
    /// poll still mutates `refreshAttempts` and `lastRefreshOutcome`, so an
    /// unreachable hub re-renders this screen every five seconds and the cards'
    /// ages climb even though no snapshot lands.
    private var freshness: SnapshotFreshness {
        store.freshness()
    }

    private var providerSectionStatus: DataStatus {
        let responseStatus = cockpitStore.overview?.providerCapacity.status ?? .ok
        if cockpitStore.providerError != nil, responseStatus == .ok {
            return .unknown
        }
        return responseStatus
    }

    // MARK: - Pieces

    private var chromeRow: some View {
        InboxChromeRow(
            onToggleTheme: { appearance.toggle(displaying: colorScheme) },
            onOpenSettings: onOpenSettings
        )
    }

    /// The pinned capacity strip. Empty (and so zero-height inside the pinned
    /// header) whenever the hub has no cockpit, or nothing to report about it.
    @ViewBuilder
    private var providerCapacity: some View {
        if cockpitStore.isCockpitAvailable,
           !cockpitStore.providerAccounts.isEmpty || cockpitStore.providerError != nil {
            ProviderCapacitySection(
                accounts: cockpitStore.providerAccounts,
                status: providerSectionStatus,
                statusMessage: cockpitStore.providerError,
                hostTitles: hostTitles,
                onOpenAnalytics: { showAnalytics = true }
            )
        }
    }

    /// Everything that is *about* the fleet rather than *in* it. Below the
    /// list, where it can no longer split it.
    @ViewBuilder
    private var analyticsSections: some View {
        if let activity = cockpitStore.activity {
            ActivitySummarySection(
                activity: activity,
                statusMessage: cockpitStore.activityError,
                onOpenAnalytics: { showAnalytics = true }
            )
        }

        if !cockpitStore.popularProjects.isEmpty {
            PopularProjectsSection(
                projects: cockpitStore.popularProjects,
                tokenCoveragePercent: cockpitStore.activity?.coverage.tokenPercent,
                onOpenAnalytics: { showAnalytics = true }
            )
        }

        if cockpitStore.isInsightsAvailable {
            InsightsSummaryRow(counts: cockpitStore.insightCounts) {
                showInsights = true
            }
        }
    }

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
            FinishedRow(count: store.finished.count, isExpanded: showFinished) {
                withAnimation(.snappy(duration: 0.2)) { showFinished.toggle() }
            }

            if showFinished {
                VStack(spacing: 10) {
                    ForEach(store.finished) { session in
                        row(for: session)
                    }
                }
            }
        }
        .padding(.top, 4)
    }

    private var actionBar: some View {
        NewSessionBar { showLaunch = true }
    }

    @Environment(\.colorScheme) private var colorScheme

    private func row(for session: SessionSummary) -> some View {
        NavigationLink {
            Group {
                if session.isStructured {
                    ChatView(
                        client: client,
                        sessionID: session.id,
                        harness: session.harness,
                        recap: session.recap ?? session.preview,
                        recapSourceSeq: session.recapSourceSeq
                    )
                } else {
                    TerminalScreen(client: client, sessionID: session.id, harness: session.harness)
                }
            }
            // Opening the session is what clears it from the badge. Marked
            // here rather than inside the destination screens because this is
            // the only place that holds the snapshot the count is derived
            // from, and it keeps both screens ignorant of badges entirely.
            .task { await markRead(session) }
            .onDisappear { Task { await markRead(session) } }
        } label: {
            SessionRow(
                session: session,
                hostTitle: hostTitle(for: session),
                freshness: freshness
            )
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

    /// Marked on open and again on leaving: the session may only start asking
    /// for something while the user is already reading it, and that should
    /// not leave a badge behind them.
    private func markRead(_ session: SessionSummary) async {
        guard let snapshot = store.snapshot else { return }
        await AttentionWatcher(notifier: notifier).markRead(session.id, in: snapshot)
    }

    /// Host id → display title from the fleet snapshot, so provider cards can
    /// name the machines a subscription covers ("Mac Mini, NAS") rather than
    /// showing raw ids.
    private var hostTitles: [String: String] {
        Dictionary(
            (store.snapshot?.hosts ?? []).map { ($0.id, $0.title) },
            uniquingKeysWith: { first, _ in first }
        )
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
