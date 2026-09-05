import Foundation
import Observation
import DroverKit

enum UITestOverrides {
    static func pairingDeviceName(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fallback: String
    ) -> String {
#if DEBUG
        let override = environment["DROVER_UI_TEST_DEVICE_NAME"]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let override, !override.isEmpty {
            return override
        }
#endif
        return fallback
    }

    static func shouldResetAuthentication(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> Bool {
#if DEBUG
        environment["DROVER_UI_TEST_RESET_AUTH"] == "1"
#else
        false
#endif
    }
}

/// App-wide configuration state: holds the current `DroverClient`/`ServerConfig`
/// (if any) and the one entry point (`configure`) that validates a candidate
/// server + token against the live server before persisting anything.
///
/// The actual "which source wins" logic (DEBUG env override vs. persisted
/// defaults + Keychain) lives in `DroverKit.ClientFactory` so it's unit
/// testable independent of SwiftUI.
@MainActor
@Observable
final class AppEnvironment {
    private static let recoveryRootCleanupPendingDefaultsKey = "drover.chat-recovery-cleanup-pending"

    private(set) var client: DroverClient?
    private(set) var config: ServerConfig?

    /// Bumped on every successful `configure()` so views that hold a
    /// `SessionStore` built from `client` can force-recreate it (SwiftUI
    /// `.id(_:)`) even when a client already existed before reconfiguring.
    private(set) var generation = 0
    /// A non-secret local state for Settings. Connectivity remains available
    /// when recovery metadata cannot be maintained, but the app must not
    /// imply that drafts or unresolved deliveries will survive recreation.
    private(set) var recoveryStatusMessage: String?
    private(set) var hasPendingLocalCleanup = false

    private let defaults: UserDefaults
    private let tokenStore: TokenStore
    private let recoveryBindingStore: RecoveryBindingStore
    private let recoveryStore: (any ChatRecoveryPersisting)?
    private let validator: @Sendable (ServerConfig, String) async -> String?
    private var operationEpoch = 0
    private var pendingCleanupBindingIDs = Set<UUID>()

    init(
        defaults: UserDefaults = .standard,
        tokenStore: TokenStore = TokenStore(),
        recoveryBindingStore: RecoveryBindingStore? = nil,
        recoveryStore: (any ChatRecoveryPersisting)? = nil,
        validator: @escaping @Sendable (ServerConfig, String) async -> String? = { config, token in
            await ClientFactory.validate(config: config, token: token)
        },
        launchEnvironment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.defaults = defaults
        self.tokenStore = tokenStore
        let resolvedBindingStore = recoveryBindingStore
            ?? RecoveryBindingStore(service: tokenStore.service)
        self.recoveryBindingStore = resolvedBindingStore
        self.recoveryStore = recoveryStore ?? Self.defaultRecoveryStore()
        self.validator = validator
        if UITestOverrides.shouldResetAuthentication(environment: launchEnvironment) {
            try? tokenStore.delete()
            try? resolvedBindingStore.clear()
            defaults.removeObject(forKey: ServerConfig.defaultsKey)
        }
        let savedConfig = ServerConfig.load(defaults: defaults)
        let savedToken = tokenStore.load()
        let startupBindingID: UUID?
        let shouldSweepRecovery: Bool
        if let savedConfig, let savedToken {
            do {
                startupBindingID = try resolvedBindingStore.binding(
                    forToken: savedToken,
                    serverURL: savedConfig.baseURL,
                    rotate: false
                )
                shouldSweepRecovery = true
            } catch {
                // Keychain access can be unavailable while the device is
                // locked. Preserve recovery files until this foreground path
                // can authenticate their binding again.
                startupBindingID = nil
                shouldSweepRecovery = false
                recoveryStatusMessage = "Chat recovery is unavailable until protected local storage can be read."
            }
        } else {
            startupBindingID = nil
            // A saved endpoint without a readable token is ambiguous (for
            // example, a locked Keychain), so it must not orphan-purge files.
            shouldSweepRecovery = savedConfig == nil
        }
        if let built = ClientFactory.make(
            defaults: defaults,
            tokenStore: tokenStore,
            credentialBindingID: startupBindingID
        ) {
            client = built.client
            config = built.config
        }
        let bindings = client?.credentialBindingID.map { Set([$0]) } ?? []
        let hasDurableRecoveryRootCleanup = defaults.bool(
            forKey: Self.recoveryRootCleanupPendingDefaultsKey
        )
        if hasDurableRecoveryRootCleanup, savedConfig == nil, savedToken == nil {
            hasPendingLocalCleanup = true
            recoveryStatusMessage = "Disconnected, but local chat recovery cleanup is still pending. Try Sign Out again."
            // This marker is written only after raw credential deletion. A
            // launch retry may therefore erase a corrupt authorization index
            // without weakening normal load/sweep fail-closed behavior.
            if let recoveryStore = self.recoveryStore {
                Task { @MainActor [weak self] in
                    do {
                        try await recoveryStore.eraseAllAfterCredentialDeletion()
                        guard let self else { return }
                        self.defaults.removeObject(forKey: Self.recoveryRootCleanupPendingDefaultsKey)
                        self.hasPendingLocalCleanup = false
                        self.recoveryStatusMessage = nil
                    } catch {
                        // Keep the durable marker and actionable state for the
                        // next launch or a repeated sign-out attempt.
                    }
                }
            }
        } else if shouldSweepRecovery, let recoveryStore = self.recoveryStore {
            Task {
                try? await recoveryStore.sweep(keeping: bindings)
            }
        }
    }

