import XCTest
import SwiftUI
import Testing
@testable import Drover
@testable import DroverKit

/// Manual entry is not a nicety: a camera can be denied, broken, or pointed at
/// a terminal whose QR will not scan, and being locked out of your own fleet
/// over that would be absurd. Both routes converge on one `PairingPayload`, so
/// these tests pin that convergence.
@MainActor
final class PairingViewTests: XCTestCase {
    func testCameraUsageDescriptionIsDeclared() throws {
        let description = Bundle.main.object(
            forInfoDictionaryKey: "NSCameraUsageDescription"
        ) as? String
        XCTAssertNotNil(description, "iOS kills the app on camera access without this")
        XCTAssertFalse(description?.isEmpty ?? true)
    }

    func testManualCodeEntryAcceptsAFormattedCode() {
        let model = PairingModel(serverURLString: "http://127.0.0.1:7080")
        model.manualCode = "k7qp-2m4x"
        XCTAssertTrue(model.canSubmitManualCode)
    }

    func testManualCodeEntryRejectsAnEmptyCode() {
        let model = PairingModel(serverURLString: "http://127.0.0.1:7080")
        model.manualCode = "   "
        XCTAssertFalse(model.canSubmitManualCode)
    }

    func testManualEntryNeedsAServerURL() {
        let model = PairingModel(serverURLString: "")
        model.manualCode = "K7QP-2M4X"
        XCTAssertFalse(model.canSubmitManualCode)
    }

    func testManualEntryBuildsTheSamePayloadAsAScan() throws {
        let model = PairingModel(serverURLString: "100.64.0.10:7080")
        model.manualCode = "K7QP-2M4X"
        let payload = try XCTUnwrap(model.manualPayload())
        XCTAssertEqual(payload.code, "K7QP-2M4X")
        XCTAssertEqual(payload.serverURL.absoluteString, "http://100.64.0.10:7080")
    }

    func testManualEntryHonoursAnHTTPSServerURL() throws {
        let model = PairingModel(serverURLString: "https://example.test:443")
        model.manualCode = "K7QP-2M4X"
        let payload = try XCTUnwrap(model.manualPayload())
        XCTAssertEqual(payload.serverURL.absoluteString, "https://example.test:443")
    }
}

// Both onboarding and Settings use AppEnvironment's pairing boundary. Keep
// these with the serialized network suites because the transport is shared.
extension MockNetworkTests {
@Suite(.serialized)
struct AppPairingRequestTests {
    @Test(arguments: [false, true])
    @MainActor
    func permittedPairingStillPostsCode(https: Bool) async throws {
        let suiteName = "drover.allowed-pairing.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        let store = TokenStore(service: suiteName)
        defer {
            try? store.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }
        let origin = https ? "https://hub.example.test" : "http://personal.example.test:7080"
        let model = PairingModel(serverURLString: origin)
        model.manualCode = "K7QP-2M4X"
        let requested = MockFlag()
        MockURLProtocol.handler = { request in
            requested.raise()
            #expect(request.url?.absoluteString == origin + "/auth/pair")
            #expect(request.httpMethod == "POST")
            let body = try? JSONSerialization.jsonObject(with: request.bodyStreamData()) as? [String: String]
            #expect(body?["code"] == "K7QP-2M4X")
            #expect(body?["device_name"] == "Synthetic Phone")
            return (201, Data(#"""
            {"token":"synthetic-token","credential_id":"synthetic-credential",
             "scope":"device","server_id":"hub","fleet_name":"Hub"}
            """#.utf8))
        }
        let environment = AppEnvironment(
            defaults: defaults, tokenStore: store,
            launchEnvironment: [:]
        )
        let response = try await environment.pair(
            payload: try #require(model.manualPayload()), deviceName: "Synthetic Phone",
            session: MockURLProtocol.session()
        )
        #expect(requested.isRaised)
        #expect(response.credentialID == "synthetic-credential")
        #expect(response.token == "synthetic-token")
    }
}
}
