import Foundation
import UserNotifications

// MARK: - Notifying

/// Abstraction over "tell the user" so `AttentionWatcher` stays pure and
/// testable — production code gets `LocalNotifier` (real `UNUserNotification-
/// Center`), tests get a `SpyNotifier` that just records calls.
public protocol Notifying: Sendable {
    func notify(title: String, body: String, id: String) async
    func setBadge(_ count: Int) async
}

// MARK: - LocalNotifier

/// Posts an immediate (no trigger) local notification and mirrors the badge
/// count onto the app icon via `UNUserNotificationCenter`. Both calls are
/// best-effort: if the user hasn't granted notification permission, `add`
/// and `setBadgeCount` simply no-op/fail silently, which is fine — the badge
/// is a courtesy, not the source of truth (that's the sessions list).
public struct LocalNotifier: Notifying {
    public init() {}

    public func notify(title: String, body: String, id: String) async {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        // "Needs you" is exactly the time-sensitive case: the harness is
        // blocked on the user. Breaks through Focus modes and lock-screen
        // deferral (requires the time-sensitive entitlement; without it iOS
        // silently downgrades to .active, so this is safe either way).
        content.interruptionLevel = .timeSensitive
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }

    public func setBadge(_ count: Int) async {
        try? await UNUserNotificationCenter.current().setBadgeCount(count)
    }
}

// MARK: - AttentionWatcher

/// Pure, testable core the BGTask (and foreground polling) drives: fetch a
/// snapshot, diff which sessions newly need the user against what was seen
/// last time, fire one notification per newly-needy session, refresh the
/// badge to the full needs-you count, then persist that full set so a
/// session that resolves and later needs attention again can re-alert.
///
/// A fetch failure changes nothing — no alert, no badge update, and the seen
/// set is left untouched, so a transient network blip can never either spam
/// a false alert or silently suppress a real one on the next successful check.
///
/// The badge counts sessions that want the user *and* that the user has not
/// opened yet. Opening one is not answering it — it stays in the inbox, still
/// flagged — but the badge exists to say "go look", and once you have looked
/// it has done its job. Read receipts are pruned the moment a session stops
/// needing the user, so its next question badges again; without that, a
/// session read once would stay silent for the life of the install.
public struct AttentionWatcher: Sendable {
    public static let seenKey = "drover.needsyou.seen"
    public static let readKey = "drover.needsyou.read"

    private let notifier: Notifying
    // `UserDefaults` isn't `Sendable`, but it's documented thread-safe and is
    // only ever read/written synchronously within `check(client:)` — same
    // rationale as the other `nonisolated(unsafe)` uses in this package.
    private nonisolated(unsafe) let seenStore: UserDefaults

    public init(notifier: Notifying, seenStore: UserDefaults = .standard) {
        self.notifier = notifier
        self.seenStore = seenStore
    }

    public func check(client: DroverClient) async {
        let snapshot: HarnessSnapshot
        do {
            snapshot = try await client.snapshot()
        } catch {
            return  // silence: never a false alert, never a lost seen-set
        }
        await evaluate(snapshot)
    }

    /// Diff/notify from a snapshot the caller already holds — the foreground
    /// polling path (SessionStore refreshes every few seconds) drives this so
    /// "response completed" alerts arrive near-real-time without a second
    /// fetch. Shares the persisted seen-set with the BGTask path, so the two
    /// never double-alert for the same transition.
    public func evaluate(_ snapshot: HarnessSnapshot) async {
        let needsYou = Self.needsYou(in: snapshot)
        let previousIDs = Set(seenStore.stringArray(forKey: Self.seenKey) ?? [])
        let fresh = SessionStore.newlyNeedsYou(current: snapshot.sessions, previousIDs: previousIDs)

        for session in fresh {
            await notifier.notify(
                title: "\(session.harness) needs you",
                body: "\(Self.cwdBasename(session)) — \(Self.bodySuffix(session))",
                id: session.id
            )
        }

        let read = prunedRead(against: needsYou)
        await notifier.setBadge(needsYou.filter { !read.contains($0.id) }.count)
        seenStore.set(needsYou.map(\.id), forKey: Self.seenKey)
    }

    /// Record that the user has opened this session, and refresh the badge to
    /// match. Marking a session that is not asking for anything is harmless:
    /// it is pruned on the next pass and never counted in the first place.
    public func markRead(_ sessionID: String, in snapshot: HarnessSnapshot) async {
        let needsYou = Self.needsYou(in: snapshot)
        var read = prunedRead(against: needsYou)
        read.insert(sessionID)
        seenStore.set(Array(read), forKey: Self.readKey)
        await notifier.setBadge(needsYou.filter { !read.contains($0.id) }.count)
    }

    /// The stored receipts, minus any session that no longer wants the user.
    /// Pruning here rather than on a timer keeps the set bounded by the inbox
    /// and makes "asked, read, asked again" behave like a new question.
    private func prunedRead(against needsYou: [SessionSummary]) -> Set<String> {
        let stored = Set(seenStore.stringArray(forKey: Self.readKey) ?? [])
        let pruned = stored.intersection(needsYou.map(\.id))
        if pruned != stored {
            seenStore.set(Array(pruned), forKey: Self.readKey)
        }
        return pruned
    }

    private static func needsYou(in snapshot: HarnessSnapshot) -> [SessionSummary] {
        snapshot.sessions.filter {
            $0.attention == .needsApproval || $0.attention == .needsInput
        }
    }

    private static func bodySuffix(_ session: SessionSummary) -> String {
        session.attention == .needsApproval ? "approval required" : "your turn"
    }

    private static func cwdBasename(_ session: SessionSummary) -> String {
        guard let cwd = session.cwd, !cwd.isEmpty else { return session.harness }
        return URL(fileURLWithPath: cwd).lastPathComponent
    }
}