    var hasTokenConfigured: Bool {
        tokenStore.load() != nil
    }

    /// Q2 injects this exact actor into every foreground ChatModel. It must
    /// not create a second store for the same directory, because temporary
    /// cleanup and index writes are serialized by this actor instance.
    var chatRecoveryStore: (any ChatRecoveryPersisting)? {
        recoveryStore
    }

    enum ConfigureOutcome {
        case success
        case failure(String)
    }

    /// Validates `urlString`/`token` against the live server (`healthz()`
    /// then `snapshot()`) before saving anything. Only on success does this
    /// persist the URL to `UserDefaults`, save the token to the Keychain, and
    /// swap in the new client.
    func configure(urlString: String, token: String) async -> ConfigureOutcome {
        guard let newConfig = ServerConfig(urlString: urlString) else {
            return .failure("Enter a valid server URL.")
        }
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty else {
            return .failure("Enter the API token.")
        }

        operationEpoch &+= 1
        let expectedOperationEpoch = operationEpoch

        // Server-checking logic lives in DroverKit so it's unit-testable;
        // this stays a thin caller.
        if let failure = await validator(newConfig, trimmedToken) {
            return .failure(failure)
        }
        guard expectedOperationEpoch == operationEpoch else {
            return .failure("Configuration was cancelled.")
        }

        do {
            try tokenStore.save(trimmedToken)
        } catch {
            return .failure("Could not save token to Keychain.")
        }

        let oldBindingID = client?.credentialBindingID
        let bindingID: UUID?
        do {
            let createdBindingID = try recoveryBindingStore.binding(
                forToken: trimmedToken,
                serverURL: newConfig.baseURL,
                rotate: true
            )
            guard try recoveryBindingStore.binding(
                forToken: trimmedToken,
                serverURL: newConfig.baseURL,
                rotate: false
            ) == createdBindingID else {
                throw ChatRecoveryError.storageUnavailable
            }
            bindingID = createdBindingID
            recoveryStatusMessage = nil
        } catch {
            // Do not retain a prior namespace for an explicitly reconfigured
            // credential. Normal requests remain available without a binding.
            try? recoveryBindingStore.clear()
            bindingID = nil
            recoveryStatusMessage = "Chat recovery is unavailable until local storage can be repaired."
        }

        newConfig.save(defaults: defaults)
        config = newConfig
        client = DroverClient(
            config: newConfig,
            token: trimmedToken,
            credentialBindingID: bindingID
        )
        generation += 1
        var retryBindings = pendingCleanupBindingIDs
        if let oldBindingID, oldBindingID != bindingID {
            retryBindings.insert(oldBindingID)
        }
        var failedBindings = Set<UUID>()
        for oldBindingID in retryBindings {
            do {
                try await recoveryStore?.purge(bindingID: oldBindingID)
                pendingCleanupBindingIDs.remove(oldBindingID)
            } catch {
                failedBindings.insert(oldBindingID)
            }
        }
        pendingCleanupBindingIDs.formUnion(failedBindings)
        hasPendingLocalCleanup = !pendingCleanupBindingIDs.isEmpty
        if hasPendingLocalCleanup {
            recoveryStatusMessage = "Connected, but previous chat recovery is still pending local cleanup."
        }
        return .success
    }

