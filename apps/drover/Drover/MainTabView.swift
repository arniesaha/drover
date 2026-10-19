import DroverKit
import SwiftUI

/// The root navigation dock of the application.
///
/// Presents a floating translucent dock hosting:
/// - Home (`SessionsView`)
/// - Inbox (`InboxView`)
/// - Analytics (`AnalyticsView`)
/// - Accounts (`ProviderAccountsView`)
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
    @State private var workStore: WorkAttentionStore
    @State private var inboxAttentionStore: InboxAttentionStore
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

    private var inboxBadgeCount: Int {
        InboxAttentionPresentation.count(
            workItems: workStore.items,
            sessions: sessionStore.snapshot?.sessions ?? [],
            insights: inboxAttentionStore.insights
        )
    }

    private var inboxBadge: String? {
        InboxAttentionPresentation.badgeValue(for: inboxBadgeCount)
    }

    private var accountsBadge: String? {
        AccountHealthPresentation.badgeValue(
            for: AccountHealthPresentation.count(accounts: cockpitStore.providerAccounts)
        )
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
                    onOpenSettings: onOpenSettings
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
                            NotificationRoute.shared.open(sessionID: sessionID)
                            selectedTab = .home
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

        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        // Reserve the exact lane occupied by the floating dock. Descendants
        // receive the adjusted safe area, so their own safe-area insets (such
        // as Home's New Session button) keep the final row above both controls.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Color.clear.frame(height: 76).allowsHitTesting(false)
        }
        .overlay(alignment: .bottom) {
            FloatingDock(
                selectedTab: $selectedTab,
                inboxBadge: inboxBadge,
                accountsBadge: accountsBadge
            )
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 14)
            .padding(.bottom, 8)
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

/// A compact app-owned dock avoids the system tab bar's opaque full-width
/// background while retaining explicit labels, selected state, and badges.
struct FloatingDock: View {
    @Binding var selectedTab: AppTab
    let inboxBadge: String?
    let accountsBadge: String?

    var body: some View {
        HStack(spacing: 2) {
            item(.home)
            item(.inbox, badge: inboxBadge)
            item(.analytics)
            item(.accounts, badge: accountsBadge)
        }
        .padding(5)
        .frame(maxWidth: .infinity)
        .frame(maxWidth: 430)
        .floatingDockSurface(in: Capsule())
    }

    @ViewBuilder
    private func item(_ tab: AppTab, badge: String? = nil) -> some View {
        Button {
            selectedTab = tab
        } label: {
            VStack(spacing: 3) {
                ZStack(alignment: .topTrailing) {
                    Image(systemName: tab.systemImage)
                        .font(.system(size: 16, weight: .semibold))
                    if let badge {
                        Text(badge)
                            .font(.system(size: 10, weight: .bold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(.white)
                            .padding(.horizontal, badge.count > 1 ? 4 : 0)
                            .frame(minWidth: 16, minHeight: 16)
                            .background(Circle().fill(DroverColor.warn))
                            .offset(x: 8, y: -7)
                            .accessibilityHidden(true)
                    }
                }
                Text(tab.title)
                    .font(.caption2.weight(tab == selectedTab ? .semibold : .regular))
                    .lineLimit(1)
            }
            .foregroundStyle(tab == selectedTab ? DroverColor.accentHi : DroverColor.muted)
            .frame(maxWidth: .infinity, minHeight: 52)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("dock-\(tab.rawValue)")
        .accessibilityLabel(accessibilityLabel(for: tab, badge: badge))
        .accessibilityAddTraits(tab == selectedTab ? .isSelected : [])
    }

    private func accessibilityLabel(for tab: AppTab, badge: String?) -> String {
        guard let badge else { return tab.title }
        let noun = badge == "1" ? "account needs attention" : "accounts need attention"
        if tab == .accounts { return "\(tab.title), \(badge) \(noun)" }
        return "\(tab.title), \(badge) unread"
    }
}
