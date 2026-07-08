import Foundation
import Testing
@testable import NexusKit

@Test func tokenRoundTripAndDelete() throws {
    let store = TokenStore(service: "com.arnab.drover.test-\(UUID().uuidString)")
    #expect(store.load() == nil)
    try store.save("secret-token-1")
    #expect(store.load() == "secret-token-1")
    try store.save("secret-token-2")           // upsert
    #expect(store.load() == "secret-token-2")
    try store.delete()
    #expect(store.load() == nil)
}

@Test func serverConfigParsing() {
    #expect(ServerConfig(urlString: "  192.168.1.149:7080 ")?.baseURL.absoluteString
            == "http://192.168.1.149:7080")
    #expect(ServerConfig(urlString: "http://100.99.1.2:7080")?.baseURL.scheme == "http")
    #expect(ServerConfig(urlString: "") == nil)
    #expect(ServerConfig(urlString: "   ") == nil)
}

@Test func serverConfigPersistence() {
    let defaults = UserDefaults(suiteName: "drover-test-\(UUID().uuidString)")!
    let config = ServerConfig(urlString: "http://example.local:7080")!
    config.save(defaults: defaults)
    #expect(ServerConfig.load(defaults: defaults) == config)
}
