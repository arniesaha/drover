import Foundation

/// The central server's base URL, persisted in `UserDefaults` (not sensitive —
/// the bearer token lives in the Keychain via `TokenStore`, never here).
public struct ServerConfig: Sendable, Equatable {
    public var baseURL: URL

    /// Trims whitespace, rejects empty input, and prepends `http://` when the
    /// input has no scheme/host of its own (e.g. a bare `host:port`).
    public init?(urlString: String) {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        let components = URLComponents(string: trimmed)
        let hasSchemeAndHost = components?.scheme != nil && components?.host != nil

        let candidate = hasSchemeAndHost ? trimmed : "http://\(trimmed)"

        guard let url = URL(string: candidate), url.host != nil else { return nil }
        self.baseURL = url
    }

    public static let defaultsKey = "drover.server.url"

    public static func load(defaults: UserDefaults = .standard) -> ServerConfig? {
        guard let stored = defaults.string(forKey: defaultsKey) else { return nil }
        return ServerConfig(urlString: stored)
    }

    public func save(defaults: UserDefaults = .standard) {
        defaults.set(baseURL.absoluteString, forKey: Self.defaultsKey)
    }

    #if DEBUG
    /// DEBUG-only override so automated simulator smoke tests never need to
    /// type credentials by hand: reads `DROVER_BASE_URL` / `DROVER_TOKEN` from
    /// the process environment. Both must be present or this returns nil.
    public static func debugOverride() -> (config: ServerConfig, token: String)? {
        let environment = ProcessInfo.processInfo.environment
        guard let baseURLString = environment["DROVER_BASE_URL"],
              let token = environment["DROVER_TOKEN"],
              let config = ServerConfig(urlString: baseURLString)
        else {
            return nil
        }
        return (config, token)
    }
    #else
    public static func debugOverride() -> (config: ServerConfig, token: String)? {
        nil
    }
    #endif
}
