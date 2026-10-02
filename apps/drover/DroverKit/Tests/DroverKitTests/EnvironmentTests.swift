import Foundation
import Testing
@testable import DroverKit

extension MockNetworkTests {
@Suite(.serialized)
struct EnvironmentTests {
    let mock = MockNetwork()

    private func validate() async -> String? {
        await ClientFactory.validate(config: ServerConfig(urlString: "http://test.local:7080")!,
                                     token: "test-token",
                                     session: mock.session())
    }

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

@Test func factoryRefusesASavedEndpointThePolicyRejects() throws {
    let defaults = UserDefaults(suiteName: "drover-env-\(UUID().uuidString)")!
    let store = TokenStore(service: "drover-env-\(UUID().uuidString)")
    ServerConfig(urlString: "https://personal.example.test")!.save(defaults: defaults)
    try store.save("tok")

    #expect(ClientFactory.make(
        defaults: defaults,
        tokenStore: store,
        endpointIsAllowed: { $0.host == "stage.example.test" }
    ) == nil)
}

/// The gate has to live here, not in one caller: `BackgroundRefresh` builds
/// its own client from the same UserDefaults and Keychain, with no
/// `AppEnvironment` in the process to ask.
@Test func factoryBuildsWhenThePolicyAcceptsTheSavedEndpoint() throws {
    let defaults = UserDefaults(suiteName: "drover-env-\(UUID().uuidString)")!
    let store = TokenStore(service: "drover-env-\(UUID().uuidString)")
    ServerConfig(urlString: "https://stage.example.test")!.save(defaults: defaults)
    try store.save("tok")

    #expect(ClientFactory.make(
        defaults: defaults,
        tokenStore: store,
        endpointIsAllowed: { $0.host == "stage.example.test" }
    ) != nil)
}

@Test func validateFailsWhenHealthzUnhealthy() async {
    mock.handler = { request in
        #expect(request.url?.path == "/healthz")
        return (500, Data())
    }
    let failure = await validate()
    #expect(failure == "Server did not respond to health check.")
}

@Test func validateReportsRejectedToken() async {
    mock.handler = { request in
        if request.url?.path == "/healthz" { return (200, Data()) }
        #expect(request.url?.path == "/harness")
        return (401, Data(#"{"error": "authentication required"}"#.utf8))
    }
    let failure = await validate()
    #expect(failure == "Token rejected by server.")
}

@Test func validateSucceedsWhenHealthzAndSnapshotGreen() async {
    mock.handler = { request in
        if request.url?.path == "/healthz" { return (200, Data()) }
        #expect(request.url?.path == "/harness")
        return (200, snapshotJSON)
    }
    let failure = await validate()
    #expect(failure == nil)
}

}

}  // extension MockNetworkTests
