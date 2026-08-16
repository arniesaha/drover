import Foundation
import Testing
@testable import DroverKit

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


@Test func tailscaleAddressDetectionIPv4() {
    // Valid Tailscale IPv4 CGNAT range: 100.64.0.0/10 (100.64.0.0 - 100.127.255.255)
    #expect(ServerConfig.isTailscale(host: "100.64.0.1"))
    #expect(ServerConfig.isTailscale(host: "100.127.255.254"))
    #expect(ServerConfig.isTailscale(host: "100.64.0.0"))
    #expect(ServerConfig.isTailscale(host: "100.127.255.255"))
    #expect(ServerConfig.isTailscale(host: "100.80.50.1"))
    #expect(ServerConfig.isTailscale(host: "100.100.100.100"))

    // Out of range IPv4
    #expect(!ServerConfig.isTailscale(host: "100.128.0.1"))
    #expect(!ServerConfig.isTailscale(host: "100.63.255.255"))
    #expect(!ServerConfig.isTailscale(host: "100.0.0.1"))
    #expect(!ServerConfig.isTailscale(host: "192.168.1.1"))
    #expect(!ServerConfig.isTailscale(host: "10.0.0.1"))
    #expect(!ServerConfig.isTailscale(host: "172.16.0.1"))
    #expect(!ServerConfig.isTailscale(host: "127.0.0.1"))
    #expect(!ServerConfig.isTailscale(host: "256.64.0.1"))
    #expect(!ServerConfig.isTailscale(host: "100.64.0"))
    #expect(!ServerConfig.isTailscale(host: "100.64.0.1.1"))
}

@Test func tailscaleAddressDetectionHostnames() {
    #expect(ServerConfig.isTailscale(host: "ts.net"))
    #expect(ServerConfig.isTailscale(host: "my-hub.ts.net"))
    #expect(ServerConfig.isTailscale(host: "MY-HUB.TS.NET"))
    #expect(ServerConfig.isTailscale(host: "tailscale.net"))
    #expect(ServerConfig.isTailscale(host: "my-node.tailscale.net"))
    #expect(ServerConfig.isTailscale(host: "box.tailnet.internal"))
    #expect(ServerConfig.isTailscale(host: "my.tailnet"))

    #expect(!ServerConfig.isTailscale(host: "example.com"))
    #expect(!ServerConfig.isTailscale(host: "localhost"))
    #expect(!ServerConfig.isTailscale(host: "not-ts-net.com"))
    #expect(!ServerConfig.isTailscale(host: "tailscale-fake.com"))
}

@Test func tailscaleURLStringAndConfigProperties() {
    #expect(ServerConfig.isTailscale(urlString: "http://100.64.0.1:7080"))
    #expect(ServerConfig.isTailscale(urlString: "100.127.255.254:7080"))
    #expect(ServerConfig.isTailscale(urlString: "http://mac-mini.ts.net:7080"))
    #expect(ServerConfig.isTailscale(urlString: "https://mac-mini.tailscale.net"))
    #expect(ServerConfig.isTailscale(urlString: "http://box.tailnet.internal:8000"))

    #expect(!ServerConfig.isTailscale(urlString: "http://192.168.1.1:7080"))
    #expect(!ServerConfig.isTailscale(urlString: "http://100.128.0.1:7080"))
    #expect(!ServerConfig.isTailscale(urlString: "http://example.com"))
    #expect(!ServerConfig.isTailscale(urlString: ""))

    let tsConfig = ServerConfig(urlString: "http://100.64.0.1:7080")!
    #expect(tsConfig.isTailscaleAddress)
    #expect(tsConfig.tailscaleHost == "100.64.0.1")

    let domainConfig = ServerConfig(urlString: "http://mac-mini.ts.net:7080")!
    #expect(domainConfig.isTailscaleAddress)
    #expect(domainConfig.tailscaleHost == "mac-mini.ts.net")

    let nonTsConfig = ServerConfig(urlString: "http://192.168.1.149:7080")!
    #expect(!nonTsConfig.isTailscaleAddress)
    #expect(nonTsConfig.tailscaleHost == nil)
}
