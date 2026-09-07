import XCTest
@testable import Drover

final class TestFlightEndpointPolicyTests: XCTestCase {
    func testStagePolicyAcceptsOnlyTheConfiguredHTTPSOrigin() throws {
        let policy = TestFlightEndpointPolicy(
            requiredOrigin: try XCTUnwrap(URL(string: "https://stage.example.test"))
        )

        XCTAssertTrue(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test"))))
        XCTAssertTrue(policy.accepts(try XCTUnwrap(URL(string: "https://STAGE.example.test:443"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "http://stage.example.test"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://other.example.test"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test:8443"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test/"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test/api"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://token@stage.example.test"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test?mode=test"))))
        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test#section"))))
    }

    func testStagePolicyRejectsARequiredOriginWithTrailingRootSlash() throws {
        let policy = TestFlightEndpointPolicy(
            requiredOrigin: try XCTUnwrap(URL(string: "https://stage.example.test/"))
        )

        XCTAssertFalse(policy.accepts(try XCTUnwrap(URL(string: "https://stage.example.test"))))
    }

    func testNilRequiredOriginAllowsOrdinarySelfHostedServers() throws {
        let policy = TestFlightEndpointPolicy(requiredOrigin: nil)

        XCTAssertTrue(policy.accepts(try XCTUnwrap(URL(string: "http://127.0.0.1:7080"))))
    }
}
