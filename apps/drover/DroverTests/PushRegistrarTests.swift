import Foundation
import Testing
@testable import Drover
@testable import DroverKit

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
    PushRegistration.setActive(true, in: defaults)
    defer {
        PushRegistration.setActive(false, in: defaults)
        defaults.removeObject(forKey: AttentionWatcher.seenKey)
        defaults.removeObject(forKey: AttentionWatcher.readKey)
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
        token: "device-token",
        session: MockURLProtocol.session()
    )

    PushRegistrar.shared.updateClient(client)
    #expect(!PushRegistration.isActive(in: defaults))
    PushRegistrar.shared.accept(token: Data([0x09, 0x28, 0x01]))

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
    PushRegistration.setActive(true, in: defaults)
    defer {
        PushRegistration.setActive(false, in: defaults)
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
        token: "device-token",
        session: MockURLProtocol.session()
    )

    PushRegistrar.shared.updateClient(client)
    #expect(!PushRegistration.isActive(in: defaults))

    PushRegistrar.shared.accept(token: Data([0x09, 0x28, 0x02]))

    let registered = await waitForPushRegistrarCondition {
        uploadAttempted.isRaised && PushRegistration.isActive(in: defaults)
    }
    #expect(registered)
}

}
}
