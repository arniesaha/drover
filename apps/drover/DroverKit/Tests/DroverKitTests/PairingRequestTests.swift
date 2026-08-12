import Foundation
import Testing
@testable import DroverKit

// Nested in `MockNetworkTests` for the same reason every other handler-using
// suite is: these mutate the process-global `MockURLProtocol.handler`, and
// `.serialized` applies recursively so they stay mutually exclusive with the
// other network suites.
extension MockNetworkTests {
@Suite(.serialized)
struct PairingRequestTests {

    private func payload(
        _ scanned: String = "drover://127.0.0.1:7080?v=1&code=K7QP-2M4X&n=home-fleet"
    ) throws -> PairingPayload {
        try #require(PairingPayload(scanned: scanned))
    }

    @Test func pairPostsTheCodeAndDecodesTheToken() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path == "/auth/pair")
            #expect(request.httpMethod == "POST")
            // Pairing is the one unauthenticated call: the device has no
            // credential yet, which is the whole point.
            #expect(request.value(forHTTPHeaderField: "Authorization") == nil)

            let sent = (try? JSONSerialization.jsonObject(
                with: request.bodyStreamData()
            )) as? [String: Any]
            #expect(sent?["code"] as? String == "K7QP-2M4X")
            #expect(sent?["device_name"] as? String == "My Phone")

            return (201, Data(#"""
            {"token":"tok","credential_id":"cid","scope":"device",
             "server_id":"sid","fleet_name":"home-fleet"}
            """#.utf8))
        }

        let response = try await DroverClient.pair(
            payload: try payload(),
            deviceName: "My Phone",
            session: MockURLProtocol.session()
        )
        #expect(response.token == "tok")
        #expect(response.credentialID == "cid")
        #expect(response.scope == "device")
        #expect(response.serverID == "sid")
        #expect(response.fleetName == "home-fleet")
    }

    @Test func expiredCodeSurfacesTheServersOwnMessage() async throws {
        MockURLProtocol.handler = { _ in
            (410, Data(#"{"error":"unknown or expired code"}"#.utf8))
        }
        await #expect(throws: DroverError.httpStatus(410, "unknown or expired code")) {
            try await DroverClient.pair(
                payload: try payload(),
                deviceName: "Phone",
                session: MockURLProtocol.session()
            )
        }
    }

    @Test func throttledCodeSurfacesTheServersOwnMessage() async throws {
        MockURLProtocol.handler = { _ in
            (429, Data(#"{"error":"too many pairing attempts"}"#.utf8))
        }
        await #expect(throws: DroverError.httpStatus(429, "too many pairing attempts")) {
            try await DroverClient.pair(
                payload: try payload(),
                deviceName: "Phone",
                session: MockURLProtocol.session()
            )
        }
    }

    @Test func anOfflineHubIsATransportError() async throws {
        MockURLProtocol.transportError = URLError(.cannotConnectToHost)
        defer { MockURLProtocol.transportError = nil }

        await #expect(throws: DroverError.self) {
            try await DroverClient.pair(
                payload: try payload(),
                deviceName: "Phone",
                session: MockURLProtocol.session()
            )
        }
    }

    @Test func theTLSPayloadDialsHTTPS() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.scheme == "https")
            return (201, Data(#"""
            {"token":"t","credential_id":"c","scope":"device",
             "server_id":"s","fleet_name":"f"}
            """#.utf8))
        }
        _ = try await DroverClient.pair(
            payload: try payload("drover://example.test:443?v=1&code=K7QP-2M4X&tls=1"),
            deviceName: "Phone",
            session: MockURLProtocol.session()
        )
    }
}
}  // extension MockNetworkTests
