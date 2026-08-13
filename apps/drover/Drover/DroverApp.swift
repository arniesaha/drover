import SwiftUI
import UserNotifications
import DroverKit

/// Without a `UNUserNotificationCenterDelegate`, iOS silently drops any
/// local notification that fires while the app is foregrounded — which is
/// exactly when the near-real-time "response completed" alerts from the
/// polling path arrive. Presenting as a banner (not an alert) keeps them
/// glanceable.
/// `@MainActor` here is load-bearing, not decoration.
///
/// These delegate methods are `async`. Without actor isolation Swift resumes
/// them on the cooperative pool, and when they return UIKit does its own
/// post-completion work — `_updateSnapshotAndStateRestoration`, through
/// `_performBlockAfterCATransactionCommitSynchronizes` — on whatever thread
/// the continuation landed on. Off the main thread that trips an internal
/// UIKit assertion and the process aborts, so *every* notification tap killed
/// the app: three crash reports on device in one afternoon, all SIGABRT, all
/// faulting in `didReceive` on `com.apple.root.user-initiated-qos.cooperative`.
private final class ForegroundNotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (
            UNNotificationPresentationOptions
        ) -> Void
    ) {
        completionHandler([.banner, .sound, .badge])
    }

    /// A "needs you" alert is about one session, so tapping it should land on
    /// that session rather than the list. Handles both kinds identically: the
    /// hub's push carries `session_id`, and `LocalNotifier` writes the same
    /// key (and uses the id as the request identifier).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let request = response.notification.request
        // Parsed here: pure, and it keeps the non-Sendable response off the
        // hop below.
        let sessionID = NotificationRoute.sessionID(
            userInfo: request.content.userInfo,
            requestIdentifier: request.identifier
        )
        // The completion-handler form of this delegate method is delivered on
        // the main thread, which is the whole reason for preferring it: the
        // handler is what releases UIKit to do its own post-tap work, and
        // that work aborts the process if it runs anywhere else.
        // `assumeIsolated` states that contract rather than assuming it
        // quietly — if it were ever violated this would fail loudly here
        // instead of deep inside UIKit.
        MainActor.assumeIsolated {
            if let sessionID { NotificationRoute.shared.open(sessionID: sessionID) }
        }
        completionHandler()
    }
}

@main
struct DroverApp: App {
    @State private var environment = AppEnvironment()
    @Environment(\.scenePhase) private var scenePhase
    private let notifier: Notifying = LocalNotifier()
    // The notification center holds its delegate weakly — keep it alive for
    // the app's lifetime.
    private let notificationPresenter = ForegroundNotificationPresenter()
    // The only route an APNs device token has into the app: the remote-
    // notification delegate callbacks have no SwiftUI equivalent.
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var pushDelegate

    init() {
        // Must happen before the app finishes launching, so this lives in
        // `init()` rather than an `.onAppear`/`.task` (BGTaskScheduler's
        // documented requirement).
        BackgroundRefresh.register(notifier: notifier)
        UNUserNotificationCenter.current().delegate = notificationPresenter
    }

    var body: some Scene {
        WindowGroup {
#if DEBUG
            if ProcessInfo.processInfo.environment[
                "DROVER_UI_TEST_CHAT_HEADER_FIXTURE"
            ] == "1" {
                ChatHeaderFixtureRoot()
            } else {
                RootView(environment: environment, notifier: notifier)
            }
#else
            RootView(environment: environment, notifier: notifier)
#endif
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                BackgroundRefresh.schedule()
            }
        }
    }
}

#if DEBUG
private struct ChatHeaderFixtureRoot: View {
    private let title = ProcessInfo.processInfo.environment[
        "DROVER_UI_TEST_CHAT_HEADER_TITLE"
    ] ?? "Chat recap fixture"
    private let metadata = ProcessInfo.processInfo.environment[
        "DROVER_UI_TEST_CHAT_HEADER_METADATA"
    ] ?? "Codex · ctx 0"

    var body: some View {
        NavigationStack {
            Color.clear
                .navigationTitle("Chat")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .principal) {
                        ChatHeaderContent(title: title, metadata: metadata)
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            Button("Fixture action") {}
                        } label: {
                            Image(systemName: "ellipsis.circle")
                        }
                        .accessibilityLabel("Session actions")
                        .accessibilityIdentifier("chat-menu")
                    }
                }
        }
        .dynamicTypeSize(.accessibility5)
    }
}
#endif

/// Composition root: no client configured → onboarding `SettingsView`;
/// otherwise a `NavigationStack` over `SessionsView`, whose own header row
/// carries the theme toggle and the way into Settings (the design puts both
/// beside the wordmark rather than in a navigation bar, so there is no toolbar
/// gear any more). `.id(environment.generation)` forces `SessionsView` (and
/// the `SessionStore` it owns) to rebuild whenever `configure()` succeeds,
/// including re-configuring while already authenticated.
///
/// The appearance preference is applied here and nowhere else: one
/// `.preferredColorScheme` over the whole tree is what keeps every palette
/// token — and both sheets — on the same ramp.
private struct RootView: View {
    var environment: AppEnvironment
    let notifier: Notifying
    @State private var showSettings = false
    @State private var appearance = AppearanceStore()

    var body: some View {
        Group {
            if let client = environment.client {
                NavigationStack {
                    SessionsView(
                        client: client,
                        notifier: notifier,
                        onOpenSettings: { showSettings = true }
                    )
                }
                .id(environment.generation)
                .sheet(isPresented: $showSettings) {
                    NavigationStack {
                        SettingsView(environment: environment)
                    }
                    .presentationCornerRadius(24)
                }
            } else {
                NavigationStack {
                    SettingsView(environment: environment)
                }
            }
        }
        .environment(appearance)
        .preferredColorScheme(appearance.appearance.colorScheme)
        .droverTint()
        // Covers both a returning user (client already configured at launch,
        // `generation` still 0) and a fresh onboarding success (`generation`
        // bumps): request once a client exists, either way. Re-requesting is
        // harmless — the system only prompts the user once and just returns
        // the existing authorization afterward.
        .task {
            await requestNotificationPermissionIfConfigured()
            // Keep a refresh request pending from launch, not just from the
            // next background transition — a force-quit cancels pending
            // BGTasks, and without this the gap lasts until the user
            // backgrounds the app again. Submitting is idempotent (replaces
            // any pending request for the same identifier).
            BackgroundRefresh.schedule()
        }
        .onChange(of: environment.generation) { _, _ in
            Task { await requestNotificationPermissionIfConfigured() }
        }
    }

    private func requestNotificationPermissionIfConfigured() async {
        guard let client = environment.client else { return }
        let granted = (try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .badge, .sound])) ?? false

        // Hand over the client regardless of the authorization answer: a
        // token already registered with the hub must still be refreshed (or
        // re-pointed at a new hub) even if the user has since turned alerts
        // off, and a device with no token simply never uploads one.
        PushRegistrar.shared.updateClient(client)

        // Asking for the token requires authorization — without it iOS never
        // calls back, and with it this is idempotent (the existing token is
        // returned rather than a new one minted).
        guard granted else { return }
        PushRegistrar.shared.requestTokenFromSystem()
    }
}