    /// Forget this device's credential and return the app to onboarding.
    ///
    /// Local only, deliberately. The usual reason to sign out is to re-pair
    /// this same phone, and revoking server-side would also cut off anything
    /// else still holding that token. Revocation belongs on the hub, where it
    /// can name which credential is going away:
    ///
    ///     drover-server credentials list
    ///     drover-server credentials revoke <id>
    ///
    /// Everything a fresh install lacks is cleared, so the next pairing
    /// cannot inherit half the old configuration.
    func signOut() async throws {
        operationEpoch &+= 1
        let previousConfig = config
        // Drop the app's foreground connection before its first suspension so
        // no visible UI or new background work can use this credential.
        client = nil
        config = nil
        generation += 1

        do {
            try tokenStore.delete()
        } catch {
            // Keep the server endpoint for a truthful retry path. The client
            // stays disconnected until the credential deletion succeeds.
            config = previousConfig
            recoveryStatusMessage = "Could not remove the token from the Keychain. Sign out is incomplete; try again."
            throw SignOutError.credentialDeletion
        }

        defaults.removeObject(forKey: ServerConfig.defaultsKey)
        // Persist the destructive cleanup intent before the first cleanup
        // suspension. A process death while the store waits on protected I/O
        // must still authorize the next uncredentialed launch to remove a
        // corrupt index and its recovery bytes.
        defaults.set(true, forKey: Self.recoveryRootCleanupPendingDefaultsKey)
        var cleanupFailed = false
        do {
            try recoveryBindingStore.clear()
        } catch {
            cleanupFailed = true
        }

        // After raw credential deletion, no binding can legitimately retain
        // recovery. This deliberately bypasses the normal fail-closed index
        // path so a corrupt index cannot strand protected bytes forever.
        do {
            guard let recoveryStore else {
                throw ChatRecoveryError.storageUnavailable
            }
            try await recoveryStore.eraseAllAfterCredentialDeletion()
            defaults.removeObject(forKey: Self.recoveryRootCleanupPendingDefaultsKey)
        } catch {
            cleanupFailed = true
        }

        if cleanupFailed {
            pendingCleanupBindingIDs.removeAll()
            hasPendingLocalCleanup = true
            recoveryStatusMessage = "Disconnected, but local chat recovery cleanup is still pending. Try Sign Out again."
            throw SignOutError.localCleanupPending
        }

        pendingCleanupBindingIDs.removeAll()
        hasPendingLocalCleanup = false
        recoveryStatusMessage = nil
    }

    private static func defaultRecoveryStore() -> (any ChatRecoveryPersisting)? {
        guard let applicationSupport = try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ) else {
            return nil
        }
        return ChatRecoveryStore(
            root: applicationSupport
                .appendingPathComponent("Drover", isDirectory: true)
                .appendingPathComponent("ChatRecovery", isDirectory: true)
        )
    }
}

private enum SignOutError: LocalizedError {
    case credentialDeletion
    case localCleanupPending

    var errorDescription: String? {
        switch self {
        case .credentialDeletion:
            return "Could not remove the token from the Keychain. Sign out is incomplete; try again."
        case .localCleanupPending:
            return "Disconnected, but local chat recovery cleanup is still pending. Try Sign Out again."
        }
    }
}
