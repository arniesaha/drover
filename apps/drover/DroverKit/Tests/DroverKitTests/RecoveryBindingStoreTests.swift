import Foundation
import Testing
@testable import DroverKit

@Suite(.serialized)
struct RecoveryBindingStoreTests {
    @Test func matchingCredentialKeepsItsBinding() throws {
        let store = RecoveryBindingStore(service: "drover-recovery-binding-\(UUID().uuidString)")
        defer { try? store.clear() }

        let first = try store.binding(
            forToken: "synthetic-token-one",
            serverURL: URL(string: "http://private.example:7080/")!,
            rotate: false
        )
        let second = try store.binding(
            forToken: "synthetic-token-one",
            serverURL: URL(string: "http://PRIVATE.example:7080")!,
            rotate: false
        )

        #expect(second == first)
    }

    @Test func rotatingOrReplacingCredentialMakesOldRecoveryUnreachable() throws {
        let store = RecoveryBindingStore(service: "drover-recovery-binding-\(UUID().uuidString)")
        defer { try? store.clear() }
        let url = URL(string: "http://private.example:7080")!

        let original = try store.binding(forToken: "synthetic-token-one", serverURL: url, rotate: false)
        let rotated = try store.binding(forToken: "synthetic-token-one", serverURL: url, rotate: true)
        let replacement = try store.binding(forToken: "synthetic-token-two", serverURL: url, rotate: false)

        #expect(rotated != original)
        #expect(replacement != rotated)
    }

    @Test func clearingMetadataStartsANewNamespaceWithoutTouchingTheTokenItem() throws {
        let service = "drover-recovery-binding-\(UUID().uuidString)"
        let bindingStore = RecoveryBindingStore(service: service)
        let tokenStore = TokenStore(service: service)
        defer {
            try? bindingStore.clear()
            try? tokenStore.delete()
        }
        let url = URL(string: "http://private.example:7080")!
        try tokenStore.save("synthetic-token")
        let before = try bindingStore.binding(forToken: "synthetic-token", serverURL: url, rotate: false)

        try bindingStore.clear()
        let after = try bindingStore.binding(forToken: "synthetic-token", serverURL: url, rotate: false)

        #expect(tokenStore.load() == "synthetic-token")
        #expect(after != before)
    }

    @Test func clientFactoryOnlyUsesAnExplicitForegroundBinding() throws {
        let suite = "drover-recovery-factory-\(UUID().uuidString)"
        let service = "drover-recovery-factory-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        let tokenStore = TokenStore(service: service)
        defer {
            try? tokenStore.delete()
            defaults.removePersistentDomain(forName: suite)
        }
        ServerConfig(urlString: "http://private.example:7080")!.save(defaults: defaults)
        try tokenStore.save("synthetic-token")
        let binding = UUID(uuidString: "00000000-0000-0000-0000-000000000100")!

        let foreground = try #require(ClientFactory.make(
            defaults: defaults,
            tokenStore: tokenStore,
            credentialBindingID: binding
        ))
        let background = try #require(ClientFactory.make(defaults: defaults, tokenStore: tokenStore))

        #expect(foreground.client.credentialBindingID == binding)
        #expect(background.client.credentialBindingID == nil)
    }
}
