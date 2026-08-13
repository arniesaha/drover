import Foundation
import Testing
@testable import DroverKit

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
extension MockNetworkTests {
@Suite(.serialized)
struct NotifierTests {

@Test func firstCheckNotifiesEachNeedsYouSessionAndSetsBadge() async throws {
    MockURLProtocol.handler = { _ in (200, snapshotData([
        (id: "sess-approval", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/Users/arnab/project"),
        (id: "sess-input", harness: "agy", status: "running", awaiting: "input", cwd: "/Users/arnab/other"),
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
        $0.id == "sess-input" && $0.title == "agy needs you" && $0.body == "other — your turn"
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
        (id: "sess-input", harness: "agy", status: "running", awaiting: "input", cwd: "/Users/arnab/other"),
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


// MARK: - Read receipts
//
// The badge counts sessions that still want the user *and* that the user has
// not looked at yet. Opening a session is not the same as answering it — the
// session stays in the inbox, and stays needs-you — but the badge is a prompt
// to go look, and once you have looked it has done its job.

@Test func openingANeedySessionClearsItFromTheBadge() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-a", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/p/a"),
        (id: "sess-b", harness: "agy", status: "running", awaiting: "input", cwd: "/p/b"),
    ]))
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.evaluate(snapshot)

    await watcher.markRead("sess-a", in: snapshot)

    let badges = await spy.badgeCounts
    #expect(badges == [2, 1])
}

@Test func aReadSessionStaysReadAcrossLaterRefreshes() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-a", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/p/a"),
        (id: "sess-b", harness: "agy", status: "running", awaiting: "input", cwd: "/p/b"),
    ]))
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.evaluate(snapshot)
    await watcher.markRead("sess-a", in: snapshot)

    await watcher.evaluate(snapshot)

    let badges = await spy.badgeCounts
    #expect(badges == [2, 1, 1])
}

@Test func readingEverySessionEmptiesTheBadgeWithoutEmptyingTheInbox() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-a", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/p/a"),
    ]))
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.evaluate(snapshot)

    await watcher.markRead("sess-a", in: snapshot)

    let badges = await spy.badgeCounts
    #expect(badges == [1, 0])
    // The session is untouched — the badge is a prompt, not the inbox.
    #expect(snapshot.sessions.contains { $0.id == "sess-a" && $0.attention == .needsApproval })
}

@Test func aSessionThatAsksAgainAfterBeingReadBadgesAgain() async throws {
    // Read receipts are pruned when a session stops needing the user, so the
    // next question is a fresh one. Without pruning, a session read once
    // would never badge again for the life of the install.
    let spy = SpyNotifier()
    let defaults = testDefaults()
    let watcher = AttentionWatcher(notifier: spy, seenStore: defaults)

    let asking = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/p/a"),
    ]))
    await watcher.evaluate(asking)
    await watcher.markRead("sess-1", in: asking)

    let working = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: nil, cwd: "/p/a"),
    ]))
    await watcher.evaluate(working)

    await watcher.evaluate(asking)

    let badges = await spy.badgeCounts
    #expect(badges == [1, 0, 0, 1])
}

@Test func markingAnUnrelatedSessionReadDoesNotChangeTheBadge() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-a", harness: "claude-code", status: "running", awaiting: "approval", cwd: "/p/a"),
        (id: "sess-working", harness: "shell", status: "running", awaiting: nil, cwd: "/p/w"),
    ]))
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    await watcher.evaluate(snapshot)

    await watcher.markRead("sess-working", in: snapshot)

    let badges = await spy.badgeCounts
    #expect(badges == [1, 1])
}


@Test func openingASessionBeforeItAsksDoesNotSwallowTheLaterQuestion() async throws {
    // A receipt means "I have seen this question", not "I have seen this
    // session". Recording one for a session that was merely working would
    // suppress the badge for a question asked minutes after the user left.
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())

    let working = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: nil, cwd: "/p/a"),
    ]))
    await watcher.evaluate(working)
    await watcher.markRead("sess-1", in: working)

    let asking = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
    ]))
    await watcher.evaluate(asking)

    let badges = await spy.badgeCounts
    #expect(badges == [0, 0, 1])
}

// MARK: - sync (the push double-alert)

@Test func syncAbsorbsWhatIsWaitingWithoutAlerting() async throws {
    // The hub already pushed for these while the app was backgrounded; the
    // app opening must not announce them a second time.
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    let waiting = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
        (id: "sess-2", harness: "codex", status: "running", awaiting: "approval", cwd: "/p/b"),
    ]))

    await watcher.sync(waiting)

    let notifications = await spy.notifications
    #expect(notifications.isEmpty)
    // The badge is still the truth, so it is set either way.
    #expect(await spy.badgeCounts == [2])
}

@Test func aSyncedSessionIsNotReAlertedByTheNextEvaluate() async throws {
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    let waiting = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
    ]))

    await watcher.sync(waiting)
    await watcher.evaluate(waiting)

    // This is the reported bug: pushed alert, then a generic local one the
    // moment the app opened.
    #expect(await spy.notifications.isEmpty)
}

@Test func syncDoesNotSuppressATransitionThatHappensAfterwards() async throws {
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    let waiting = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
    ]))
    await watcher.sync(waiting)

    let alsoSecond = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
        (id: "sess-2", harness: "codex", status: "running", awaiting: "approval", cwd: "/p/b"),
    ]))
    await watcher.evaluate(alsoSecond)

    // Absorbing the backlog must not go on to mute the session that starts
    // waiting while the user is watching.
    let ids = await spy.notifications.map(\.id)
    #expect(ids == ["sess-2"])
}

@Test func syncOnAnIdleFleetClearsTheBadge() async throws {
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    let idle = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-1", harness: "claude-code", status: "running", awaiting: nil, cwd: "/p/a"),
    ]))

    await watcher.sync(idle)

    #expect(await spy.badgeCounts == [0])
}

@Test func aLocalNotificationCarriesItsSessionIdForTapRouting() async throws {
    // The tap handler reads this key; without it a local alert can only fall
    // back to the request identifier.
    let spy = SpyNotifier()
    let watcher = AttentionWatcher(notifier: spy, seenStore: testDefaults())
    let asking = try HarnessSnapshot.decode(from: snapshotData([
        (id: "sess-42", harness: "claude-code", status: "running", awaiting: "input", cwd: "/p/a"),
    ]))

    await watcher.evaluate(asking)

    #expect(await spy.notifications.map(\.id) == ["sess-42"])
}

}

}  // extension MockNetworkTests
