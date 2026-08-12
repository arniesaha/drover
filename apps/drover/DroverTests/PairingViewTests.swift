import XCTest
import SwiftUI
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
