import Foundation
import Testing
@testable import Drover
@testable import DroverKit

// Synthetic credential used only with the intercepted URLSession.
private let pushRegistrarFixtureCredential = "synthetic-device-credential"

private actor PushRegistrarSpyNotifier: Notifying {
    private(set) var notifiedIDs: [String] = []

    func notify(title: String, body: String, id: String) async {
        notifiedIDs.append(id)
    }

    func setBadge(_ count: Int) async {}
}

private func pushRegistrarSnapshotData(sessionID: String) -> Data {
    Data("""
    {"hosts": [], "sessions": [
      {"session_id": "\(sessionID)", "host_id": "host-1", "harness": "claude-code",
       "mode": "structured", "status": "running", "awaiting": "input",
       "cwd": "/p/drover", "last_activity": null}
    ], "cwd_suggestions": []}
    """.utf8)
}

private final class MockCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func increment() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}

@MainActor
private func waitForPushRegistrarCondition(
    timeoutNanoseconds: UInt64 = 1_000_000_000,
    _ condition: @escaping () -> Bool
) async -> Bool {
    let step: UInt64 = 10_000_000
    var waited: UInt64 = 0
    while waited < timeoutNanoseconds {
        if condition() { return true }
        try? await Task.sleep(nanoseconds: step)
        waited += step
    }
    return condition()
}

