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
              let host = components.host, !host.isEmpty
        else { return nil }

        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            guard let found = items.first(where: { $0.name == name })?.value,
                  !found.isEmpty
            else { return nil }
            return found
        }

        guard value("v") == Self.supportedVersion else { return nil }
        guard let code = value("code") else { return nil }

        let scheme = value("tls") == "1" ? "https" : "http"
        var authority = host
        if let port = components.port {
            authority += ":\(port)"
        }
        guard let url = URL(string: "\(scheme)://\(authority)") else { return nil }

        self.serverURL = url
        self.code = code
        self.fleetName = value("n")
    }
}
