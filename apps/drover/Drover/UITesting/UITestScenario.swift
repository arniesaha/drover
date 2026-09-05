import Foundation
import DroverKit

#if DEBUG
/// Isolated building blocks for the one allowed environment-selected scenario.
struct UITestScenarioTransport {
    let runID: String
    let client: DroverClient
    let receiptState: FixtureReceiptState

    init?(environment: [String: String] = ProcessInfo.processInfo.environment) {
        guard environment["DROVER_UI_TEST_SCENARIO"] == "core-journey" else { return nil }
        guard let rawRunID = environment["DROVER_UI_TEST_RUN_ID"],
              let runUUID = UUID(uuidString: rawRunID),
              let config = ServerConfig(urlString: FixtureScenarioData.coreJourney.serverURLString)
        else {
            preconditionFailure("core-journey requires a UUID isolation identifier")
        }
        let runID = runUUID.uuidString
        self.runID = runID
        let receiptState = FixtureReceiptState(runID: runID)
        FixtureHubURLProtocol.install(receiptState: receiptState)
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [FixtureHubURLProtocol.self]
        self.client = DroverClient(
            config: config,
            token: FixtureScenarioData.syntheticBearerToken,
            credentialBindingID: FixtureScenarioData.coreJourney.credentialBindingID,
            session: URLSession(configuration: sessionConfiguration)
        )
        self.receiptState = receiptState
    }

    /// The final scenario uses this root for its foreground-only recovery
    /// actor. The root is distinct from the production Application Support
    /// namespace and stable only for this test run's terminate/relaunch.
    func recoveryRoot(fileManager: FileManager = .default) throws -> URL {
        let support = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        return support
            .appendingPathComponent("DroverUITesting", isDirectory: true)
            .appendingPathComponent(runID, isDirectory: true)
            .appendingPathComponent("ChatRecovery", isDirectory: true)
    }
}

@MainActor
struct UITestScenario {
    let transport: UITestScenarioTransport
    let defaults: UserDefaults
    let environment: AppEnvironment
    private let recoveryStore: ChatRecoveryStore
    private let catalogStore: HarnessModelCatalogStore

    init?(launchEnvironment: [String: String] = ProcessInfo.processInfo.environment) {
        guard let transport = UITestScenarioTransport(environment: launchEnvironment) else { return nil }
        self.transport = transport
        guard let defaults = UserDefaults(suiteName: "com.arnab.drover.ui-fixture.\(transport.runID)"),
              let root = try? transport.recoveryRoot() else {
            preconditionFailure("Could not create isolated synthetic stores")
        }
        self.defaults = defaults
        self.catalogStore = HarnessModelCatalogStore(defaults: defaults)
        self.recoveryStore = ChatRecoveryStore(root: root)
        self.environment = AppEnvironment(
            fixtureClient: launchEnvironment["DROVER_UI_TEST_START_UNPAIRED"] == "1" ? nil : transport.client,
            defaults: defaults,
            tokenStore: TokenStore(service: "com.arnab.drover.ui-fixture.\(transport.runID)"),
            recoveryStore: recoveryStore
        )
    }

    func makeClient() -> (client: DroverClient, chatModelFactory: ChatModelFactory) {
        (transport.client, { client, sessionID, harness in
            ChatModel(
                client: client, sessionID: sessionID, harness: harness,
                store: catalogStore,
                deliveryConfirmationTimeout: .milliseconds(100),
                recoveryStore: recoveryStore,
                recoveryWriteGate: environment.chatRecoveryWriteGate,
                recoveryGeneration: environment.chatRecoveryGeneration,
                streamFactory: { client, sessionID in
                    MessageStream(client: client, sessionID: sessionID,
                                  connector: FixtureWebSocketConnector())
                }
            )
        })
    }

    /// A real durable record in another session proves namespace separation.
    /// It is seeded once and never rewrites a record the journey has changed.
    func prepare() async throws {
        // Keep only this run's namespace. Relaunching the same UUID preserves
        // recovery; a new test removes only older synthetic UUID directories
        // and their explicitly named suites, never production app state.
        let runRoot = try transport.recoveryRoot().deletingLastPathComponent()
        let fixtureRoot = runRoot.deletingLastPathComponent()
        let fileManager = FileManager.default
        if fileManager.fileExists(atPath: fixtureRoot.path) {
            for child in try fileManager.contentsOfDirectory(
                at: fixtureRoot, includingPropertiesForKeys: nil
            ) where UUID(uuidString: child.lastPathComponent) != nil
                && child.lastPathComponent != transport.runID {
                try fileManager.removeItem(at: child)
                defaults.removePersistentDomain(
                    forName: "com.arnab.drover.ui-fixture.\(child.lastPathComponent)"
                )
            }
        }
        let otherKey = ChatRecoveryKey(
            serverURL: transport.client.config.baseURL,
            credentialBindingID: FixtureScenarioData.coreJourney.credentialBindingID,
            sessionID: FixtureScenarioData.otherSessionID
        )
        if try await recoveryStore.load(for: otherKey) == nil {
            try await recoveryStore.save(ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: UUID(uuidString: "00000000-0000-4000-8000-000000000043")!,
                    text: "Synthetic other-session pending delivery"
                )
            ), for: otherKey)
        }
    }
}

struct FixtureNotifier: Notifying {
    func notify(title: String, body: String, id: String) async {}
    func setBadge(_ count: Int) async {}
}
#endif
