import XCTest
@testable import Drover
@testable import DroverKit

/// Signing out has to leave the app in the state a fresh install is in, or the
/// next pairing inherits half the old configuration. These pin that: no token,
/// no client, no saved URL.
///
/// No `setUp`/`tearDown` overrides and no stored properties, deliberately.
/// Those overrides are nonisolated while this class is `@MainActor`, so under
/// the Swift on the CI runner (older than the local toolchain, which does not
/// reproduce it) touching isolated state from them is a compile error, and the
/// `async` variants then trip `sending value of non-Sendable type 'XCTestCase'`
/// on the `super` call. Per-test local state sidesteps both, and is clearer
/// anyway: each test owns its own Keychain service and defaults suite.
@MainActor
final class SignOutTests: XCTestCase {

    /// Builds an isolated environment, runs the body, and always cleans up.
    private func withEnvironment(
        configured: Bool = true,
        _ body: (AppEnvironment, TokenStore, UserDefaults) throws -> Void
    ) rethrows {
        let suiteName = "drover.signout.\(UUID().uuidString)"
        let service = "drover-signout-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let store = TokenStore(service: service)
        defer {
            try? store.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }

        if configured {
            try? store.save("existing-token")
            ServerConfig(urlString: "http://127.0.0.1:7080")!.save(defaults: defaults)
        }
        let environment = AppEnvironment(defaults: defaults, tokenStore: store)
        try body(environment, store, defaults)
    }

    func testSignOutClearsTheToken() throws {
        try withEnvironment { environment, store, _ in
            try XCTSkipUnless(
                environment.hasTokenConfigured,
                "Keychain unavailable in this test host"
            )

            environment.signOut()

            XCTAssertNil(store.load())
            XCTAssertFalse(environment.hasTokenConfigured)
        }
    }

    func testSignOutDropsTheClientSoTheAppReturnsToOnboarding() throws {
        try withEnvironment { environment, _, _ in
            try XCTSkipUnless(environment.client != nil, "Keychain unavailable")

            environment.signOut()

            XCTAssertNil(
                environment.client, "a live client would keep the inbox showing"
            )
            XCTAssertNil(environment.config)
        }
    }

    func testSignOutForgetsTheServerURL() {
        withEnvironment { environment, _, defaults in
            XCTAssertNotNil(ServerConfig.load(defaults: defaults))

            environment.signOut()

            XCTAssertNil(ServerConfig.load(defaults: defaults))
        }
    }

    func testSignOutBumpsGenerationSoViewsRebuild() {
        withEnvironment { environment, _, _ in
            let before = environment.generation
            environment.signOut()
            XCTAssertGreaterThan(environment.generation, before)
        }
    }

    func testSignOutIsIdempotent() {
        withEnvironment { environment, _, defaults in
            environment.signOut()
            environment.signOut()

            XCTAssertNil(environment.client)
            XCTAssertNil(ServerConfig.load(defaults: defaults))
            XCTAssertFalse(environment.hasTokenConfigured)
        }
    }

    func testSignOutOnAnUnconfiguredAppIsHarmless() {
        withEnvironment(configured: false) { environment, _, _ in
            environment.signOut()
            XCTAssertNil(environment.client)
        }
    }
}
