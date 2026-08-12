import XCTest
@testable import DroverKit

final class PairingPayloadTests: XCTestCase {
    func testParsesAPlainPayload() throws {
        let payload = try XCTUnwrap(
            PairingPayload(scanned: "drover://100.64.0.10:7080?v=1&code=K7QP-2M4X&n=home-fleet")
        )
        XCTAssertEqual(payload.serverURL.absoluteString, "http://100.64.0.10:7080")
        XCTAssertEqual(payload.code, "K7QP-2M4X")
        XCTAssertEqual(payload.fleetName, "home-fleet")
    }

    func testTLSFlagSelectsHTTPS() throws {
        let payload = try XCTUnwrap(
            PairingPayload(scanned: "drover://example.test:443?v=1&code=K7QP-2M4X&tls=1")
        )
        XCTAssertEqual(payload.serverURL.absoluteString, "https://example.test:443")
    }

    func testFleetNameIsOptional() throws {
        let payload = try XCTUnwrap(
            PairingPayload(scanned: "drover://100.64.0.10:7080?v=1&code=K7QP-2M4X")
        )
        XCTAssertNil(payload.fleetName)
    }

    func testPercentEscapedFleetNameIsDecoded() throws {
        let payload = try XCTUnwrap(
            PairingPayload(scanned: "drover://100.64.0.10:7080?v=1&code=K7QP-2M4X&n=my%20fleet")
        )
        XCTAssertEqual(payload.fleetName, "my fleet")
    }

    func testRejectsForeignScheme() {
        XCTAssertNil(PairingPayload(scanned: "https://example.test?code=K7QP-2M4X"))
        XCTAssertNil(PairingPayload(scanned: "mobilecli://host:1?code=K7QP-2M4X"))
    }

    func testRejectsMissingCode() {
        XCTAssertNil(PairingPayload(scanned: "drover://100.64.0.10:7080?v=1"))
        XCTAssertNil(PairingPayload(scanned: "drover://100.64.0.10:7080?v=1&code="))
    }

    func testRejectsUnsupportedVersion() {
        XCTAssertNil(PairingPayload(scanned: "drover://100.64.0.10:7080?v=9&code=K7QP-2M4X"))
    }

    func testRejectsGarbage() {
        XCTAssertNil(PairingPayload(scanned: ""))
        XCTAssertNil(PairingPayload(scanned: "not a url at all"))
    }
}
