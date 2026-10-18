import SwiftUI
import DroverKit

/// The fleet inbox: a wordmark row carrying the two app-level controls, a
/// pinned status header (the count that says how much wants you, the host
/// strip that says where the herd is, and the provider capacity strip), then
/// one list of live sessions, over a pinned "New Session" footer.
///
/// At standard text sizes only the list scrolls. At accessibility sizes the
/// status header scrolls with it to leave room for sessions. The action bar stays
/// available below. At standard sizes the header above it and action bar stay
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
    @State private var workStore: WorkAttentionStore
    @State private var focusedAttentionID: String?
    @State private var attentionFocusRevision = 0
    @State private var store: SessionStore
    @State private var cockpitStore: CockpitStore
    private let client: DroverClient
    private let notifier: Notifying
    private let recoveryStore: (any ChatRecoveryPersisting)?
    private let recoveryWriteGate: ChatRecoveryWriteGate
    private let recoveryGeneration: Int
    private let chatModelFactory: ChatModelFactory?
    private let defaults: UserDefaults
    private let onOpenSettings: () -> Void
    private let onOpenAccountsTab: (() -> Void)?
    @Environment(\.scenePhase) private var scenePhase
    @Environment(AppearanceStore.self) private var appearance
    @State private var exporterFreshness = ExporterFreshnessGate()
    @State private var showLaunch = false
    @State private var launchedSession: LaunchedSession?
    @State private var showFinished = false
    @State private var showAnalytics = false
    @State private var showAccounts = false
    @State private var showInsights = false
    @State private var showHistory = false
    @State private var notificationSessionIDs: Set<String>?
    @State private var filterState: FleetFilterState
    @State private var isOverviewExpanded = false

    init(
        client: DroverClient,
        notifier: Notifying = LocalNotifier(),
        recoveryStore: (any ChatRecoveryPersisting)?,
        recoveryWriteGate: ChatRecoveryWriteGate,
        recoveryGeneration: Int,
        chatModelFactory: ChatModelFactory? = nil,
        defaults: UserDefaults = .standard,
        sessionStore: SessionStore? = nil,
        cockpitStore: CockpitStore? = nil,
        onOpenSettings: @escaping () -> Void = {},
        onOpenAccountsTab: (() -> Void)? = nil
    ) {
        self.client = client
        self.notifier = notifier
        self.recoveryStore = recoveryStore
        self.recoveryWriteGate = recoveryWriteGate
        self.recoveryGeneration = recoveryGeneration
        self.chatModelFactory = chatModelFactory
        self.defaults = defaults
        self.onOpenSettings = onOpenSettings
        self.onOpenAccountsTab = onOpenAccountsTab
        _filterState = State(initialValue: FleetFilterState.load(from: defaults))
        _workStore = State(initialValue: WorkAttentionStore(service: client))
        _store = State(initialValue: sessionStore ?? SessionStore(client: client))
        _cockpitStore = State(initialValue: cockpitStore ?? CockpitStore(client: client))
    }

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(spacing: 0) {
            chromeRow
            if notificationSessionIDs != nil {
                HStack {
                    Text("Notification sessions")
                    Spacer()
                    Button("Show all") { notificationSessionIDs = nil }
                }
                .padding(.horizontal, 18)
            }

            if !dynamicTypeSize.isAccessibilitySize {
                statusHeader
            }

            if let warning = exporterFreshness.warningText {
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.vertical, 8)
                    .accessibilityIdentifier("exporterFreshnessWarning")
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        if dynamicTypeSize.isAccessibilitySize { statusHeader }
                        if workStore.isVisible {
                            WorkAttentionList(store: workStore, focusedID: focusedAttentionID) { sessionID in
                                NotificationRoute.shared.open(sessionID: sessionID)
                                openSessionFromNotification()
                            }
                            .id("work-attention")
                        }
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
                            if filterState.isFiltered {
                                ContentUnavailableView(
                                    "No matching sessions",
                                    systemImage: "line.3.horizontal.decrease.circle",
                                    description: Text("Try clearing the active filter to see all sessions.")
                                )
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 40)
                            } else {
                                ContentUnavailableView("Nothing running",
                                                       systemImage: "rectangle.stack.badge.plus",
                                                       description: Text("Start a new session when you're ready."))
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 40)
                            }
                        } else {
                            ForEach(inboxSessions, id: \.id) { session in
                                row(for: session)
                                    .id(session.id)
                            }
                            .animation(.easeInOut(duration: 0.2), value: inboxSessions.map(\.id))
                        }

                        if notificationSessionIDs == nil, !finishedSessions.isEmpty {
                            finishedSection
                        }

                        // The live list holds only recent finished sessions per
                        // host; everything older, across every host, is History.
                        if store.hasLoadedOnce {
                            historyEntry
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
                .accessibilityIdentifier("fleet-list")
                .refreshable {
                    await workStore.refresh()
                    await store.refresh()
                    if let snapshot = store.snapshot {
                        await cockpitStore.refresh(for: snapshot)
                    }
                }
                .overlay {
                    if !store.hasLoadedOnce {
                        if let deadline = store.busyUntil {
                            Label(RetryPolicy.busyMessage(until: deadline), systemImage: "clock")
                                .droverText(.subtitle)
                        } else if let error = store.lastError {
                            if store.isTailscaleTransportFailure {
                                ContentUnavailableView {
                                    Label("Can't reach the hub over Tailscale", systemImage: "network.badge.shield.half.filled")
                                } description: {
                                    let host = client.config.tailscaleHost ?? client.config.baseURL.host ?? "Tailscale"
                                    Text("Your Drover hub is configured at a Tailscale address (\(host)). Check that Tailscale is connected on this device and that the hub is running.")
                                } actions: {
                                    Button("Retry") {
                                        Task { await store.refresh() }
                                    }
                                    .buttonStyle(.bordered)
                                }
                            } else {
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
                            }
                        } else {
                            // The spinner alone cannot distinguish a hub that is
                            // unreachable from a first load repeatedly torn down
                            // before it lands, which is exactly why a recurring
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

                .onChange(of: attentionFocusRevision) { _, _ in
                    if let id = focusedAttentionID { proxy.scrollTo(id, anchor: .top) }
                }
                .onChange(of: workStore.items) { _, _ in
                    if let id = focusedAttentionID { proxy.scrollTo(id, anchor: .top) }
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
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                await workStore.refresh()
                if workStore.isReachable {
                    await WorkAttentionNotifications.evaluate(workStore.items, notifier: notifier, defaults: defaults)
                }
                openAttentionFromNotification()
                guard let interval = workStore.pollIntervalSeconds else { return }
                do { try await Task.sleep(for: .seconds(interval)) }
                catch { return }
            }
        }
        .onChange(of: NotificationRoute.shared.pendingAttentionRef) { _, _ in
            openAttentionFromNotification()
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                do {
                    let health = try await client.exporterHealth()
                    guard !Task.isCancelled else { return }
                    exporterFreshness.fetched(health)
                }
                catch { /* Keep the last known warning during transient failures. */ }
                do { try await Task.sleep(for: .seconds(15)) }
                catch { return }
            }
        }
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
        //
        // Once the hub is pushing it announces every transition itself, and
        // `AttentionWatcher` falls back to badge-and-seen-set only — otherwise
        // each pushed alert was followed by a second, generic, local one.
        .onChange(of: store.needsYou) { _, _ in
            guard let snapshot = store.snapshot else { return }
            let watcher = AttentionWatcher(notifier: notifier, seenStore: defaults)
            Task { await watcher.evaluate(snapshot) }
        }
        // A tapped alert names one session; open that session rather than
        // leaving the user on the list to find it again themselves.
        .onChange(of: NotificationRoute.shared.pendingSessionIDs) { _, _ in
            openSessionFromNotification()
        }
        .onChange(of: NotificationRoute.shared.pendingSessionID) { _, _ in
            openSessionFromNotification()
        }
        // A cold launch delivers the tap before the first poll returns, so the
        // id cannot resolve to a session yet. Try again when the list arrives.
        .onChange(of: store.snapshot?.sessions.count) { _, _ in
            openSessionFromNotification()
        }
        .onAppear { openSessionFromNotification(); openAttentionFromNotification() }
        .sheet(isPresented: $showLaunch) {
            NavigationStack {
                LaunchView(
                    client: client, snapshot: store.snapshot,
                    catalogStore: HarnessModelCatalogStore(defaults: defaults)
                ) { sessionID, isStructured, harness in
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
            Group {
                if launched.isStructured {
                    ChatView(
                        client: client,
                        sessionID: launched.id,
                        harness: launched.harness,
                        recoveryStore: recoveryStore,
                        recoveryWriteGate: recoveryWriteGate,
                        recoveryGeneration: recoveryGeneration,
                        chatModelFactory: chatModelFactory,
                        onSessionEnded: { id, ended in store.markSessionEnded(id, ended: ended) }
                    )
                } else {
                    TerminalScreen(client: client, sessionID: launched.id, harness: launched.harness)
                }
            }
            .toolbar(.hidden, for: .tabBar)
        }
        .navigationDestination(isPresented: $showAnalytics) {
            AnalyticsView(store: cockpitStore)
                .toolbar(.hidden, for: .tabBar)
        }
        .navigationDestination(isPresented: $showAccounts) {
            ProviderAccountsView(
                accounts: cockpitStore.providerAccounts,
                status: providerSectionStatus,
                statusMessage: cockpitStore.providerError,
                hostTitles: hostTitles,
                onRefresh: {
                    await store.refresh()
                    if let snapshot = store.snapshot { await cockpitStore.refresh(for: snapshot) }
                }
            )
            .toolbar(.hidden, for: .tabBar)
        }
        .navigationDestination(isPresented: $showInsights) {
            InsightsView(client: client, store: cockpitStore)
                .toolbar(.hidden, for: .tabBar)
        }
        .navigationDestination(isPresented: $showHistory) {
            HistoryView(client: client)
                .toolbar(.hidden, for: .tabBar)
        }
    }

    @ViewBuilder
    private var statusHeader: some View {
        if store.hasLoadedOnce {
            VStack(alignment: .leading, spacing: 8) {
                InboxStatusHeader(
                    summary: summary,
                    hostGroups: store.hostGroups,
                    selectedHostID: filterState.hostID,
                    onSelectHost: { hostID in
                        withAnimation(.easeInOut(duration: 0.2)) {
                            filterState.hostID = hostID
                            filterState.save(to: defaults)
                        }
                    },
                    isOverviewExpanded: isOverviewExpanded,
                    onToggleOverview: {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            isOverviewExpanded.toggle()
                        }
                    },
                    onRetry: { Task { await store.refresh() } }
                ) { providerCapacity }
                .accessibilityIdentifier("home-status-header")

                if isOverviewExpanded {
                    FleetOverviewView(
                        overview: store.overview,
                        filterState: filterState,
                        onSelectCell: { hostID, project in
                            withAnimation(.easeInOut(duration: 0.2)) {
                                if filterState.hostID == hostID && filterState.project == project {
                                    filterState.clear()
                                } else {
                                    filterState.hostID = hostID
                                    filterState.project = project
                                }
                                filterState.save(to: defaults)
                            }
                        },
                        onSelectHost: { hostID in
                            withAnimation(.easeInOut(duration: 0.2)) {
                                if filterState.hostID == hostID {
                                    filterState.hostID = nil
                                } else {
                                    filterState.hostID = hostID
                                }
                                filterState.save(to: defaults)
                            }
                        },
                        onSelectProject: { project in
                            withAnimation(.easeInOut(duration: 0.2)) {
                                if filterState.project == project {
                                    filterState.project = nil
                                } else {
                                    filterState.project = project
                                }
                                filterState.save(to: defaults)
                            }
                        }
                    )
                    .padding(.horizontal, 14)
                }

                if store.overview.projects.count > 1 {
                    projectStrip
                        .padding(.horizontal, 14)
                }

                if filterState.isFiltered {
                    filterBanner
                        .padding(.horizontal, 14)
                }
            }
        }
    }

    private var projectStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        filterState.project = nil
                        filterState.save(to: defaults)
                    }
                } label: {
                    Text("All Projects")
                        .font(.system(.caption, design: .default, weight: filterState.project == nil ? .medium : .regular))
                        .foregroundStyle(filterState.project == nil ? DroverColor.accentHi : DroverColor.muted)
                        .padding(.horizontal, 9)
                        .background {
                            if filterState.project == nil {
                                Capsule().fill(DroverColor.accentTint)
                            }
                        }
                        .overlay {
                            Capsule().strokeBorder(filterState.project == nil ? DroverColor.accent : DroverColor.line, lineWidth: filterState.project == nil ? 1.5 : 1)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("project-chip-all")

                ForEach(store.overview.projects) { proj in
                    let isSelected = filterState.project == proj.name
                    Button {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            if filterState.project == proj.name {
                                filterState.project = nil
                            } else {
                                filterState.project = proj.name
                            }
                            filterState.save(to: defaults)
                        }
                    } label: {
                        HStack(spacing: 5) {
                            Text(proj.name)
                                .font(.system(.caption, design: .default, weight: isSelected ? .medium : .regular))
                                .foregroundStyle(isSelected ? DroverColor.accentHi : DroverColor.text)
                                .lineLimit(1)
                            Text("\(proj.activeCount)")
                                .droverText(.marker)
                                .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.faint))
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 5)
                        .background {
                            if isSelected {
                                Capsule().fill(DroverColor.accentTint)
                            }
                        }
                        .overlay {
                            Capsule().strokeBorder(isSelected ? DroverColor.accent : DroverColor.line, lineWidth: isSelected ? 1.5 : 1)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("project-chip-\(proj.name)")
                    .accessibilityLabel("\(proj.name), \(proj.activeCount) active\(isSelected ? ", selected" : "")")
                }
            }
        }
        .scrollClipDisabled()
    }

    private var filterBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "line.3.horizontal.decrease.circle.fill")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)

            Text(filterDescriptionText)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(DroverColor.text)
                .lineLimit(1)
                .truncationMode(.middle)

            Text("(\(inboxSessions.count) active)")
                .droverText(.marker)
                .foregroundStyle(DroverColor.muted)

            Spacer(minLength: 8)

            Button("Clear") {
                withAnimation(.easeInOut(duration: 0.2)) {
                    filterState.clear()
                    filterState.save(to: defaults)
                }
            }
            .font(.system(.caption2, design: .default, weight: .semibold))
            .foregroundStyle(DroverColor.accentHi)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(DroverColor.bg, in: Capsule())
            .overlay {
                Capsule().strokeBorder(DroverColor.accent, lineWidth: 1)
            }
            .accessibilityIdentifier("clear-filters-button")
            .accessibilityLabel("Clear active filters")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(DroverColor.accentTint, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(DroverColor.accent.opacity(0.4), lineWidth: 1)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("active-filters-banner")
    }

    private var filterDescriptionText: String {
        var parts: [String] = []
        if let hostID = filterState.hostID {
            let title = store.snapshot?.hosts.first { $0.id == hostID }?.title ?? hostID
            parts.append(title)
        }
        if let project = filterState.project {
            parts.append(project)
        }
        return parts.joined(separator: " · ")
    }

    private var historyEntry: some View {
        Button { showHistory = true } label: {
            CockpitCard {
                HStack(spacing: 12) {
                    Image(systemName: "clock.arrow.circlepath").foregroundStyle(DroverColor.accentHi)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("History").droverText(.h2)
                        Text("Every session across your hosts").droverText(.subtitle)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(DroverColor.faint)
                }
                .frame(minHeight: 44, alignment: .leading)
            }
        }
        .buttonStyle(.plain)
        .padding(.top, 4)
        .accessibilityIdentifier("history-entry")
    }

    private var summary: FleetSummaryPresentation {
        FleetSummaryPresentation(
            snapshot: store.snapshot,
            isReachable: store.isReachable,
            error: store.lastError,
            isTailscaleTransportFailure: store.isTailscaleTransportFailure
        )
    }

    /// The filtered live list, in one piece: needs-you first, then working, each
    /// stabilized by hysteresis.
    private var inboxSessions: [SessionSummary] {
        if let ids = notificationSessionIDs {
            return store.snapshot?.sessions.filter { ids.contains($0.id) } ?? []
        }
        return filterState.filter(store.inboxSessions)
    }

    private var finishedSessions: [SessionSummary] {
        filterState.filter(store.finished)
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
                onOpenAccounts: {
                    if let onOpenAccountsTab {
                        onOpenAccountsTab()
                    } else {
                        showAccounts = true
                    }
                }
            )
        }
    }

    /// Everything that is *about* the fleet rather than *in* it. Below the
    /// list, where it can no longer split it.
    @ViewBuilder
    private var analyticsSections: some View {
        Button { showAnalytics = true } label: {
            CockpitCard {
                HStack(spacing: 12) {
                    Image(systemName: "chart.bar").foregroundStyle(DroverColor.accentHi)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Analytics").droverText(.h2)
                        Text("Projects, agents, hosts and models").droverText(.subtitle)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(DroverColor.faint)
                }
                .frame(minHeight: 44, alignment: .leading)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("analytics-summary")

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
            FinishedRow(count: finishedSessions.count, isExpanded: showFinished) {
                withAnimation(.snappy(duration: 0.2)) { showFinished.toggle() }
            }

            if showFinished {
                VStack(spacing: 10) {
                    ForEach(finishedSessions, id: \.id) { session in
                        row(for: session).id(session.id)
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
                        recapSourceSeq: session.recapSourceSeq,
                        recoveryStore: recoveryStore,
                        recoveryWriteGate: recoveryWriteGate,
                        recoveryGeneration: recoveryGeneration,
                        chatModelFactory: chatModelFactory,
                        onSessionEnded: { id, ended in store.markSessionEnded(id, ended: ended) }
                    )
                } else {
                    TerminalScreen(client: client, sessionID: session.id, harness: session.harness)
                }
            }
            .toolbar(.hidden, for: .tabBar)
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
            .disabled(!crossHarnessTargets(for: session).contains(session.harness))
            // Cross-harness targets from the session's host (via the polled
            // snapshot): structured launchers only — a PTY-only target would
            // execute the typed-in handoff seed as commands.
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

    private func openAttentionFromNotification() {
        guard let ref = NotificationRoute.shared.pendingAttentionRef else { return }
        guard workStore.isReachable else { return }
        notificationSessionIDs = nil
        launchedSession = nil
        focusedAttentionID = workStore.items.first {
            $0.workspaceID == ref.workspaceID && $0.id == ref.id
        }?.id ?? "work-attention"
        attentionFocusRevision += 1
        _ = NotificationRoute.shared.consumeAttention()
    }

    /// Marked on open and again on leaving: the session may only start asking
    /// for something while the user is already reading it, and that should
    /// not leave a badge behind them.
    /// Push straight into the session a tapped alert was about.
    ///
    /// Reuses the destination `LaunchView` already navigates through, so a
    /// notification lands on exactly the screen launching that session would.
    /// The id is only consumed once a matching session is actually in the
    /// snapshot: a cold launch delivers the tap before the first poll returns,
    /// and dropping it there would strand the user on the list.
    private func openSessionFromNotification() {
        if let ids = NotificationRoute.shared.consumeSummary() {
            notificationSessionIDs = Set(ids)
            launchedSession = nil
            return
        }
        guard let pending = NotificationRoute.shared.pendingSessionID else { return }
        guard let session = store.snapshot?.sessions.first(where: { $0.id == pending }) else {
            return
        }
        notificationSessionIDs = nil
        _ = NotificationRoute.shared.consume()
        launchedSession = LaunchedSession(
            id: session.id,
            isStructured: session.isStructured,
            harness: session.harness
        )
    }

    private func markRead(_ session: SessionSummary) async {
        guard let snapshot = store.snapshot else { return }
        await AttentionWatcher(notifier: notifier, seenStore: defaults).markRead(session.id, in: snapshot)
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
        store.snapshot?.hosts.first { $0.id == session.hostID }?.title ?? session.hostDisplayName ?? session.hostID
    }

    /// Harnesses the session's host advertises a structured launch for. The
    /// handoff seed becomes their first turn; a PTY-only target would type it
    /// into a terminal as commands, so those are left out.
    private func crossHarnessTargets(for session: SessionSummary) -> [String] {
        let offers = store.snapshot?.hosts
            .first { $0.id == session.hostID }?
            .launchableOffers ?? []
        return offers.filter { $0.launchMode == .structured }.map(\.name)
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
