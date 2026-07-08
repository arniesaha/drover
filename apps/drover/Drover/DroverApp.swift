import SwiftUI
import UserNotifications
import NexusKit

@main
struct DroverApp: App {
    @State private var environment = AppEnvironment()
    @Environment(\.scenePhase) private var scenePhase
    private let notifier: Notifying = LocalNotifier()

    init() {
        // Must happen before the app finishes launching, so this lives in
        // `init()` rather than an `.onAppear`/`.task` (BGTaskScheduler's
        // documented requirement).
        BackgroundRefresh.register(notifier: notifier)
    }

    var body: some Scene {
        WindowGroup {
            RootView(environment: environment, notifier: notifier)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                BackgroundRefresh.schedule()
            }
        }
    }
}

/// Composition root: no client configured → onboarding `SettingsView`;
/// otherwise a `NavigationStack` over `SessionsView` with Settings reachable
/// from the toolbar gear. `.id(environment.generation)` forces `SessionsView`
/// (and the `SessionStore` it owns) to rebuild whenever `configure()`
/// succeeds, including re-configuring while already authenticated.
private struct RootView: View {
    var environment: AppEnvironment
    let notifier: Notifying
    @State private var showSettings = false

    var body: some View {
        Group {
            if let client = environment.client {
                NavigationStack {
                    SessionsView(client: client, notifier: notifier)
                        .toolbar {
                            ToolbarItem(placement: .topBarTrailing) {
                                Button {
                                    showSettings = true
                                } label: {
                                    Image(systemName: "gearshape")
                                }
                            }
                        }
                }
                .id(environment.generation)
                .sheet(isPresented: $showSettings) {
                    NavigationStack {
                        SettingsView(environment: environment)
                    }
                }
            } else {
                NavigationStack {
                    SettingsView(environment: environment)
                }
            }
        }
        // Covers both a returning user (client already configured at launch,
        // `generation` still 0) and a fresh onboarding success (`generation`
        // bumps): request once a client exists, either way. Re-requesting is
        // harmless — the system only prompts the user once and just returns
        // the existing authorization afterward.
        .task { await requestNotificationPermissionIfConfigured() }
        .onChange(of: environment.generation) { _, _ in
            Task { await requestNotificationPermissionIfConfigured() }
        }
    }

    private func requestNotificationPermissionIfConfigured() async {
        guard environment.client != nil else { return }
        _ = try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .badge, .sound])
    }
}
