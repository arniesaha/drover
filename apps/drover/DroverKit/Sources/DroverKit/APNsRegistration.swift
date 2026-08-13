import Foundation

// MARK: - APNsEnvironment

/// Which half of Apple's push infrastructure minted this device token.
///
/// A token is only valid against the environment that issued it: presenting a
/// development token to `api.push.apple.com` fails with `BadDeviceToken` and
/// nothing else, so the server has to be told which host to use rather than
/// guessing. That is why the registration call carries it.
public enum APNsEnvironment: String, Sendable, Equatable {
    case sandbox
    case production

    /// Read from the embedded provisioning profile rather than `#if DEBUG`.
    ///
    /// The two disagree in a case this project actually hits: a Release-
    /// configuration build installed over `devicectl` with a development
    /// profile still receives a *sandbox* token, and a `#if DEBUG` check would
    /// call it production and register a token the server can never deliver
    /// to. The profile's `aps-environment` entitlement is the only thing that
    /// knows the truth. `#if DEBUG` remains the fallback for the Simulator and
    /// for unit-test bundles, neither of which embeds a profile.
    public static func current(bundle: Bundle = .main) -> APNsEnvironment {
        if let value = apsEnvironmentEntitlement(in: bundle) {
            return value == "production" ? .production : .sandbox
        }
        #if DEBUG
        return .sandbox
        #else
        return .production
        #endif
    }

    /// Pull `Entitlements.aps-environment` out of `embedded.mobileprovision`.
    ///
    /// The file is a CMS envelope wrapping an XML plist. Rather than decode
    /// the signature (which would need Security framework work for no benefit
    /// here), find the plist by its document markers and parse that slice.
    static func apsEnvironmentEntitlement(in bundle: Bundle) -> String? {
        guard
            let url = bundle.url(forResource: "embedded", withExtension: "mobileprovision"),
            let data = try? Data(contentsOf: url),
            let start = data.range(of: Data("<?xml".utf8)),
            let end = data.range(of: Data("</plist>".utf8), in: start.upperBound..<data.endIndex)
        else { return nil }

        let plistData = data[start.lowerBound..<end.upperBound]
        guard
            let plist = try? PropertyListSerialization.propertyList(
                from: plistData, options: [], format: nil
            ) as? [String: Any],
            let entitlements = plist["Entitlements"] as? [String: Any],
            let environment = entitlements["aps-environment"] as? String
        else { return nil }

        return environment
    }
}

// MARK: - Token formatting

extension Data {
    /// APNs device tokens travel as lowercase hex. `description` on `Data`
    /// used to render `<a1b2 c3d4>` and is not a documented format, so the
    /// conversion is explicit.
    public var apnsHexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

// The `DroverClient` calls themselves live in `DroverClient.swift`: its
// `request(path:method:body:)` helper is `private`, which in Swift reaches
// extensions in the same file only.
