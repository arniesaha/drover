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
    private let gate: DemoActivityGate

    init(gate: DemoActivityGate = .shared) {
        self.gate = gate
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (
            UNNotificationPresentationOptions
        ) -> Void
    ) {
        guard !gate.isActive else {
            completionHandler([])
            return
        }
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
        guard !gate.isActive else {
            completionHandler()
            return
        }
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
    @State private var environment: AppEnvironment
    @State private var demoSession: DemoSession?
    @Environment(\.scenePhase) private var scenePhase
    private let notifier: Notifying
#if DEBUG
    private let testScenario: UITestScenario?
#endif
    // The notification center holds its delegate weakly — keep it alive for
    // the app's lifetime.
    private let notificationPresenter = ForegroundNotificationPresenter()
    // The only route an APNs device token has into the app: the remote-
    // notification delegate callbacks have no SwiftUI equivalent.
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var pushDelegate

    init() {
#if DEBUG
        // Select the fixture before constructing anything that reads the
        // operator's saved connection, recovery files, or preferences.
        let scenario = UITestScenario()
        self.testScenario = scenario
        if let scenario {
            _environment = State(initialValue: scenario.environment)
            notifier = FixtureNotifier()
            return
        }
#endif
        _environment = State(initialValue: AppEnvironment())
        notifier = LocalNotifier()
        // Must happen before the app finishes launching, so this lives in
        // `init()` rather than an `.onAppear`/`.task` (BGTaskScheduler's
        // documented requirement).
        BackgroundRefresh.register(notifier: notifier)
        UNUserNotificationCenter.current().delegate = notificationPresenter
    }

    var body: some Scene {
        WindowGroup {
#if DEBUG
            if let testScenario {
                if let demoSession {
                    DemoRoot(session: demoSession, onReset: resetDemo, onExit: exitDemo)
                } else {
                    FixturePreparedRoot(
                        scenario: testScenario,
                        notifier: notifier,
                        onTryDemo: enterDemo
                    )
                }
            } else if ProcessInfo.processInfo.environment[
                "DROVER_UI_TEST_CHAT_HEADER_FIXTURE"
            ] == "1" {
                ChatHeaderFixtureRoot()
            } else {
                appRoot
            }
#else
            appRoot
#endif
        }
        .onChange(of: scenePhase) { _, phase in
#if DEBUG
            guard testScenario == nil else { return }
#endif
            guard demoSession == nil else { return }
            if phase == .background {
                BackgroundRefresh.schedule()
            }
        }
    }

    @ViewBuilder
    private var appRoot: some View {
        if let demoSession {
            DemoRoot(session: demoSession, onReset: resetDemo, onExit: exitDemo)
        } else {
            RootView(
                environment: environment,
                notifier: notifier,
                onTryDemo: enterDemo
            )
        }
    }

    @MainActor
    private func enterDemo() {
        guard demoSession == nil, let session = try? DemoSession() else { return }
        // Set every external-operation gate before the demo root is made
        // visible. The existing production environment remains allocated and
        // untouched behind this replacement view.
        DemoActivityGate.shared.activate()
        BackgroundRefresh.cancelActiveWorkForDemo()
        PushRegistrar.shared.setDemoSuspended(true)
        demoSession = session
    }

    @MainActor
    private func exitDemo() {
        guard let demoSession else { return }
        demoSession.end()
        self.demoSession = nil
        DemoActivityGate.shared.deactivate()
        PushRegistrar.shared.setDemoSuspended(false)
    }

    @MainActor
    private func resetDemo() {
        guard let previous = demoSession, let replacement = try? DemoSession() else { return }
        // Replacing the isolated environment also strands any departing
        // ChatModel's debounced write in its old in-memory recovery actor.
        // The new tree therefore starts from a genuinely empty local state.
        previous.end()
        demoSession = replacement
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

/// Composition root: no client configured → onboarding `OnboardingView`;
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
    @State private var appearance: AppearanceStore
    private let defaults: UserDefaults
    private let chatModelFactory: ChatModelFactory?
    private let backgroundActivityEnabled: Bool
    private let onTryDemo: () -> Void
    private let demoSession: DemoSession?
    private let onResetDemo: () -> Void
    private let onExitDemo: () -> Void

    init(environment: AppEnvironment, notifier: Notifying,
         defaults: UserDefaults = .standard,
         chatModelFactory: ChatModelFactory? = nil,
         backgroundActivityEnabled: Bool = true,
         onTryDemo: @escaping () -> Void = {},
         demoSession: DemoSession? = nil,
         onResetDemo: @escaping () -> Void = {},
         onExitDemo: @escaping () -> Void = {}) {
        self.environment = environment
        self.notifier = notifier
        self.defaults = defaults
        self.chatModelFactory = chatModelFactory
        self.backgroundActivityEnabled = backgroundActivityEnabled
        self.onTryDemo = onTryDemo
        self.demoSession = demoSession
        self.onResetDemo = onResetDemo
        self.onExitDemo = onExitDemo
        _appearance = State(initialValue: AppearanceStore(defaults: defaults))
    }

    var body: some View {
        Group {
            if let client = environment.client {
                NavigationStack {
                    SessionsView(
                        client: client,
                        notifier: notifier,
                        recoveryStore: environment.chatRecoveryStore,
                        recoveryWriteGate: environment.chatRecoveryWriteGate,
                        recoveryGeneration: environment.chatRecoveryGeneration,
                        chatModelFactory: chatModelFactory,
                        defaults: defaults,
                        notificationRoutingEnabled: demoSession == nil,
                        onOpenSettings: { showSettings = true }
                    )
                }
                .id(environment.generation)
                .sheet(isPresented: $showSettings) {
                    NavigationStack {
                        if let demoSession {
                            DemoSettingsView(
                                session: demoSession,
                                onReset: onResetDemo,
                                onExit: onExitDemo
                            )
                        } else {
                            SettingsView(environment: environment, onTryDemo: onTryDemo)
                        }
                    }
                    .presentationCornerRadius(24)
                }
            } else {
                NavigationStack {
                    OnboardingView(environment: environment, onTryDemo: onTryDemo)
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
            guard backgroundActivityEnabled else { return }
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
        guard backgroundActivityEnabled else { return }
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

/// A compact, persistent disclosure and escape hatch sits above the real
/// fleet/chat/launch views for the full demo visit. It is intentionally not a
/// mock screen: Reset rebuilds the same local SessionStore tree from bounded
/// state, and Exit restores the original environment still held by DroverApp.
private struct DemoRoot: View {
    let session: DemoSession
    let onReset: () -> Void
    let onExit: () -> Void

    var body: some View {
        RootView(
            environment: session.environment,
            notifier: DemoNotifier(),
            defaults: session.defaults,
            chatModelFactory: session.chatModelFactory,
            backgroundActivityEnabled: false,
            demoSession: session,
            onResetDemo: onReset,
            onExitDemo: onExit
        )
        .id(ObjectIdentifier(session))
        .safeAreaInset(edge: .top, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "play.circle.fill")
                Text("Demo")
                    .font(.caption.weight(.semibold))
                    .accessibilityIdentifier("demo-mode-label")
                Text("All actions run locally")
                    .font(.caption2)
                    .foregroundStyle(DroverColor.muted)
                Spacer(minLength: 0)
                Button("Reset", action: onReset)
                .font(.caption.weight(.semibold))
                .accessibilityIdentifier("demo-reset")
                Button("Reconnect") { session.simulateReconnect() }
                    .font(.caption.weight(.semibold))
                    .accessibilityIdentifier("demo-reconnect")
                Button("Exit") { onExit() }
                    .font(.caption.weight(.semibold))
                    .accessibilityIdentifier("demo-exit")
            }
            .foregroundStyle(DroverColor.accentHi)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(DroverColor.surface)
            .overlay(alignment: .bottom) { Divider().overlay(DroverColor.line) }
        }
    }
}

#if DEBUG
private struct FixturePreparedRoot: View {
    let scenario: UITestScenario
    let notifier: Notifying
    let onTryDemo: () -> Void
    @State private var isPrepared = false

    var body: some View {
        Group {
            if isPrepared {
                RootView(
                    environment: scenario.environment, notifier: notifier,
                    defaults: scenario.defaults,
                    chatModelFactory: scenario.makeClient().chatModelFactory,
                    backgroundActivityEnabled: false,
                    onTryDemo: onTryDemo
                )
                .overlay(alignment: .topTrailing) {
                    TimelineView(.periodic(from: .now, by: 0.2)) { _ in
                        VStack {
                            Text(String(scenario.transport.receiptState.receiptCount))
                                .accessibilityIdentifier("fixture-turn-receipt-count")
                            Text(String(scenario.transport.receiptState.submissionCount))
                                .accessibilityIdentifier("fixture-turn-submission-count")
                        }
                        .font(.caption2)
                        .allowsHitTesting(false)
                    }
                }
            } else {
                ProgressView("Preparing synthetic journey")
            }
        }
        .task {
            do {
                try await scenario.prepare()
                isPrepared = true
            } catch {
                preconditionFailure("Could not prepare isolated synthetic journey")
            }
        }
    }
}
#endif
