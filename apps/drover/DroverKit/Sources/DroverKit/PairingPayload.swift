import Foundation

/// A scanned `drover://` pairing QR.
///
/// The payload is deliberately terse — QR density is what decides whether a
/// code rendered in a terminal window is scannable at all — so this parser
/// stays strict about the few fields that are there. Anything it does not
/// recognise is rejected rather than guessed at: a half-understood payload
/// would point the app at the wrong host with a code that cannot work.
public struct PairingPayload: Equatable, Sendable {
    public let serverURL: URL
    public let code: String
    public let fleetName: String?

    /// Only version 1 exists. A newer QR means the app is older than the hub,
    /// which is worth rejecting here rather than failing later on a 400.
    private static let supportedVersion = "1"

    public init?(scanned: String) {
        let trimmed = scanned.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
              components.scheme == "drover",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.path.isEmpty || components.path == "/",
              components.fragment == nil,
              components.port.map({ (1...65535).contains($0) }) ?? true
        else { return nil }

        let items = components.queryItems ?? []
        let allowed = Set(["v", "code", "tls", "n"])
        guard items.allSatisfy({ allowed.contains($0.name) }),
              Set(items.map(\.name)).count == items.count,
              items.first(where: { $0.name == "tls" }).map({ ["0", "1"].contains($0.value ?? "") }) ?? true
        else { return nil }
        func value(_ name: String) -> String? {
            guard let found = items.first(where: { $0.name == name })?.value,
                  !found.isEmpty
            else { return nil }
            return found
        }

        guard value("v") == Self.supportedVersion else { return nil }
        guard let code = value("code") else { return nil }

        let scheme = value("tls") == "1" ? "https" : "http"
        var authority = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        if let port = components.port {
            authority += ":\(port)"
        }
        guard let url = URL(string: "\(scheme)://\(authority)") else { return nil }

        self.serverURL = url
        self.code = code
        self.fleetName = value("n")
    }
}
