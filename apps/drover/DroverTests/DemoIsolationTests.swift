import Foundation
import XCTest
@testable import Drover
@testable import DroverKit

@MainActor
final class DemoIsolationTests: XCTestCase {
    func testDemoSessionUsesOnlyTheStrictLocalTransport() async throws {
        let recorder = DemoOperationRecorder()
        let demo = try DemoSession(recorder: recorder)
        defer { demo.end() }

        let client = try XCTUnwrap(demo.environment.client)
        let snapshot = try await client.snapshot()
        XCTAssertEqual(snapshot.hosts.map(\.id), [DemoScenarioData.hostID])

        let launchedSessionID = try await client.createSession(
            hostID: DemoScenarioData.hostID,
            harness: "codex",
            mode: "structured",
            prompt: "Synthetic demo launch",
            cwd: "/demo/workspace"
        )
        XCTAssertEqual(launchedSessionID, DemoScenarioData.launchedSessionID)

        try await client.answerPermission(
            sessionID: DemoScenarioData.approvalSessionID,
            requestID: DemoScenarioData.approvalRequestID,
            decision: "allow",
            note: nil
        )

        let unknown = URL(
            string: "https://demo.drover.invalid/harness/unrecognised-route"
        )!
        let (_, unknownResponse) = try await demo.transport.session.data(from: unknown)
        XCTAssertEqual((unknownResponse as? HTTPURLResponse)?.statusCode, 404)

        let externalURL = URL(string: "https://example.com/live")!
        let (_, externalResponse) = try await demo.transport.session.data(from: externalURL)
        XCTAssertEqual((externalResponse as? HTTPURLResponse)?.statusCode, 400)

        _ = demo.transport.webSocketConnector.connect(
            client.streamRequest(sessionID: DemoScenarioData.chatSessionID)
        )

        let nonDemoRequest = URLRequest(url: externalURL)
        var invalidSocket = demo.transport.webSocketConnector
            .connect(nonDemoRequest)
            .makeAsyncIterator()
        do {
            _ = try await invalidSocket.next()
            XCTFail("a non-demo WebSocket request must fail locally")
        } catch {
            XCTAssertEqual((error as? URLError)?.code, .unsupportedURL)
        }

        let operations = recorder.snapshot
        XCTAssertGreaterThan(operations.localHTTPRequests, 0)
        XCTAssertGreaterThan(operations.localWebSocketConnections, 0)
        XCTAssertEqual(operations.liveHTTPRequests, 0)
        XCTAssertEqual(operations.liveWebSocketConnections, 0)
        XCTAssertEqual(operations.apnsRegistrationRequests, 0)
    }

    func testDemoGateBlocksBackgroundAndAPNsWorkThenResumesOnceExited() async throws {
        let gate = DemoActivityGate()
        let config = try XCTUnwrap(ServerConfig(urlString: "https://demo.drover.invalid"))
        let client = DroverClient(config: config, token: DemoScenarioData.syntheticToken)
        let counts = DemoGateCounts()

        gate.activate()
        BackgroundRefresh.schedule(gate: gate) { _ in counts.recordSchedule() }
        let completedWhileActive = await BackgroundRefresh.performWork(
            gate: gate,
            makeClient: {
                counts.recordClientConstruction()
                return client
            },
            check: { _ in counts.recordBackgroundCheck() }
        )

        let registrar = PushRegistrar(
            gate: gate,
            requestSystemToken: { counts.recordSystemTokenRequest() },
            uploadToken: { _, _ in counts.recordUpload() },
            setPushActive: { _ in }
        )
        registrar.setDemoSuspended(true)
        registrar.updateClient(client)
        registrar.requestTokenFromSystem()
        registrar.accept(token: Data([0x01, 0x02]))
        await Task.yield()

        XCTAssertEqual(counts.snapshot.scheduled, 0)
        XCTAssertFalse(completedWhileActive)
        XCTAssertEqual(counts.snapshot.constructedClients, 0)
        XCTAssertEqual(counts.snapshot.backgroundChecks, 0)
        XCTAssertEqual(counts.snapshot.systemTokenRequests, 0)
        XCTAssertEqual(counts.snapshot.uploads, 0)

        gate.deactivate()
        registrar.setDemoSuspended(false)
        for _ in 0..<20 where counts.snapshot.uploads == 0 { await Task.yield() }
        XCTAssertEqual(counts.snapshot.uploads, 1)
    }

