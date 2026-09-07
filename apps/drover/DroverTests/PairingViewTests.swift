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
struct StagePairingRequestTests {
    @Test(arguments: [false, true])
    @MainActor
    func offStagePairingMakesNoRequestOrCredential(manual: Bool) async throws {
        let suiteName = "drover.stage-pairing.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        let store = TokenStore(service: suiteName)
        defer {
            try? store.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }
        let requested = MockFlag()
        let validated = MockFlag()
        MockURLProtocol.handler = { _ in
            requested.raise()
            return (201, Data(#"""
            {"token":"unexpected-token","credential_id":"unexpected-credential",
             "scope":"device","server_id":"hub","fleet_name":"Personal"}
            """#.utf8))
        }
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: store,
            endpointPolicy: TestFlightEndpointPolicy(
                requiredOrigin: URL(string: "https://stage.example.test")!
            ),
            validator: { _, _ in validated.raise(); return nil },
            launchEnvironment: [:]
        )
        let model = PairingModel(serverURLString: "https://personal.example.test")
        model.manualCode = "K7QP-2M4X"
        let payload = try #require(manual ? model.manualPayload() : PairingPayload(
            scanned: "drover://personal.example.test?v=1&code=K7QP-2M4X&tls=1"
        ))

        do {
            _ = try await environment.pair(
                payload: payload, deviceName: "Synthetic Phone",
                session: MockURLProtocol.session()
            )
            Issue.record("off-stage pairing must be rejected before consuming a code")
        } catch {
            #expect(error.localizedDescription == "This TestFlight build connects only to its staging hub.")
        }
        #expect(!requested.isRaised)
        #expect(!validated.isRaised)
        #expect(store.load() == nil)
        #expect(ServerConfig.load(defaults: defaults) == nil)
        #expect(environment.client == nil)
    }

    @Test(arguments: [false, true])
    @MainActor
    func permittedPairingStillPostsCode(stageOnly: Bool) async throws {
        let suiteName = "drover.allowed-pairing.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        let store = TokenStore(service: suiteName)
        defer {
            try? store.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }
        let origin = stageOnly ? "https://stage.example.test" : "http://personal.example.test:7080"
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
             "scope":"device","server_id":"hub","fleet_name":"Stage"}
            """#.utf8))
        }
        let environment = AppEnvironment(
            defaults: defaults, tokenStore: store,
            endpointPolicy: TestFlightEndpointPolicy(
                requiredOrigin: stageOnly ? URL(string: origin)! : nil
            ),
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
