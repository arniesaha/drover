import Foundation
import Testing
@testable import DroverKit

struct RetryPolicyTests {
    private let now = Date(timeIntervalSince1970: 1_000_000_000)

    @Test func parsesDeltaSecondsAndHTTPDates() {
        #expect(RetryPolicy.retryAfter(" 120 \n", now: now) == 120)
        #expect(RetryPolicy.retryAfter("0", now: now) == 0)
        #expect(RetryPolicy.retryAfter("Sun, 09 Sep 2001 01:48:40 GMT", now: now) == 120)
        #expect(RetryPolicy.retryAfter("Sunday, 09-Sep-01 01:48:40 GMT", now: now) == 120)
        #expect(RetryPolicy.retryAfter("Sun Sep 9 01:48:40 2001", now: now) == 120)
        #expect(RetryPolicy.retryAfter("Sun, 09 Sep 2001 01:46:00 GMT", now: now) == 0)
    }

    @Test(arguments: ["", "busy", "-1", "+1", "1.5", "NaN", "Infinity", "١٢", "1, 2"])
    func invalidHeadersUseLocalBackoff(value: String) {
        #expect(RetryPolicy.retryAfter(value, now: now) == nil)
        #expect(RetryPolicy.delay(retryAfter: RetryPolicy.retryAfter(value), jitter: 0) == 1)
    }

    @Test func clampsLocalBackoffAndJitterWithoutShorteningServerFloor() {
        #expect(RetryPolicy.delay(backoff: -10, jitter: -1) == 1)
        #expect(RetryPolicy.delay(backoff: 1000, jitter: 2) == 330)
        #expect(RetryPolicy.delay(retryAfter: 3600, jitter: 0) == 3600)
        #expect(RetryPolicy.delay(retryAfter: 3600, jitter: 1) == 3630)
        #expect(RetryPolicy.delay(backoff: .infinity, jitter: .nan) == 300)
        let huge = RetryPolicy.retryAfter(String(repeating: "9", count: 400), now: now)!
        #expect(huge.isFinite)
        #expect(huge > 3600)
    }

    @Test func jitterAlwaysAddsAtMostTwentyPercentOrThirtySeconds() {
        for floor in [1.0, 10, 120, 300, 3600] {
            for _ in 0..<100 {
                let delay = RetryPolicy.delay(retryAfter: floor)
                #expect(delay >= floor)
                #expect(delay <= floor + min(30, floor * 0.2))
            }
        }
    }

    @Test func gateNeverShortensDeadlineAndPersistsAcrossInstances() async {
        let suite = UUID().uuidString
        let url = URL(string: "http://retry.test")!
        let gate = HubRetryGate(defaults: UserDefaults(suiteName: suite)!)
        let first = await gate.record(for: url, header: "3600", now: now, jitter: 0)
        let second = await gate.record(for: url, header: "1", now: now, jitter: 0)
        #expect(first == second)
        let relaunched = HubRetryGate(defaults: UserDefaults(suiteName: suite)!)
        #expect(await relaunched.deadline(for: url, now: now) == first)
        #expect(await relaunched.deadline(for: url, now: first) == nil)
        #expect(await relaunched.deadline(for: URL(string: "http://other.test")!, now: now) == nil)
        UserDefaults(suiteName: suite)!.removeObject(forKey: "drover.retryAfter.\(url.absoluteString)")
    }

    @Test func waitIsCancellable() async throws {
        let task = Task { try await RetryPolicy.wait(until: Date().addingTimeInterval(3600)) }
        task.cancel()
        await #expect(throws: CancellationError.self) { try await task.value }
    }
}

extension MockNetworkTests {
@Suite(.serialized)
struct RetryCallSiteTests {
    let mock = MockNetwork()
    private func client() -> DroverClient { mock.client() }

    @Test @MainActor func allReadsAndBackgroundWatcherRespectSharedCooldown() async throws {
        nonisolated(unsafe) var requests = 0
        mock.responseHeaders = ["Retry-After": "120"]
        defer { mock.responseHeaders = nil }
        mock.handler = { _ in
            requests += 1
            return (503, Data())
        }
        let gate = HubRetryGate()
        let config = ServerConfig(urlString: "http://retry.test")!
        let first = DroverClient(config: config, token: "one", session: mock.session(), retryGate: gate)
        let background = DroverClient(config: config, token: "two", session: mock.session(), retryGate: gate)
        do { _ = try await first.snapshot(); Issue.record("Expected busy") }
        catch DroverError.busy(let deadline) { #expect(deadline.timeIntervalSinceNow >= 119) }
        let controlReads: [() async throws -> Void] = [
            { _ = try await first.snapshot() },
            { _ = try await first.messagePage(sessionID: "s1", request: .newest(limit: 50)) },
            { _ = try await first.authFlow(hostID: "h1", harness: "codex", flowID: "f1") },
        ]
        for read in controlReads {
            do { try await read(); Issue.record("Cooldown bypassed") }
            catch DroverError.busy { }
        }
        #expect(requests == 1)
        // The analytical lane is admitted separately by the hub: one probe,
        // then its own cooldown covers every analytical read.
        let analyticalReads: [() async throws -> Void] = [
            { _ = try await first.cockpitOverview() },
            { _ = try await first.analytics() },
            { _ = try await first.insights() },
            { _ = try await first.insightDetail(findingID: "f1") },
        ]
        for read in analyticalReads {
            do { try await read(); Issue.record("Cooldown bypassed") }
            catch DroverError.busy { }
        }
        #expect(requests == 2)
        let store = SessionStore(client: first)
        await store.refresh()
        #expect(store.busyUntil != nil)
        #expect(store.lastError?.hasPrefix("Hub busy, retrying in ") == true)
        let watcher = AttentionWatcher(notifier: RetryNotifier())
        #expect(await watcher.check(client: background) == false)
        #expect(requests == 2)
        // Push registration stays a single PUT and preserves #439's status.
        await #expect(throws: DroverError.httpStatus(503, "unexpected status 503")) {
            try await first.registerAPNsToken(Data([1]))
        }
        #expect(requests == 3)
    }

