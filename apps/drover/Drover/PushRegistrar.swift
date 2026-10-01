import DroverKit
import UIKit

/// Receives the APNs device token from iOS and hands it to the hub.
///
/// Two independent things have to be true before the token can be uploaded:
/// iOS has to have issued one, and the app has to hold a configured
/// `DroverClient` to send it with. They complete in either order — a returning
/// user has a client at launch and waits on APNs, a user finishing onboarding
/// already has a token and gains a client — so this holds whichever arrives
/// first and uploads once both exist, rather than assuming a sequence.
///
/// A singleton because the thing it models is genuinely process-global: iOS
/// issues one APNs token per app, not one per view or per client.
@MainActor
final class PushRegistrar {
    static let shared = PushRegistrar()

    private var deviceToken: Data?
    private var client: DroverClient?
    /// The token verified with the current hub in this process. Repeated iOS
    /// callbacks need no upload after success; cold launches always revalidate.
    private var uploadedToken: Data?

    private var uploadGeneration = 0

    init() {
        // Persisted success belongs to a previous process, not a verified hub
        // in this launch. Keep fallback available until registration succeeds.
        PushRegistration.setActive(false)
    }

    /// Ask iOS for a token. Safe to call repeatedly: iOS returns the existing
    /// token rather than minting a new one, so this can follow every
    /// authorization check without special-casing the first launch.
    func requestTokenFromSystem() {
        UIApplication.shared.registerForRemoteNotifications()
    }

    func accept(token: Data) {
        deviceToken = token
        uploadIfReady()
    }

    /// Called whenever the app's client changes — onboarding completing, or a
    /// reconfigure pointed at a different hub.
    func updateClient(_ client: DroverClient?) {
        uploadGeneration += 1
        self.client = client
        // A different hub has never seen this token, so let it be re-sent.
        // Until that upload succeeds, the new hub is not a proven announcer:
        // clear any stale success from the previous hub so local fallback is
        // never suppressed during migrations or re-pairing.
        uploadedToken = nil
        PushRegistration.setActive(false)
        uploadIfReady()
    }

    /// Drop the registration server-side. Used on sign-out, so a signed-out
    /// phone stops lighting up for a fleet it no longer belongs to.
    func unregister() async {
        let previousClient = client
        updateClient(nil)
        try? await previousClient?.unregisterAPNsToken()
    }

    private func uploadIfReady() {
        guard let client, let deviceToken, deviceToken != uploadedToken else { return }
        uploadGeneration += 1
        let generation = uploadGeneration
        Task {
            guard generation == uploadGeneration else { return }
            do {
                try await client.registerAPNsToken(deviceToken)
                guard generation == uploadGeneration else { return }
                uploadedToken = deviceToken
                // From here the hub announces every awaiting transition, so
                // the app's own watcher must stop doing it too.
                PushRegistration.setActive(true)
            } catch {
                guard generation == uploadGeneration else { return }
                uploadedToken = nil
                // Leave `uploadedToken` unset so the next launch or
                // reconfigure retries. If a previous launch believed hub push
                // was active, clear that stale state now: a failed upload is
                // evidence the hub cannot promise the APNs path for this
                // install, and local notifications must resume immediately.
                PushRegistration.setActive(false)
                NSLog("drover: APNs token upload failed: \(error.localizedDescription)")
            }
        }
    }
}

/// Thin shim: `didRegisterForRemoteNotificationsWithDeviceToken` has no
/// SwiftUI equivalent, so the token can only arrive through a
/// `UIApplicationDelegate`. It holds no state of its own — the adaptor is free
/// to construct it whenever it likes.
final class PushAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken token: Data
    ) {
        Task { @MainActor in
            PushRegistrar.shared.accept(token: token)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // Not fatal and not worth surfacing: the app still has the foreground
        // watcher and the BGTask poller behind this. The usual cause on a
        // development build is simply no network at launch.
        NSLog("drover: APNs registration failed: \(error.localizedDescription)")
    }
}