    func testDemoEntryInvalidatesAPNsUploadQueuedBeforeTheTransition() throws {
        let gate = DemoActivityGate()
        let config = try XCTUnwrap(ServerConfig(urlString: "https://demo.drover.invalid"))
        let client = DroverClient(config: config, token: DemoScenarioData.syntheticToken)
        let counts = DemoGateCounts()
        let registrar = PushRegistrar(
            gate: gate,
            requestSystemToken: {},
            uploadToken: { _, _ in counts.recordUpload() },
            setPushActive: { _ in }
        )

        registrar.updateClient(client)
        registrar.accept(token: Data([0x03, 0x04]))
        // The Task inherits this main-actor turn and cannot run until this
        // test yields. Demo entry must invalidate it before the upload call.
        gate.activate()
        registrar.setDemoSuspended(true)

        let expectation = expectation(description: "queued upload gets a turn")
        DispatchQueue.main.async { expectation.fulfill() }
        wait(for: [expectation], timeout: 1)
        XCTAssertEqual(counts.snapshot.uploads, 0)
    }

    func testDemoEntryAndExitLeaveTheExistingBindingAndPendingDraftUntouched() async throws {
        let suiteName = "drover.demo-isolation.\(UUID().uuidString)"
        let service = "drover-demo-isolation-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        let tokenStore = TokenStore(service: service)
        let bindingStore = RecoveryBindingStore(service: service)
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("DroverDemoIsolationTests-\(UUID().uuidString)")
        let recovery = ChatRecoveryStore(root: root)
        defer {
            try? tokenStore.delete()
            try? bindingStore.clear()
            defaults.removePersistentDomain(forName: suiteName)
            try? FileManager.default.removeItem(at: root)
        }

        try tokenStore.save("existing-synthetic-token")
        let config = try XCTUnwrap(ServerConfig(urlString: "http://127.0.0.1:7080"))
        config.save(defaults: defaults)
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: tokenStore,
            recoveryBindingStore: bindingStore,
            recoveryStore: recovery,
            validator: { _, _ in nil },
            launchEnvironment: [:]
        )
        try XCTSkipUnless(environment.client != nil, "Keychain unavailable")
        let existingClient = try XCTUnwrap(environment.client)
        let existingBindingID = try XCTUnwrap(existingClient.credentialBindingID)
        let originalKey = ChatRecoveryKey(
            serverURL: existingClient.config.baseURL,
            credentialBindingID: existingBindingID,
            sessionID: "existing-session"
        )
        let chat = ChatModel(
            client: existingClient,
            sessionID: "existing-session",
            recoveryStore: recovery,
            recoveryWriteGate: environment.chatRecoveryWriteGate,
            recoveryGeneration: environment.chatRecoveryGeneration
        )
        chat.composerText = "A returning user's pending draft"

        let demo = try DemoSession()
        demo.end()

        await chat.prepareForDeparture()
        let restored = try await recovery.load(for: originalKey)
        XCTAssertEqual(restored?.draftText, "A returning user's pending draft")
        XCTAssertEqual(environment.client?.config.baseURL, existingClient.config.baseURL)
        XCTAssertEqual(environment.client?.credentialBindingID, existingBindingID)
        XCTAssertEqual(ServerConfig.load(defaults: defaults)?.baseURL, config.baseURL)
    }
}

private final class DemoGateCounts: @unchecked Sendable {
    struct Snapshot {
        var scheduled = 0
        var constructedClients = 0
        var backgroundChecks = 0
        var systemTokenRequests = 0
        var uploads = 0
    }

    private let lock = NSLock()
    private var value = Snapshot()

    var snapshot: Snapshot { lock.withLock { value } }

    func recordSchedule() { lock.withLock { value.scheduled += 1 } }
    func recordClientConstruction() { lock.withLock { value.constructedClients += 1 } }
    func recordBackgroundCheck() { lock.withLock { value.backgroundChecks += 1 } }
    func recordSystemTokenRequest() { lock.withLock { value.systemTokenRequests += 1 } }
    func recordUpload() { lock.withLock { value.uploads += 1 } }
}
