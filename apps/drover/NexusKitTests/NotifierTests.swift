import Foundation
import Testing
@testable import NexusKit

// MARK: - SpyNotifier

/// Records every `notify`/`setBadge` call instead of touching
/// `UNUserNotificationCenter`, so `AttentionWatcher` tests stay hermetic.
private actor SpyNotifier: Notifying {
    private(set) var notifications: [(title: String, body: String, id: String)] = []
    private(set) var badgeCounts: [Int] = []

    func notify(title: String, body: String, id: String) async {
        notifications.append((title: title, body: body, id: id))
    }

    func setBadge(_ count: Int) async {
        badgeCounts.append(count)
    }
}

// MARK: - Snapshot fixture builder

private func snapshotData(
    _ sessions: [(id: String, harness: String, status: String, awaiting: String?, cwd: String?)]
) -> Data {
    let sessionsJSON = sessions.map { session -> String in
        let awaitingJSON = session.awaiting.map { "\"\($0)\"" } ?? "null"
        let cwdJSON = session.cwd.map { "\"\($0)\"" } ?? "null"
        return """
        {"session_id": "\(session.id)", "host_id": "host-1", "harness": "\(session.harness)",
         "mode": "structured", "status": "\(session.status)", "awaiting": \(awaitingJSON),
         "cwd": \(cwdJSON), "last_activity": null}
        """
    }.joined(separator: ",")
    return Data("""
    {"hosts": [], "sessions": [\(sessionsJSON)], "cwd_suggestions": []}
    """.utf8)
}

private func testDefaults() -> UserDefaults {
    UserDefaults(suiteName: "drover-notifier-test-\(UUID().uuidString)")!
}

// MARK: - Tests

/// `.serialized`: every test in this file mutates the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
@Suite(.serialized)
struct NotifierTests {

@Test func firstCheckNotifiesEachNeedsYouSessionAndSetsBadge() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-approval", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
        (id: "sess-input", harness: "gemini", status: "running", awaiting: "input", cwd: "/Users/arnab/other"),
        (id: "sess-working", harness: "shell", status: "running", awaiting: nil, cwd: "/tmp"),
    ])) }

    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.check(client: client())

    let notifications = await spy.notifications
    #expect(notifications.count == 2)
    #expect(notifications.contains {
        $0.id == "sess-approval" && $0.title == "claude-code needs you" && $0.body == "project — approval required"
    })
    #expect(notifications.contains {
        $0.id == "sess-input" && $0.title == "gemini needs you" && $0.body == "other — your turn"
    })

    let badges = await spy.badgeCounts
    #expect(badges == [2])
}

@Test func secondIdenticalCheckStaysSilent() async throws {
    let snapshotBytes = snapshotData([
        (id: "sess-approval", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
    ])
    MockURLProtocol.handler = { _ in (200, snapshotBytes) }

    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.check(client: client())
    await watcher.check(client: client())

    let notifications = await spy.notifications
    #expect(notifications.count == 1)   // identical second snapshot: no repeat alert
    let badges = await spy.badgeCounts
    #expect(badges == [1, 1])           // badge still refreshed on every check
}

@Test func sessionThatLeftAndReenteredNeedsYouReAlerts() async throws {
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())

    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
    ])) }
    await watcher.check(client: client())

    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-1", harness: "claude-code", status: "completed", awaiting: nil, cwd: "/Users/arnab/project"),
    ])) }
    await watcher.check(client: client())

    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
    ])) }
    await watcher.check(client: client())

    let notifications = await spy.notifications
    #expect(notifications.filter { $0.id == "sess-1" }.count == 2)  // alerted on entry, then again on re-entry
    let badges = await spy.badgeCounts
    #expect(badges == [1, 0, 1])
}

@Test func evaluateDiffsAProvidedSnapshotWithoutFetching() async throws {
    // Foreground polling already holds a fresh snapshot — evaluate() must
    // diff/notify from it directly, no second network fetch. Handler would
    // fail the test loudly if a fetch happened.
    MockURLProtocol.handler = { _ in
        Issue.record("evaluate() must not fetch")
        return (500, Data())
    }
    let snapshot = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-input", harness: "gemini", status: "running", awaiting: "input", cwd: "/Users/arnab/other"),
    ]))
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.evaluate(snapshot)

    let notifications = await spy.notifications
    #expect(notifications.count == 1)
    #expect(notifications[0].id == "sess-input")
    #expect(notifications[0].body == "other — your turn")
    let badges = await spy.badgeCounts
    #expect(badges == [1])
}

@Test func fetchFailureChangesNothing() async throws {
    let spy = SpyNotifier()
    let defaults = testDefaults()
    let watcher = AttentionWatcher(notifier: spy, seenStore: defaults)

    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
    ])) }
    await watcher.check(client: client())

    MockURLProtocol.handler = { _ in (500, Data()) }
    await watcher.check(client: client())

    let notifications = await spy.notifications
    #expect(notifications.count == 1)   // unchanged by the failed check
    let badges = await spy.badgeCounts
    #expect(badges == [1])              // no new setBadge call on failure
    #expect(Set(defaults.stringArray(forKey: AttentionWatcher.seenKey) ?? []) == ["sess-1"])  // seen set untouched
}

}