    @Test func analyticalCooldownDoesNotStallSessionReads() async throws {
        nonisolated(unsafe) var paths: [String] = []
        mock.responseHeaders = ["Retry-After": "120"]
        defer { mock.responseHeaders = nil }
        mock.handler = { request in
            let path = request.url?.path ?? ""
            paths.append(path)
            if path.hasPrefix("/cockpit") { return (503, Data()) }
            return (200, Data(#"{"hosts":[],"sessions":[]}"#.utf8))
        }
        let gate = HubRetryGate()
        let config = ServerConfig(urlString: "http://retry.test")!
        let client = DroverClient(
            config: config, token: "t", session: mock.session(), retryGate: gate
        )
        do { _ = try await client.cockpitOverview(); Issue.record("Expected busy") }
        catch DroverError.busy { }
        // The analytical store recovering must not hide the fleet (#363).
        _ = try await client.snapshot()
        _ = try await client.snapshot()
        #expect(await gate.deadline(for: config.baseURL) == nil)
        do { _ = try await client.insights(); Issue.record("Analytical cooldown bypassed") }
        catch DroverError.busy { }
        #expect(paths.filter { $0.hasPrefix("/cockpit") }.count == 1)
        #expect(paths.filter { $0 == "/harness" }.count == 2)
        #expect(paths.count == 3)
    }

    @Test func socketBusyCooldownSurvivesManualSessionRestart() async throws {
        nonisolated(unsafe) var requests = 0
        mock.handler = { _ in
            requests += 1
            return (200, Data(#"{"messages":[],"max_seq":0}"#.utf8))
        }
        let client = client()
        let stream = MessageStream(client: client, sessionID: "s1", connector: BusyMessageConnector())
        for await event in await stream.events() {
            if case .busy = event { break }
        }
        await stream.stop()
        // A new pump/client read must still honor the handshake's deadline.
        do { _ = try await client.snapshot(); Issue.record("Socket cooldown bypassed") }
        catch DroverError.busy(let deadline) { #expect(deadline.timeIntervalSinceNow >= 119) }
        #expect(requests == 1)
    }

    @Test func sessionStreamEmitsBusyAndDoesNotReconnectEarly() async throws {
        nonisolated(unsafe) var requests = 0
        mock.responseHeaders = ["Retry-After": "120"]
        defer { mock.responseHeaders = nil }
        mock.handler = { _ in requests += 1; return (503, Data()) }
        let stream = MessageStream(client: client(), sessionID: "s1", reconnectBaseDelay: .milliseconds(1))
        for await event in await stream.events() {
            if case .busy(let deadline) = event {
                #expect(deadline.timeIntervalSinceNow >= 119)
                break
            }
        }
        try await Task.sleep(for: .milliseconds(30))
        await stream.stop()
        #expect(requests == 1)
    }
}
}

private actor RetryNotifier: Notifying {
    func notify(title: String, body: String, id: String) async {}
    func setBadge(_ count: Int) async {}
}

private final class BusyTerminalConnector: TerminalConnecting, @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    let deadline = Date().addingTimeInterval(120)
    var connectCount: Int { lock.withLock { count } }

    func connect(_ request: URLRequest) -> TerminalConnection {
        lock.withLock { count += 1 }
        return TerminalConnection(frames: AsyncThrowingStream { continuation in
            continuation.finish(throwing: DroverError.busy(until: deadline))
        }, send: { _ in })
    }
}

struct RetrySocketTests {
    @Test func terminalNudgeCannotSkipServerCooldown() async throws {
        let connector = BusyTerminalConnector()
        let stream = TerminalStream(
            request: URLRequest(url: URL(string: "ws://retry.test/terminal")!),
            connector: connector, reconnectBaseDelay: .milliseconds(1)
        )
        for await event in await stream.events() {
            if case .connectFailed(let reason) = event {
                #expect(reason.hasPrefix("Hub busy, retrying in "))
                await stream.nudge()
                try await Task.sleep(for: .milliseconds(40))
                #expect(connector.connectCount == 1)
                await stream.stop()
                break
            }
        }
    }
}

private struct BusyMessageConnector: WebSocketConnecting {
    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            continuation.finish(throwing: DroverError.busy(until: Date().addingTimeInterval(120)))
        }
    }
}
