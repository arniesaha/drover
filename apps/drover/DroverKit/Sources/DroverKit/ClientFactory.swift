import Foundation

/// Builds a `DroverClient` from whatever configuration is available, so
/// `AppEnvironment` (app target) doesn't have to know the precedence rules
/// itself — this is the one place that logic lives, and it's unit-testable.
public enum ClientFactory {
    /// DEBUG env override wins (simulator smoke tests supply
    /// `DROVER_BASE_URL`/`DROVER_TOKEN` so no credentials need to be typed);
    /// otherwise the persisted `ServerConfig` plus the Keychain token are
    /// used. Background callers leave `credentialBindingID` nil: this factory
    /// never reads or writes foreground recovery metadata.
    ///
    /// `endpointIsAllowed` is where a restricted build (an internal TestFlight
    /// stage lock) decides whether an endpoint may be dialled at all. It has
    /// to be enforced *here*, on both branches: a caller-side check cannot see
    /// the DEBUG override this factory prefers over the saved config, and
    /// `BackgroundRefresh` builds its own client from the same UserDefaults
    /// and Keychain with no `AppEnvironment` in the process to ask.
    public static func make(defaults: UserDefaults = .standard,
                             tokenStore: TokenStore = TokenStore(),
                             credentialBindingID: UUID? = nil,
                             endpointIsAllowed: (URL) -> Bool = { _ in true })
        -> (client: DroverClient, config: ServerConfig)? {
        if let override = ServerConfig.debugOverride() {
            guard endpointIsAllowed(override.config.baseURL) else { return nil }
            return (DroverClient(config: override.config, token: override.token), override.config)
        }
        guard let config = ServerConfig.load(defaults: defaults),
              endpointIsAllowed(config.baseURL),
              let token = tokenStore.load()
        else {
            return nil
        }
        return (
            DroverClient(
                config: config,
                token: token,
                credentialBindingID: credentialBindingID
            ),
            config
        )
    }

    /// Checks a candidate server + token against the live server: `healthz()`
    /// first (reachability, no auth), then `snapshot()` (token + API shape).
    /// Returns `nil` on success, else a user-facing error message. This is
    /// the server-checking logic behind `AppEnvironment.configure`, kept
    /// here so it's testable against an injected `URLSession`.
    public static func validate(config: ServerConfig, token: String,
                                session: URLSession = .shared) async -> String? {
        let client = DroverClient(config: config, token: token, session: session)
        do {
            guard try await client.healthz() else {
                return "Server did not respond to health check."
            }
            _ = try await client.snapshot()
            return nil
        } catch let error as DroverError where error == .unauthorized {
            return "Token rejected by server."
        } catch {
            return "Could not reach server: \(error.localizedDescription)"
        }
    }
}
