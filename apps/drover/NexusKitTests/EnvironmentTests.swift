import Foundation
import Testing
@testable import NexusKit

// MARK: - validate (the server-checking logic behind AppEnvironment.configure)

private func validate() async -> String? {
    await ClientFactory.validate(config: ServerConfig(urlString: "http://test.local:7080")!,
                                 token: "test-token",
                                 session: MockURLProtocol.session())
}

/// `.serialized`: several tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment for why that
/// requires serialization rather than Swift Testing's default parallelism.
@Suite(.serialized)
struct EnvironmentTests {

@Test func factoryNilWhenUnconfigured() {
    let defaults = UserDefaults(suiteName: "drover-env-\(UUID().uuidString)")!
    let store = TokenStore(service: "drover-env-\(UUID().uuidString)")
    #expect(ClientFactory.make(defaults: defaults, tokenStore: store) == nil)
}

@Test func factoryBuildsWhenConfigured() throws {
    let defaults = UserDefaults(suiteName: "drover-env-\(UUID().uuidString)")!
    let store = TokenStore(service: "drover-env-\(UUID().uuidString)")
    ServerConfig(urlString: "http://h:7080")!.save(defaults: defaults)
    try store.save("tok")
    #expect(ClientFactory.make(defaults: defaults, tokenStore: store) != nil)
}

@Test func validateFailsWhenHealthzUnhealthy() async {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/healthz")
        return (500, Data())
    }
    let failure = await validate()
    #expect(failure == "Server did not respond to health check.")
}

@Test func validateReportsRejectedToken() async {
    MockURLProtocol.handler = { request in
        if request.url?.path == "/healthz" { return (200, Data()) }
        #expect(request.url?.path == "/harness")
        return (401, Data(#"{"error": "authentication required"}"#.utf8))
    }
    let failure = await validate()
    #expect(failure == "Token rejected by server.")
}

@Test func validateSucceedsWhenHealthzAndSnapshotGreen() async {
    MockURLProtocol.handler = { request in
        if request.url?.path == "/healthz" { return (200, Data()) }
        #expect(request.url?.path == "/harness")
        return (200, snapshotJSON)
    }
    let failure = await validate()
    #expect(failure == nil)
}

}
