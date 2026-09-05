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

    private let gate: DemoActivityGate
    private let requestSystemToken: () -> Void
    private let uploadToken: @Sendable (DroverClient, Data) async throws -> Void
    private let setPushActive: (Bool) -> Void
    private var deviceToken: Data?
    private var client: DroverClient?
    /// What the hub already has, so a relaunch with an unchanged token is not
    /// another PUT on every cold start.
    private var uploadedToken: Data?
    private var isDemoSuspended = false
    private var uploadGeneration = 0

    init(
        gate: DemoActivityGate = .shared,
        requestSystemToken: @escaping () -> Void = {
            UIApplication.shared.registerForRemoteNotifications()
        },
        uploadToken: @escaping @Sendable (DroverClient, Data) async throws -> Void = defaultUpload,
        setPushActive: @escaping (Bool) -> Void = { PushRegistration.setActive($0) }
    ) {
        self.gate = gate
        self.requestSystemToken = requestSystemToken
        self.uploadToken = uploadToken
        self.setPushActive = setPushActive
    }

    private nonisolated static func defaultUpload(
        client: DroverClient, token: Data
    ) async throws {
        try await client.registerAPNsToken(token)
    }

    /// Ask iOS for a token. Safe to call repeatedly: iOS returns the existing
    /// token rather than minting a new one, so this can follow every
    /// authorization check without special-casing the first launch.
    func requestTokenFromSystem() {
        guard !isDemoSuspended, !gate.isActive else { return }
        requestSystemToken()
    }

    func accept(token: Data) {
        deviceToken = token
        uploadIfReady()
    }

    /// Called whenever the app's client changes — onboarding completing, or a
    /// reconfigure pointed at a different hub.
    func updateClient(_ client: DroverClient?) {
        self.client = client
        // A different hub has never seen this token, so let it be re-sent.
        uploadedToken = nil
        uploadGeneration &+= 1
        uploadIfReady()
    }

    /// Entering demo mode leaves the real client and any system-issued token
    /// intact, but invalidates queued upload tasks and blocks every later
    /// callback from starting an upload. Exiting resumes the one pending
    /// registration if both values are already available.
    func setDemoSuspended(_ suspended: Bool) {
        guard isDemoSuspended != suspended else { return }
        isDemoSuspended = suspended
        uploadGeneration &+= 1
        if !suspended { uploadIfReady() }
    }

    /// Drop the registration server-side. Used on sign-out, so a signed-out
    /// phone stops lighting up for a fleet it no longer belongs to.
    func unregister() async {
        guard !isDemoSuspended, !gate.isActive else { return }
        guard let client else { return }
        try? await client.unregisterAPNsToken()
        uploadedToken = nil
        self.client = nil
        // Nothing is pushing any more, so local alerts are the only ones left.
        setPushActive(false)
    }

    private func uploadIfReady() {
        guard !isDemoSuspended, !gate.isActive,
              let client, let deviceToken, deviceToken != uploadedToken
        else { return }
        let generation = uploadGeneration
        Task { [weak self] in
            // This second gate closes the scheduled-Task race: entry can
            // happen after `uploadIfReady` queues this closure but before it
            // reaches the real client call.
            guard let self,
                  !self.isDemoSuspended,
                  !self.gate.isActive,
                  self.uploadGeneration == generation
            else { return }
            do {
                try await self.uploadToken(client, deviceToken)
                guard !self.isDemoSuspended,
                      !self.gate.isActive,
                      self.uploadGeneration == generation
                else { return }
                self.uploadedToken = deviceToken
                // From here the hub announces every awaiting transition, so
                // the app's own watcher must stop doing it too.
                self.setPushActive(true)
            } catch {
                // Leave `uploadedToken` unset so the next launch or
                // reconfigure retries. Push is best-effort; the foreground
                // watcher and BGTask poller still cover the user meanwhile.
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
