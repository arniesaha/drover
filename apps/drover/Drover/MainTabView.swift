import DroverKit
import SwiftUI

/// The root navigation dock of the application.
///
/// Presents the five first-class destinations.  The system tab bar becomes a
/// sidebar automatically on iPad, preserving the same information
/// architecture at every size class.
///
/// Hosts:
/// - Home (`SessionsView`)
/// - Inbox (`InboxView`)
/// - Analytics (`AnalyticsView`)
/// - Accounts (`ProviderAccountsView`)
/// - Settings (`SettingsView`)
struct MainTabView: View {
    let client: DroverClient
    let environment: AppEnvironment
    let notifier: Notifying
    let defaults: UserDefaults
    let chatModelFactory: ChatModelFactory?

    @State private var sessionStore: SessionStore
    @State private var cockpitStore: CockpitStore
    @State private var workStore: WorkAttentionStore
    @State private var inboxAttentionStore: InboxAttentionStore
    @State private var selectedTab: AppTab
    @State private var showGlobalLaunch = false
    @State private var inboxSession: InboxSessionRoute?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.scenePhase) private var scenePhase

    init(
        client: DroverClient,
        environment: AppEnvironment,
        notifier: Notifying,
        defaults: UserDefaults = .standard,
        chatModelFactory: ChatModelFactory? = nil,
        initialTab: AppTab? = nil,
        sessionStore: SessionStore? = nil,
        cockpitStore: CockpitStore? = nil
    ) {
        self.client = client
        self.environment = environment
        self.notifier = notifier
        self.defaults = defaults
        self.chatModelFactory = chatModelFactory
        _sessionStore = State(initialValue: sessionStore ?? SessionStore(client: client))
        _cockpitStore = State(initialValue: cockpitStore ?? CockpitStore(client: client))
        _workStore = State(initialValue: WorkAttentionStore(service: client))
        _inboxAttentionStore = State(initialValue: InboxAttentionStore(client: client))
        _selectedTab = State(initialValue: initialTab ?? AppTab.load(from: defaults, default: .home))
    }

    private var providerSectionStatus: DataStatus {
        let responseStatus = cockpitStore.overview?.providerCapacity.status ?? .ok
        if cockpitStore.providerError != nil, responseStatus == .ok {
            return .unknown
        }
        return responseStatus
    }

    private var hostTitles: [String: String] {
        Dictionary(
            (sessionStore.snapshot?.hosts ?? []).map { ($0.id, $0.title) },
            uniquingKeysWith: { first, _ in first }
        )
    }

    @ViewBuilder
    private func tabLabel(for tab: AppTab) -> some View {
        if dynamicTypeSize.isAccessibilitySize {
            Image(systemName: tab.systemImage)
                .accessibilityLabel(tab.title)
        } else {
            Label(tab.title, systemImage: tab.systemImage)
        }
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            NavigationStack {
                SessionsView(
                    client: client,
                    isDemoMode: environment.isDemoMode,
                    notifier: notifier,
                    recoveryStore: environment.chatRecoveryStore,
                    recoveryWriteGate: environment.chatRecoveryWriteGate,
                    recoveryGeneration: environment.chatRecoveryGeneration,
                    chatModelFactory: chatModelFactory,
                    defaults: defaults,
                    sessionStore: sessionStore,
                    cockpitStore: cockpitStore,
                    onOpenInbox: { selectedTab = .inbox },
                    showsNewSessionAction: false
                )
            }
            .tabItem { tabLabel(for: .home) }
            .tag(AppTab.home)
            .accessibilityLabel(AppTab.home.title)
            .accessibilityIdentifier(AppTab.home.accessibilityIdentifier)

            NavigationStack {
                LazyTabRoot(isSelected: selectedTab == .inbox) {
                    InboxView(
                        client: client,
                        sessionStore: sessionStore,
                        cockpitStore: cockpitStore,
                        workStore: workStore,
                        attentionStore: inboxAttentionStore,
                        onOpenSession: { sessionID in
                            guard let session = sessionStore.snapshot?.sessions.first(where: { $0.id == sessionID }) else {
                                return
                            }
                            inboxSession = InboxSessionRoute(session: session)
                        }
                    )
                }
            }
            .tabItem { Label("Inbox", systemImage: "tray.full") }
            .tag(AppTab.inbox)
            .accessibilityLabel(AppTab.inbox.title)
            .accessibilityIdentifier(AppTab.inbox.accessibilityIdentifier)

            NavigationStack {
                LazyTabRoot(isSelected: selectedTab == .analytics) {
                    AnalyticsView(store: cockpitStore)
                }
            }
            .tabItem { tabLabel(for: .analytics) }
            .tag(AppTab.analytics)
            .accessibilityLabel(AppTab.analytics.title)
            .accessibilityIdentifier(AppTab.analytics.accessibilityIdentifier)

            NavigationStack {
                LazyTabRoot(isSelected: selectedTab == .accounts) {
                    ProviderAccountsView(
                        accounts: cockpitStore.providerAccounts,
                        status: providerSectionStatus,
                        statusMessage: cockpitStore.providerError,
                        hostTitles: hostTitles,
                        onRefresh: {
                            await sessionStore.refresh()
                            if let snapshot = sessionStore.snapshot {
                                await cockpitStore.refresh(for: snapshot)
                            }
                        }
                    )
                }
            }
            .tabItem { tabLabel(for: .accounts) }
            .tag(AppTab.accounts)
            .accessibilityLabel(AppTab.accounts.title)
            .accessibilityIdentifier(AppTab.accounts.accessibilityIdentifier)

            NavigationStack {
                LazyTabRoot(isSelected: selectedTab == .settings) {
                    SettingsView(environment: environment, showsDismissButton: false)
                }
            }
            .tabItem { tabLabel(for: .settings) }
            .tag(AppTab.settings)
            .accessibilityLabel(AppTab.settings.title)
            .accessibilityIdentifier(AppTab.settings.accessibilityIdentifier)
        }
        // iOS 18 renders this as the familiar tab bar on iPhone and promotes
        // the same ordered destinations to a persistent sidebar on iPad.
        .tabViewStyle(.sidebarAdaptable)
        // This remains available from every destination on compact layouts,
        // rather than being embedded in (and duplicated by) the Home feed.
        .safeAreaInset(edge: .bottom, alignment: .trailing, spacing: 0) {
            NewSessionBar { showGlobalLaunch = true }
                .padding(.trailing, DroverSpace.x4)
                .padding(.vertical, DroverSpace.x2)
        }
        .sheet(isPresented: $showGlobalLaunch) {
            NavigationStack {
                LaunchView(
                    client: client,
                    snapshot: sessionStore.snapshot,
                    providerAccounts: cockpitStore.providerAccounts,
                    catalogStore: HarnessModelCatalogStore(defaults: defaults)
                ) { _, _, _ in
                    // The polling Home view receives the newly launched
                    // session; returning there keeps launch a global action,
                    // not an Inbox workflow.
                    selectedTab = .home
                }
                .navigationTitle("New Session")
            }
            .presentationDetents([.large])
            .presentationCornerRadius(DroverRadius.sheet)
        }
        // A detail launched in Inbox is presented over that navigation stack.
        // Dismissing it therefore lands exactly back in Inbox, instead of
        // silently switching the user to Home.
        .sheet(item: $inboxSession) { route in
            NavigationStack {
                Group {
                    if route.isStructured {
                        ChatView(
                            client: environment.isDemoMode ? nil : client,
                            sessionID: route.id,
                            harness: route.harness,
                            recap: route.recap,
                            recapSourceSeq: route.recapSourceSeq,
                            recoveryStore: environment.chatRecoveryStore,
                            recoveryWriteGate: environment.chatRecoveryWriteGate,
                            recoveryGeneration: environment.chatRecoveryGeneration,
                            chatModelFactory: chatModelFactory,
                            isDemoMode: environment.isDemoMode
                        )
                    } else {
                        TerminalScreen(client: client, sessionID: route.id, harness: route.harness)
                    }
                }
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Inbox") { inboxSession = nil }
                    }
                }
            }
        }
        .onChange(of: selectedTab) { _, newTab in
            newTab.save(to: defaults)
        }
        .onChange(of: NotificationRoute.shared.pendingSessionID) { _, pending in
            if pending != nil {
                selectedTab = .home
                selectedTab.save(to: defaults)
            }
        }
        .onChange(of: NotificationRoute.shared.pendingAttentionRef) { _, pending in
            if pending != nil {
                selectedTab = .inbox
                selectedTab.save(to: defaults)
            }
        }
        .onChange(of: NotificationRoute.shared.pendingSessionIDs) { _, pending in
            if pending != nil {
                selectedTab = .home
                selectedTab.save(to: defaults)
            }
        }
        .onAppear {
            if NotificationRoute.shared.pendingSessionID != nil
                || NotificationRoute.shared.pendingAttentionRef != nil
                || NotificationRoute.shared.pendingSessionIDs != nil {
                selectedTab = NotificationRoute.shared.pendingAttentionRef == nil ? .home : .inbox
            }
        }
        .task { sessionStore.startPolling() }
        .task(id: sessionStore.snapshot?.cockpitAPIVersion) {
            guard let snapshot = sessionStore.snapshot else {
                cockpitStore.updateCapability(from: nil)
                return
            }
            cockpitStore.updateCapability(from: snapshot)
            await inboxAttentionStore.refresh(isAvailable: cockpitStore.isInsightsAvailable)
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                await workStore.refresh()
                if workStore.isReachable {
                    await WorkAttentionNotifications.evaluate(workStore.items, notifier: notifier, defaults: defaults)
                }
                guard let interval = workStore.pollIntervalSeconds else { return }
                do { try await Task.sleep(for: .seconds(interval)) }
                catch { return }
            }
        }
    }
}

private struct InboxSessionRoute: Identifiable {
    let id: String
    let isStructured: Bool
    let harness: String
    let recap: String?
    let recapSourceSeq: Int?

    init(session: SessionSummary) {
        id = session.id
        isStructured = session.isStructured
        harness = session.harness
        recap = session.recap ?? session.preview
        recapSourceSeq = session.recapSourceSeq
    }
}

/// Defers constructing and loading a tab's view hierarchy until the tab is first visited,
/// preventing unneeded network requests, tasks, and accessibility snapshot overhead on launch.
private struct LazyTabRoot<Content: View>: View {
    let isSelected: Bool
    @ViewBuilder let content: () -> Content
    @State private var hasAppeared = false

    var body: some View {
        Group {
            if hasAppeared || isSelected {
                content()
                    .onAppear { hasAppeared = true }
            } else {
                Color.clear
            }
        }
    }
}
