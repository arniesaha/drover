import Foundation
import Testing
@testable import DroverKit

// MARK: - Token encoding

@Test func deviceTokenEncodesAsLowercaseHex() {
    let token = Data([0x00, 0x0f, 0xa1, 0xff, 0x10])

    // Apple's own docs and every provider expect unpadded lowercase hex.
    // `Data.description` renders "<000fa1ff 10>", which is why this is
    // explicit rather than string-interpolated.
    #expect(token.apnsHexString == "000fa1ff10")
}

@Test func emptyTokenEncodesAsEmptyString() {
    #expect(Data().apnsHexString == "")
}

@Test func everyByteValueRoundTrips() {
    let token = Data((0...255).map { UInt8($0) })
    let hex = token.apnsHexString

    #expect(hex.count == 512)
    // Two chars per byte, no separators, no "0x" prefix.
    #expect(hex.hasPrefix("000102"))
    #expect(hex.hasSuffix("fdfeff"))
}

// MARK: - Environment

@Test func environmentRawValuesMatchTheServerContract() {
    // The server validates against exactly these two strings
    // (credentials.APNS_ENVIRONMENTS); a rename here is a 400 at runtime.
    #expect(APNsEnvironment.sandbox.rawValue == "sandbox")
    #expect(APNsEnvironment.production.rawValue == "production")
}

/// A bundle with no `embedded.mobileprovision`, which is what the Simulator
/// and the test bundle itself look like.
private final class BundleWithoutProfile: Bundle, @unchecked Sendable {
    override func url(forResource name: String?, withExtension ext: String?) -> URL? {
        nil
    }
}

@Test func missingProvisioningProfileFallsBackToBuildConfiguration() {
    let environment = APNsEnvironment.current(bundle: BundleWithoutProfile())

    // Tests only ever run against a DEBUG build of the package.
    #expect(environment == .sandbox)
}

@Test func missingProvisioningProfileYieldsNoEntitlement() {
    #expect(APNsEnvironment.apsEnvironmentEntitlement(in: BundleWithoutProfile()) == nil)
}

/// Serves a file whose bytes wrap an XML plist in noise, the way a real
/// `embedded.mobileprovision` wraps one in a CMS signature.
private final class BundleWithProfile: Bundle, @unchecked Sendable {
    nonisolated(unsafe) static var apsEnvironment = "development"
    nonisolated(unsafe) static var written: URL?

    override func url(forResource name: String?, withExtension ext: String?) -> URL? {
        let plist = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
              <key>Name</key><string>Drover Development</string>
              <key>Entitlements</key>
              <dict>
                <key>aps-environment</key><string>\(Self.apsEnvironment)</string>
              </dict>
            </dict>
            </plist>
            """
        var data = Data([0x30, 0x82, 0x0a, 0x01])  // CMS/DER preamble bytes
        data.append(Data(plist.utf8))
        data.append(Data([0x00, 0x01, 0x02]))  // trailing signature bytes

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("embedded-\(UUID().uuidString).mobileprovision")
        try? data.write(to: url)
        Self.written = url
        return url
    }
}

@Test func developmentProfileMeansSandbox() {
    BundleWithProfile.apsEnvironment = "development"
    defer { BundleWithProfile.written.map { try? FileManager.default.removeItem(at: $0) } }

    // A development profile issues sandbox tokens even from a Release build,
    // which is exactly the case `#if DEBUG` would get wrong.
    #expect(APNsEnvironment.current(bundle: BundleWithProfile()) == .sandbox)
}

@Test func productionProfileMeansProduction() {
    BundleWithProfile.apsEnvironment = "production"
    defer { BundleWithProfile.written.map { try? FileManager.default.removeItem(at: $0) } }

    #expect(APNsEnvironment.current(bundle: BundleWithProfile()) == .production)
}

@Test func entitlementIsParsedOutOfTheSignedEnvelope() {
    BundleWithProfile.apsEnvironment = "development"
    defer { BundleWithProfile.written.map { try? FileManager.default.removeItem(at: $0) } }

    // The plist has to be found between binary CMS bytes on both sides.
    #expect(
        APNsEnvironment.apsEnvironmentEntitlement(in: BundleWithProfile()) == "development"
    )
}
