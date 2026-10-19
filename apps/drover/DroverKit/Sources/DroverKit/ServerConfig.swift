import Foundation
import Network

/// The host classes ATS permits for plain HTTP when
/// `NSAllowsLocalNetworking` is enabled. Keep this separate from reachability:
/// a Tailscale DNS name can be reachable, but a fully-qualified name still
/// needs HTTPS under the Store build's ATS policy.
public enum HTTPHostClassification: Sendable, Equatable {
    case rfc1918IPv4
    case uniqueLocalIPv6
    case linkLocalIP
    case loopbackIP
    case carrierGradeNAT
    case publicIP
    case localDomain
    case hostname
}

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

    /// True when `baseURL` points to a Tailscale node, detected via IPv4 CGNAT
    /// range (100.64.0.0/10) or Tailscale hostnames (*.ts.net, *.tailscale.net, or containing .tailnet).
    public var isTailscaleAddress: Bool {
        guard let host = baseURL.host else { return false }
        return Self.isTailscale(host: host)
    }

    /// The host part if `baseURL` is a Tailscale address, else `nil`.
    public var tailscaleHost: String? {
        isTailscaleAddress ? baseURL.host : nil
    }

    /// Host category used to decide whether a plain-HTTP endpoint remains
    /// valid in the Release and StoreRelease ATS policy.
    public var httpHostClassification: HTTPHostClassification? {
        guard let host = baseURL.host else { return nil }
        return Self.classifyHTTPHost(host)
    }

    /// Store builds intentionally permit HTTP only for RFC1918 IPv4,
    /// link-local and loopback addresses, plus `.local` names. Carrier-grade
    /// NAT addresses (including Tailscale's 100.64.0.0/10 range), public IPs,
    /// IPv6 ULAs, and ordinary hostnames need HTTPS.
    public var requiresHTTPSForAppTransportSecurity: Bool {
        baseURL.scheme?.lowercased() == "http"
            && !(httpHostClassification?.allowsPlainHTTP ?? false)
    }

    /// User-facing copy shared by QR/manual pairing and saved-config recovery.
    public static let httpsRePairMessage = "iOS blocked this plain-HTTP hub address for transport security. Use the hub’s HTTPS Tailscale (*.ts.net) URL (Tailscale Serve), or a local LAN/.local address."

    public static func classifyHTTPHost(_ host: String) -> HTTPHostClassification {
        let cleaned = host.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        let lower = cleaned.lowercased()
        if lower == "localhost" {
            return .loopbackIP
        }
        if let octets = ipv4Octets(lower) {
            if octets[0] == 10
                || (octets[0] == 172 && (16...31).contains(octets[1]))
                || (octets[0] == 192 && octets[1] == 168) {
                return .rfc1918IPv4
            }
            if octets[0] == 169 && octets[1] == 254 {
                return .linkLocalIP
            }
            if octets[0] == 127 {
                return .loopbackIP
            }
            if octets[0] == 100 && (64...127).contains(octets[1]) {
                return .carrierGradeNAT
            }
            return .publicIP
        }
        if let address = IPv6Address(lower) {
            let bytes = Array(address.rawValue)
            if bytes.dropLast().allSatisfy({ $0 == 0 }) && bytes.last == 1 {
                return .loopbackIP
            }
            if (bytes[0] & 0xfe) == 0xfc {
                return .uniqueLocalIPv6
            }
            if bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80 {
                return .linkLocalIP
            }
            return .publicIP
        }
        if lower.hasSuffix(".local") {
            return .localDomain
        }
        return .hostname
    }

    /// Detects if a host string is a Tailscale IPv4 address (CGNAT 100.64.0.0/10)
    /// or a Tailscale domain (*.ts.net, *.tailscale.net, or containing .tailnet).
    public static func isTailscale(host: String) -> Bool {
        let cleaned = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return false }

        // Strip port if present (e.g. host:port or [ipv6]:port)
        let hostPart: String
        if cleaned.hasPrefix("[") && cleaned.contains("]") {
            let end = cleaned.firstIndex(of: "]")!
            hostPart = String(cleaned[cleaned.index(after: cleaned.startIndex)..<end])
        } else if let colonIndex = cleaned.firstIndex(of: ":") {
            hostPart = String(cleaned[..<colonIndex])
        } else {
            hostPart = cleaned
        }

        let lower = hostPart.lowercased()
        if isTailscaleIPv4(lower) {
            return true
        }
        if lower == "ts.net" || lower.hasSuffix(".ts.net")
            || lower == "tailscale.net" || lower.hasSuffix(".tailscale.net")
            || lower.contains(".tailnet") {
            return true
        }
        return false
    }

    /// Detects if a URL string resolves to a Tailscale endpoint.
    public static func isTailscale(urlString: String) -> Bool {
        if let config = ServerConfig(urlString: urlString) {
            return config.isTailscaleAddress
        }
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        if let components = URLComponents(string: trimmed), let host = components.host {
            return isTailscale(host: host)
        }
        return isTailscale(host: trimmed)
    }

    private static func isTailscaleIPv4(_ host: String) -> Bool {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        guard let octet0 = UInt8(parts[0]),
              let octet1 = UInt8(parts[1]),
              let _ = UInt8(parts[2]),
              let _ = UInt8(parts[3]) else {
            return false
        }
        return octet0 == 100 && (64...127).contains(octet1)
    }

    private static func ipv4Octets(_ host: String) -> [UInt8]? {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        let octets = parts.compactMap { UInt8($0) }
        return octets.count == 4 ? octets : nil
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

private extension HTTPHostClassification {
    var allowsPlainHTTP: Bool {
        switch self {
        case .rfc1918IPv4, .linkLocalIP, .loopbackIP, .localDomain:
            return true
        case .uniqueLocalIPv6, .carrierGradeNAT, .publicIP, .hostname:
            return false
        }
    }
}
