import DroverKit
import SwiftUI

/// The root navigation dock of the application.
///
/// Presents a floating translucent dock hosting:
/// - Analytics (`AnalyticsView`)
/// - Insights (`InsightsView`)
/// - Accounts (`ProviderAccountsView`)
/// - Home (`SessionsView`)
///
/// Preserves per-tab navigation stacks and scroll position, handles deep linking
/// (notification tap -> session), and ensures the floating dock does not obscure
/// content or controls.
struct MainTabView: View {
    let client: DroverClient
    let environment: AppEnvironment
    let notifier: Notifying
    let defaults: UserDefaults
    let chatModelFactory: ChatModelFactory?
    let onOpenSettings: () -> Void

    @State private var sessionStore: SessionStore
    @State private var cockpitStore: CockpitStore
    @State private var selectedTab: AppTab
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
        cockpitStore: CockpitStore? = nil,
        onOpenSettings: @escaping () -> Void
    ) {
        self.client = client
        self.environment = environment
        self.notifier = notifier
        self.defaults = defaults
        self.chatModelFactory = chatModelFactory
        self.onOpenSettings = onOpenSettings
        _sessionStore = State(initialValue: sessionStore ?? SessionStore(client: client))
        _cockpitStore = State(initialValue: cockpitStore ?? CockpitStore(client: client))
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
                AnalyticsView(store: cockpitStore)
            }
            .tabItem { tabLabel(for: .analytics) }
            .tag(AppTab.analytics)
            .accessibilityLabel(AppTab.analytics.title)
            .accessibilityIdentifier(AppTab.analytics.accessibilityIdentifier)

            NavigationStack {
                InsightsView(client: client, store: cockpitStore)
            }
            .tabItem { tabLabel(for: .insights) }
            .tag(AppTab.insights)
            .accessibilityLabel(AppTab.insights.title)
            .accessibilityIdentifier(AppTab.insights.accessibilityIdentifier)

            NavigationStack {
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
            .tabItem { tabLabel(for: .accounts) }
            .tag(AppTab.accounts)
            .accessibilityLabel(AppTab.accounts.title)
            .accessibilityIdentifier(AppTab.accounts.accessibilityIdentifier)

            NavigationStack {
                SessionsView(
                    client: client,
                    notifier: notifier,
                    recoveryStore: environment.chatRecoveryStore,
                    recoveryWriteGate: environment.chatRecoveryWriteGate,
                    recoveryGeneration: environment.chatRecoveryGeneration,
                    chatModelFactory: chatModelFactory,
                    defaults: defaults,
                    sessionStore: sessionStore,
                    cockpitStore: cockpitStore,
                    onOpenSettings: onOpenSettings,
                    onOpenAccountsTab: nil
                )
            }
            .tabItem { tabLabel(for: .home) }
            .tag(AppTab.home)
            .accessibilityLabel(AppTab.home.title)
            .accessibilityIdentifier(AppTab.home.accessibilityIdentifier)
        }
        .tabViewStyle(.sidebarAdaptable)
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
                selectedTab = .home
                selectedTab.save(to: defaults)
            }
        }
        .onChange(of: NotificationRoute.shared.pendingSessionIDs) { _, pending in
            if pending != nil {
                selectedTab = .home
                selectedTab.save(to: defaults)
            }
        }
        .task {
            sessionStore.startPolling()
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                await sessionStore.refresh()
            }
        }
        .task(id: sessionStore.snapshot?.cockpitAPIVersion) {
            guard let snapshot = sessionStore.snapshot else {
                cockpitStore.updateCapability(from: nil)
                return
            }
            cockpitStore.startForegroundPolling(for: snapshot)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                sessionStore.startPolling()
                if let snapshot = sessionStore.snapshot {
                    cockpitStore.startForegroundPolling(for: snapshot)
                }
            } else {
                sessionStore.stopPolling()
                cockpitStore.stopForegroundPolling()
            }
        }
        .onAppear {
            if NotificationRoute.shared.pendingSessionID != nil
                || NotificationRoute.shared.pendingAttentionRef != nil
                || NotificationRoute.shared.pendingSessionIDs != nil {
                selectedTab = .home
            }
        }
    }
}