extension MockNetworkTests {
@Suite(.serialized)
struct PushRegistrarTests {

@Test @MainActor func failedTokenUploadClearsStaleHubPushStateSoLocalAlertsResume() async throws {
    let defaults = UserDefaults.standard
    let sessionID = "push-fallback-\(UUID().uuidString)"
    defaults.removeObject(forKey: AttentionWatcher.seenKey)
    defaults.removeObject(forKey: AttentionWatcher.readKey)
    let registrar = PushRegistrar()
    PushRegistration.setActive(true, in: defaults)
    defer {
        PushRegistration.setActive(false, in: defaults)
        defaults.removeObject(forKey: AttentionWatcher.seenKey)
        defaults.removeObject(forKey: AttentionWatcher.readKey)
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
    }

    let uploadAttempted = MockFlag()
    MockURLProtocol.handler = { request in
        if request.httpMethod == "PUT", request.url?.path == "/auth/device/apns" {
            uploadAttempted.raise()
            return (503, Data(#"{"error":"hub push is unavailable"}"#.utf8))
        }
        return (200, pushRegistrarSnapshotData(sessionID: sessionID))
    }
    let client = DroverClient(
        config: ServerConfig(urlString: "http://drover.test")!,
        token: pushRegistrarFixtureCredential,
        session: MockURLProtocol.session()
    )

    registrar.updateClient(client)
    #expect(!PushRegistration.isActive(in: defaults))
    // Simulate stale persisted success as the upload is about to start too,
    // so this checks the failure handler in addition to updateClient.
    PushRegistration.setActive(true, in: defaults)
    registrar.accept(token: Data([0x09, 0x28, 0x01]))

    let recovered = await waitForPushRegistrarCondition {
        uploadAttempted.isRaised && !PushRegistration.isActive(in: defaults)
    }
    #expect(recovered)

    let spy = PushRegistrarSpyNotifier()
    let snapshot = try HarnessSnapshot.decode(
        from: pushRegistrarSnapshotData(sessionID: sessionID)
    )
    await AttentionWatcher(notifier: spy, seenStore: defaults).evaluate(snapshot)

    #expect(await spy.notifiedIDs == [sessionID])
}

@Test @MainActor func hubChangeClearsStalePushStateUntilUploadSucceeds() async throws {
    let defaults = UserDefaults.standard
    let registrar = PushRegistrar()
    PushRegistration.setActive(true, in: defaults)
    defer {
        PushRegistration.setActive(false, in: defaults)
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
    }

    let uploadAttempted = MockFlag()
    MockURLProtocol.handler = { request in
        if request.httpMethod == "PUT", request.url?.path == "/auth/device/apns" {
            uploadAttempted.raise()
            return (204, Data())
        }
        return (404, Data())
    }
    let client = DroverClient(
        config: ServerConfig(urlString: "http://new-hub.test")!,
        token: pushRegistrarFixtureCredential,
        session: MockURLProtocol.session()
    )

    registrar.updateClient(client)
    #expect(!PushRegistration.isActive(in: defaults))

    registrar.accept(token: Data([0x09, 0x28, 0x02]))

    let registered = await waitForPushRegistrarCondition {
        uploadAttempted.isRaised && PushRegistration.isActive(in: defaults)
    }
    #expect(registered)
}

@Test @MainActor func revalidationAfterHubRejectsTokenResumesLocalAlerts() async throws {
    let defaults = UserDefaults.standard
    let sessionID = "push-rejected-\(UUID().uuidString)"
    defaults.removeObject(forKey: AttentionWatcher.seenKey)
    defaults.removeObject(forKey: AttentionWatcher.readKey)
    let registrar = PushRegistrar()
    defer {
        PushRegistration.setActive(false, in: defaults)
        defaults.removeObject(forKey: AttentionWatcher.seenKey)
        defaults.removeObject(forKey: AttentionWatcher.readKey)
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
    }

    // The hub accepts the first upload, then Apple rejects the token (or the
    // hub's key) and every later upload gets #439's push-unavailable 503.
    let hubRejected = MockFlag()
    let uploads = MockCounter()
    MockURLProtocol.handler = { request in
        if request.httpMethod == "PUT", request.url?.path == "/auth/device/apns" {
            uploads.increment()
            if hubRejected.isRaised {
                return (503, Data(#"{"error":"hub push is unavailable"}"#.utf8))
            }
            return (204, Data())
        }
        return (200, pushRegistrarSnapshotData(sessionID: sessionID))
    }
    let client = DroverClient(
        config: ServerConfig(urlString: "http://drover.test")!,
        token: pushRegistrarFixtureCredential,
        session: MockURLProtocol.session()
    )
    registrar.updateClient(client)
    registrar.accept(token: Data([0x09, 0x28, 0x03]))
    let registered = await waitForPushRegistrarCondition {
        PushRegistration.isActive(in: defaults)
    }
    #expect(registered)

    // A repeated iOS callback alone does not re-upload once verified...
    registrar.accept(token: Data([0x09, 0x28, 0x03]))
    try await Task.sleep(nanoseconds: 50_000_000)
    #expect(uploads.value == 1)

    // ...but returning to the foreground asks the hub again.
    hubRejected.raise()
    registrar.revalidate()
    let fellBack = await waitForPushRegistrarCondition {
        uploads.value == 2 && !PushRegistration.isActive(in: defaults)
    }
    #expect(fellBack)

    let spy = PushRegistrarSpyNotifier()
    let snapshot = try HarnessSnapshot.decode(
        from: pushRegistrarSnapshotData(sessionID: sessionID)
    )
    await AttentionWatcher(notifier: spy, seenStore: defaults).evaluate(snapshot)
    #expect(await spy.notifiedIDs == [sessionID])
}

@Test @MainActor func revalidationThatSucceedsKeepsHubPushActive() async throws {
    let registrar = PushRegistrar()
    defer {
        PushRegistration.setActive(false)
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
    }
    let uploads = MockCounter()
    MockURLProtocol.handler = { request in
        if request.httpMethod == "PUT" { uploads.increment() }
        return (204, Data())
    }
    let client = DroverClient(config: ServerConfig(urlString: "http://drover.test")!,
                              token: pushRegistrarFixtureCredential, session: MockURLProtocol.session())
    registrar.updateClient(client)
    registrar.accept(token: Data([0x04]))
    #expect(await waitForPushRegistrarCondition { PushRegistration.isActive() })

    registrar.revalidate()
    #expect(await waitForPushRegistrarCondition { uploads.value == 2 })
    try await Task.sleep(nanoseconds: 50_000_000)
    #expect(PushRegistration.isActive())
}

@Test @MainActor func relaunchClearsPersistedSuccessBeforeATokenArrives() {
    PushRegistration.setActive(true)
    let registrar = PushRegistrar()
    #expect(!PushRegistration.isActive())
    registrar.updateClient(nil)
}

@Test @MainActor func signOutWithoutAClientClearsPersistedSuccess() async {
    let registrar = PushRegistrar()
    PushRegistration.setActive(true)
    await registrar.unregister()
    #expect(!PushRegistration.isActive())
}

@Test(arguments: [204, 503]) @MainActor
func lateUploadFromPreviousHubCannotChangeCurrentHubState(oldStatus: Int) async throws {
    let registrar = PushRegistrar()
    let oldResponseDelivered = MockFlag()
    let oldUploadStarted = MockFlag()
    defer {
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
        MockURLProtocol.responseDelay = nil
    }
    MockURLProtocol.responseDelay = { request in
        if request.url?.host == "old-hub.test" {
            oldUploadStarted.raise()
            return 0.2
        }
        return nil
    }
    MockURLProtocol.handler = { request in
        if request.url?.host == "old-hub.test" {
            oldResponseDelivered.raise()
            return (oldStatus, Data())
        }
        return (oldStatus == 204 ? 503 : 204, Data())
    }
    func client(_ host: String) -> DroverClient {
        DroverClient(config: ServerConfig(urlString: "http://\(host)")!,
                     token: pushRegistrarFixtureCredential, session: MockURLProtocol.session())
    }
    registrar.updateClient(client("old-hub.test"))
    registrar.accept(token: Data([0x01]))
    let started = await waitForPushRegistrarCondition { oldUploadStarted.isRaised }
    #expect(started)
    registrar.updateClient(client("new-hub.test"))
    let delivered = await waitForPushRegistrarCondition { oldResponseDelivered.isRaised }
    #expect(delivered)
    // Let the URLSession continuation finish before checking the actor state.
    try await Task.sleep(nanoseconds: 100_000_000)
    #expect(PushRegistration.isActive() == (oldStatus == 503))
}

@Test @MainActor func signOutInvalidatesAnUploadAlreadyInFlight() async throws {
    let registrar = PushRegistrar()
    let uploadStarted = MockFlag()
    let responseDelivered = MockFlag()
    defer {
        registrar.updateClient(nil)
        MockURLProtocol.handler = nil
        MockURLProtocol.responseDelay = nil
    }
    MockURLProtocol.responseDelay = { request in
        if request.httpMethod == "PUT" {
            uploadStarted.raise()
            return 0.2
        }
        return nil
    }
    MockURLProtocol.handler = { request in
        if request.httpMethod == "PUT" { responseDelivered.raise() }
        return (204, Data())
    }
    let client = DroverClient(config: ServerConfig(urlString: "http://drover.test")!,
                              token: pushRegistrarFixtureCredential, session: MockURLProtocol.session())
    registrar.updateClient(client)
    registrar.accept(token: Data([0x02]))
    let started = await waitForPushRegistrarCondition { uploadStarted.isRaised }
    #expect(started)
    await registrar.unregister()
    let delivered = await waitForPushRegistrarCondition { responseDelivered.isRaised }
    #expect(delivered)
    try await Task.sleep(nanoseconds: 100_000_000)
    #expect(!PushRegistration.isActive())
}

}
}
