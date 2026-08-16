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
    private(set) var client: DroverClient?
    private(set) var config: ServerConfig?

    /// Bumped on every successful `configure()` so views that hold a
    /// `SessionStore` built from `client` can force-recreate it (SwiftUI
    /// `.id(_:)`) even when a client already existed before reconfiguring.
    private(set) var generation = 0

    private let defaults: UserDefaults
    private let tokenStore: TokenStore

    init(
        defaults: UserDefaults = .standard,
        tokenStore: TokenStore = TokenStore(),
        launchEnvironment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.defaults = defaults
        self.tokenStore = tokenStore
        if UITestOverrides.shouldResetAuthentication(environment: launchEnvironment) {
            try? tokenStore.delete()
            defaults.removeObject(forKey: ServerConfig.defaultsKey)
        }
        if let built = ClientFactory.make(defaults: defaults, tokenStore: tokenStore) {
            client = built.client
            config = built.config
        }
    }

    var hasTokenConfigured: Bool {
        tokenStore.load() != nil
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

        // Server-checking logic lives in DroverKit so it's unit-testable;
        // this stays a thin caller.
        if let failure = await ClientFactory.validate(config: newConfig, token: trimmedToken) {
            return .failure(failure)
        }

        newConfig.save(defaults: defaults)
        do {
            try tokenStore.save(trimmedToken)
        } catch {
            return .failure("Could not save token to Keychain.")
        }

        config = newConfig
        client = DroverClient(config: newConfig, token: trimmedToken)
        generation += 1
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
    func signOut() {
        try? tokenStore.delete()
        defaults.removeObject(forKey: ServerConfig.defaultsKey)
        client = nil
        config = nil
        // Bump so a view holding a store built from the old client rebuilds
        // rather than rendering against a dead one.
        generation += 1
    }
}
