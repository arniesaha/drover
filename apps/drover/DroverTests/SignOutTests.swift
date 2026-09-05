import XCTest
import Security
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
        launchEnvironment: [String: String] = ProcessInfo.processInfo.environment,
        _ body: @MainActor (AppEnvironment, TokenStore, UserDefaults, ChatRecoveryStore, URL) async throws -> Void
    ) async throws {
        let suiteName = "drover.signout.\(UUID().uuidString)"
        let service = "drover-signout-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let store = TokenStore(service: service)
        let bindingStore = RecoveryBindingStore(service: service)
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("DroverSignOutTests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let recovery = ChatRecoveryStore(root: root)
        defer {
            try? store.delete()
            try? bindingStore.clear()
            defaults.removePersistentDomain(forName: suiteName)
            try? FileManager.default.removeItem(at: root)
        }

        if configured {
            try? store.save("existing-token")
            ServerConfig(urlString: "http://127.0.0.1:7080")!.save(defaults: defaults)
        }
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: store,
            recoveryBindingStore: bindingStore,
            recoveryStore: recovery,
            launchEnvironment: launchEnvironment
        )
        try await body(environment, store, defaults, recovery, root)
    }

    func testSignOutClearsTheToken() async throws {
        try await withEnvironment { environment, store, _, _, _ in
            try XCTSkipUnless(
                environment.hasTokenConfigured,
                "Keychain unavailable in this test host"
            )

            try await environment.signOut()

            XCTAssertNil(store.load())
            XCTAssertFalse(environment.hasTokenConfigured)
        }
    }

    func testSignOutDropsTheClientSoTheAppReturnsToOnboarding() async throws {
        try await withEnvironment { environment, _, _, _, _ in
            try XCTSkipUnless(environment.client != nil, "Keychain unavailable")

            try await environment.signOut()

            XCTAssertNil(
                environment.client, "a live client would keep the inbox showing"
            )
            XCTAssertNil(environment.config)
        }
    }

    func testSignOutForgetsTheServerURL() async throws {
        try await withEnvironment { environment, _, defaults, _, _ in
            XCTAssertNotNil(ServerConfig.load(defaults: defaults))

            try await environment.signOut()

            XCTAssertNil(ServerConfig.load(defaults: defaults))
        }
    }

    func testSignOutBumpsGenerationSoViewsRebuild() async throws {
        try await withEnvironment { environment, _, _, _, _ in
            let before = environment.generation
            try await environment.signOut()
            XCTAssertGreaterThan(environment.generation, before)
        }
    }

    func testSignOutIsIdempotent() async throws {
        try await withEnvironment { environment, _, defaults, _, _ in
            try await environment.signOut()
            try await environment.signOut()

            XCTAssertNil(environment.client)
            XCTAssertNil(ServerConfig.load(defaults: defaults))
            XCTAssertFalse(environment.hasTokenConfigured)
        }
    }

    func testSignOutOnAnUnconfiguredAppIsHarmless() async throws {
        try await withEnvironment(configured: false) { environment, _, _, _, _ in
            try await environment.signOut()
            XCTAssertNil(environment.client)
        }
    }

    func testUITestResetLaunchClearsPersistedAuthenticationBeforeLoading() async throws {
        try await withEnvironment(
            launchEnvironment: ["DROVER_UI_TEST_RESET_AUTH": "1"]
        ) { environment, store, defaults, _, _ in
            XCTAssertNil(store.load(), "the UI-test credential must leave Keychain at launch")
            XCTAssertNil(ServerConfig.load(defaults: defaults))
            XCTAssertNil(environment.client)
            XCTAssertNil(environment.config)
        }
    }

    func testSignOutPurgesOnlyTheOldCredentialBinding() async throws {
        try await withEnvironment { environment, _, _, recovery, root in
            let bindingID = try XCTUnwrap(environment.client?.credentialBindingID)
            let key = ChatRecoveryKey(
                serverURL: URL(string: "http://127.0.0.1:7080")!,
                credentialBindingID: bindingID,
                sessionID: "synthetic-session"
            )
            try await recovery.save(ChatRecoverySnapshot(draftText: "synthetic draft"), for: key)
            let crashTemporary = root.appendingPathComponent(".synthetic-crash.tmp")
            try Data("synthetic temporary payload".utf8).write(to: crashTemporary)

            try await environment.signOut()

            let restored = try await recovery.load(for: key)
            XCTAssertNil(restored)
            XCTAssertFalse(FileManager.default.fileExists(atPath: crashTemporary.path))
        }
    }

    func testRecoveryFilesAreNoBackupAndCompleteProtection() async throws {
        try await withEnvironment { environment, _, _, recovery, root in
            let bindingID = try XCTUnwrap(environment.client?.credentialBindingID)
            let key = ChatRecoveryKey(
                serverURL: URL(string: "http://127.0.0.1:7080")!,
                credentialBindingID: bindingID,
                sessionID: "synthetic-session"
            )
            try await recovery.save(ChatRecoverySnapshot(draftText: "synthetic draft"), for: key)
            let children = try FileManager.default.contentsOfDirectory(
                at: root,
                includingPropertiesForKeys: nil
            )
            let protectedURLs = [root] + children
            for url in protectedURLs {
                let resources = try url.resourceValues(forKeys: [.isExcludedFromBackupKey])
                XCTAssertEqual(resources.isExcludedFromBackup, true, url.lastPathComponent)
#if !targetEnvironment(simulator)
                let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
                XCTAssertEqual(attributes[.protectionKey] as? FileProtectionType, .complete, url.lastPathComponent)
#endif
            }
        }
    }

    func testSignOutReportsPendingCleanupAfterPurgeFailure() async throws {
        let suiteName = "drover.signout.failure.\(UUID().uuidString)"
        let service = "drover-signout-failure-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let tokenStore = TokenStore(service: service)
        let bindingStore = RecoveryBindingStore(service: service)
        let recoveryStore = FailingPurgeRecoveryStore()
        defer {
            try? tokenStore.delete()
            try? bindingStore.clear()
            defaults.removePersistentDomain(forName: suiteName)
        }
        try tokenStore.save("existing-token")
        ServerConfig(urlString: "http://127.0.0.1:7080")!.save(defaults: defaults)
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: tokenStore,
            recoveryBindingStore: bindingStore,
            recoveryStore: recoveryStore,
            launchEnvironment: [:]
        )
        try XCTSkipUnless(environment.client?.credentialBindingID != nil, "Keychain unavailable")

        do {
            try await environment.signOut()
            XCTFail("sign out must report failed local cleanup")
        } catch {
            XCTAssertNil(environment.client)
            XCTAssertFalse(environment.hasTokenConfigured)
            XCTAssertTrue(environment.hasPendingLocalCleanup)
            XCTAssertEqual(
                environment.recoveryStatusMessage,
                "Disconnected, but local chat recovery cleanup is still pending. Try Sign Out again."
            )
        }
    }

    func testSignOutInvalidatesDelayedConfigurationBeforeItCanCommit() async throws {
        let suiteName = "drover.signout.race.\(UUID().uuidString)"
        let service = "drover-signout-race-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let tokenStore = TokenStore(service: service)
        let bindingStore = RecoveryBindingStore(service: service)
        let delayedValidator = DelayedValidator()
        let root = try temporaryRecoveryRoot()
        defer {
            try? tokenStore.delete()
            try? bindingStore.clear()
            defaults.removePersistentDomain(forName: suiteName)
            try? FileManager.default.removeItem(at: root)
        }
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: tokenStore,
            recoveryBindingStore: bindingStore,
            recoveryStore: ChatRecoveryStore(root: root),
            validator: { config, token in
                await delayedValidator.validate(config: config, token: token)
            },
            launchEnvironment: [:]
        )
        let raceCredential = "synthetic-race-value"
        let configure = Task { @MainActor in
            await environment.configure(
                urlString: "http://127.0.0.1:7080",
                token: raceCredential
            )
        }
        await delayedValidator.waitUntilStarted()

        try await environment.signOut()
        await delayedValidator.release()
        _ = await configure.value

        XCTAssertNil(tokenStore.load())
        XCTAssertNil(ServerConfig.load(defaults: defaults))
        XCTAssertNil(environment.client)
        XCTAssertNil(environment.config)
        XCTAssertFalse(try hasRecoveryBindingMetadata(service: service))
    }

    func testSignOutRetriesOldBindingAfterReplacementPurgeFailure() async throws {
        let suiteName = "drover.signout.replacement.\(UUID().uuidString)"
        let service = "drover-signout-replacement-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let tokenStore = TokenStore(service: service)
        let bindingStore = RecoveryBindingStore(service: service)
        let root = try temporaryRecoveryRoot()
        let durableStore = ChatRecoveryStore(root: root)
        let recoveryStore = FailOncePurgeRecoveryStore(store: durableStore)
        defer {
            try? tokenStore.delete()
            try? bindingStore.clear()
            defaults.removePersistentDomain(forName: suiteName)
            try? FileManager.default.removeItem(at: root)
        }
        try tokenStore.save("synthetic-original-token")
        let config = ServerConfig(urlString: "http://127.0.0.1:7080")!
        config.save(defaults: defaults)
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: tokenStore,
            recoveryBindingStore: bindingStore,
            recoveryStore: recoveryStore,
            validator: { _, _ in nil },
            launchEnvironment: [:]
        )
        let oldBinding = try XCTUnwrap(environment.client?.credentialBindingID)
        let oldKey = ChatRecoveryKey(
            serverURL: config.baseURL,
            credentialBindingID: oldBinding,
            sessionID: "synthetic-session"
        )
        try await durableStore.save(.init(draftText: "synthetic old draft"), for: oldKey)

        let replacementCredential = "synthetic-replacement-value"
        let outcome = await environment.configure(
            urlString: config.baseURL.absoluteString,
            token: replacementCredential
        )
        guard case .success = outcome else {
            return XCTFail("replacement configuration should validate")
        }
        XCTAssertTrue(environment.hasPendingLocalCleanup)

        try await environment.signOut()

        let restored = try await durableStore.load(for: oldKey)
        XCTAssertNil(restored)
    }
}

private func temporaryRecoveryRoot() throws -> URL {
    let root = try FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: true
    ).appendingPathComponent("DroverSignOutTests-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return root
}

private func hasRecoveryBindingMetadata(service: String) throws -> Bool {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: "chat-recovery-binding-v1",
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    let status = SecItemCopyMatching(query as CFDictionary, nil)
    if status == errSecItemNotFound {
        return false
    }
    guard status == errSecSuccess else { throw KeychainError.osStatus(status) }
    return true
}

private actor DelayedValidator {
    private var hasStarted = false
    private var startWaiters: [CheckedContinuation<Void, Never>] = []
    private var releaseContinuation: CheckedContinuation<Void, Never>?

    func validate(config: ServerConfig, token: String) async -> String? {
        hasStarted = true
        let waiters = startWaiters
        startWaiters.removeAll()
        for waiter in waiters {
            waiter.resume()
        }
        await withCheckedContinuation { continuation in
            releaseContinuation = continuation
        }
        return nil
    }

    func waitUntilStarted() async {
        guard !hasStarted else { return }
        await withCheckedContinuation { continuation in
            startWaiters.append(continuation)
        }
    }

    func release() {
        releaseContinuation?.resume()
        releaseContinuation = nil
    }
}

private actor FailingPurgeRecoveryStore: ChatRecoveryPersisting {
    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? { nil }
    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {}
    func remove(for key: ChatRecoveryKey) async throws {}
    func purge(bindingID: UUID) async throws { throw ChatRecoveryError.storageUnavailable }
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
}

private actor FailOncePurgeRecoveryStore: ChatRecoveryPersisting {
    private let store: ChatRecoveryStore
    private var shouldFailPurge = true

    init(store: ChatRecoveryStore) {
        self.store = store
    }

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        try await store.load(for: key)
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        try await store.save(snapshot, for: key)
    }

    func remove(for key: ChatRecoveryKey) async throws {
        try await store.remove(for: key)
    }

    func purge(bindingID: UUID) async throws {
        if shouldFailPurge {
            shouldFailPurge = false
            throw ChatRecoveryError.storageUnavailable
        }
        try await store.purge(bindingID: bindingID)
    }

    func sweep(keeping bindingIDs: Set<UUID>) async throws {
        try await store.sweep(keeping: bindingIDs)
    }
}
