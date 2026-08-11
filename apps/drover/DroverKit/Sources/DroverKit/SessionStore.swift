import Foundation
import Observation

/// One host's slice of the fleet: the host row plus its *active* sessions
/// (needs-approval / needs-input / working), waiting-first.
public struct HostGroup: Sendable, Equatable, Identifiable {
    public let host: HostSummary
    public let sessions: [SessionSummary]

    public var id: String { host.id }

    public init(host: HostSummary, sessions: [SessionSummary]) {
        self.host = host
        self.sessions = sessions
    }
}

/// Observable façade over a `DroverClient` snapshot: buckets sessions by what
/// the user needs to do about them, polls on an interval, and never loses
/// the last-known-good snapshot just because a refresh failed.
@MainActor
@Observable
public final class SessionStore {
    private let client: DroverClient?

    public private(set) var snapshot: HarnessSnapshot?
    public private(set) var lastError: String?
    public private(set) var isReachable: Bool = false

    /// True once any refresh has succeeded. Lets the UI distinguish
    /// "never loaded" (spinner / retriable error) from "loaded but empty".
    /// Never reset: after first success the list renders last-known state.
    public private(set) var hasLoadedOnce = false

    // `nonisolated(unsafe)` solely so `deinit` (nonisolated in Swift 6) can
    // cancel it; every other access is from `@MainActor` methods, and deinit
    // runs with exclusive access to the dying object, so there is no race.
    private nonisolated(unsafe) var pollingTask: Task<Void, Never>?

    public init(client: DroverClient? = nil) {
        self.client = client
    }

    deinit {
        // Belt one: whoever drops the store (e.g. SwiftUI discarding a view
        // subtree via `.id(_:)`) must not leak a still-spinning poll task.
        pollingTask?.cancel()
    }

    // MARK: - Buckets

    /// Sessions that need the user's attention, approval requests first,
    /// then by most-recent activity.
    public var needsYou: [SessionSummary] {
        let sessions = (snapshot?.sessions ?? []).filter {
            $0.attention == .needsApproval || $0.attention == .needsInput
        }
        return sessions.sorted(by: Self.needsYouOrdering)
    }

    public var activeSessions: [SessionSummary] {
        Self.activeSessions(from: snapshot?.sessions ?? [])
    }

    public nonisolated static func activeSessions(from sessions: [SessionSummary]) -> [SessionSummary] {
        sessions
            .filter {
                switch $0.attention {
                case .needsApproval, .needsInput, .working: return true
                case .done, .errored: return false
                }
            }
            .sorted(by: Self.activeOrdering)
    }

    public var working: [SessionSummary] {
        (snapshot?.sessions ?? [])
            .filter { $0.attention == .working }
            .sorted(by: Self.byActivityDescending)
    }

    /// The inbox list, as one uninterrupted run: everything that wants a human
    /// first (approvals before questions, newest first), then everything still
    /// running (newest first).
    ///
    /// The screen used to build this itself, as two `ForEach`es with four
    /// analytics sections between them — which read as a sort bug, because a
    /// session touched 27 minutes ago rendered three sections below one last
    /// touched two days ago. The buckets are still real and still in this
    /// order; they are simply no longer allowed to be split apart (see #80).
    public var inboxSessions: [SessionSummary] {
        Self.inboxSessions(from: snapshot?.sessions ?? [])
    }

    public nonisolated static func inboxSessions(from sessions: [SessionSummary]) -> [SessionSummary] {
        let needsYou = sessions
            .filter { $0.attention == .needsApproval || $0.attention == .needsInput }
            .sorted(by: needsYouOrdering)
        let working = sessions
            .filter { $0.attention == .working }
            .sorted(by: byActivityDescending)
        return needsYou + working
    }

    public var finished: [SessionSummary] {
        (snapshot?.sessions ?? [])
            .filter { $0.attention == .done || $0.attention == .errored }
            .sorted(by: Self.byActivityDescending)
    }

    /// Sessions grouped by host, fleet-first: online hosts before stale
    /// before offline, waiting sessions at the top of their group. Hosts
    /// with no active sessions still appear (the one-glance fleet view);
    /// sessions whose host the hub no longer lists get a synthesized
    /// offline group rather than vanishing.
    public var hostGroups: [HostGroup] {
        Self.hostGroups(hosts: snapshot?.hosts ?? [], sessions: snapshot?.sessions ?? [])
    }

    public nonisolated static func hostGroups(
        hosts: [HostSummary],
        sessions: [SessionSummary]
    ) -> [HostGroup] {
        let active = sessions.filter {
            switch $0.attention {
            case .needsApproval, .needsInput, .working: return true
            case .done, .errored: return false
            }
        }
        var byHost = Dictionary(grouping: active, by: \.hostID)
        var groups = hosts.map { host in
            HostGroup(
                host: host,
                sessions: (byHost.removeValue(forKey: host.id) ?? []).sorted(by: groupOrdering)
            )
        }
        for (hostID, orphans) in byHost {
            groups.append(HostGroup(
                host: HostSummary(id: hostID, displayName: hostID, status: "offline"),
                sessions: orphans.sorted(by: groupOrdering)
            ))
        }
        return groups.sorted(by: hostOrdering)
    }

    private nonisolated static func attentionRank(_ session: SessionSummary) -> Int {
        switch session.attention {
        case .needsApproval: return 0
        case .needsInput: return 1
        default: return 2
        }
    }

    private nonisolated static func groupOrdering(_ a: SessionSummary, _ b: SessionSummary) -> Bool {
        let (ra, rb) = (attentionRank(a), attentionRank(b))
        if ra != rb { return ra < rb }
        return byActivityDescending(a, b)
    }

    private nonisolated static func presenceRank(_ host: HostSummary) -> Int {
        switch host.presence {
        case .online: return 0
        case .stale: return 1
        case .offline: return 2
        }
    }

    private nonisolated static func hostOrdering(_ a: HostGroup, _ b: HostGroup) -> Bool {
        let (ra, rb) = (presenceRank(a.host), presenceRank(b.host))
        if ra != rb { return ra < rb }
        return a.host.title.localizedCaseInsensitiveCompare(b.host.title) == .orderedAscending
    }

    // MARK: - Refresh

    public func refresh() async {
        guard let client else { return }
        do {
            let fresh = try await client.snapshot()
            snapshot = fresh
            lastError = nil
            isReachable = true
            hasLoadedOnce = true
        } catch {
            // A cancelled request means *we* tore it down (a superseded poll,
            // a dismissed screen) — the hub never said anything. Treating it
            // as a failure flashed an unreachable banner over a perfectly
            // healthy fleet.
            if let droverError = error as? DroverError, droverError.isCancellation {
                return
            }
            if (error as? URLError)?.code == .cancelled {
                return
            }
            isReachable = false
            lastError = Self.errorMessage(for: error)
            // Deliberately keep the cached `snapshot` as-is.
        }
    }

    // MARK: - Handoff

    /// Server-side handoff from the session list: returns the new session
    /// (id plus whether it's structured, for navigation), or nil on failure.
    /// Refresh failures flip `isReachable` and surface in the unreachable
    /// banner; action failures keep `isReachable == true` and surface in the
    /// sessions list's inline error section. `targetHarness` retargets the
    /// new session's harness; nil keeps the source session's own.
    public func continueSession(_ sessionID: String, targetHarness: String? = nil) async -> ContinuedSession? {
        guard let client else { return nil }
        do {
            let continued = try await client.continueSession(sessionID: sessionID,
                                                             targetHarness: targetHarness)
            lastError = nil
            return continued
        } catch {
            switch error {
            case DroverError.conflict(let message), DroverError.badRequest(let message):
                lastError = message
            default:
                lastError = "Couldn't start handoff — is the host online?"
            }
            return nil
        }
    }

    // MARK: - Polling

    public func startPolling(every seconds: Double = 5) {
        stopPolling()
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                // Belt two: if the store is gone, exit instead of spinning
                // forever as an empty loop. The `do` scope releases the
                // strong reference before the sleep so the loop never keeps
                // a dropped store alive across a poll interval.
                do {
                    guard let self else { return }
                    await self.refresh()
                }
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    public func stopPolling() {
        pollingTask?.cancel()
        pollingTask = nil
    }

    // MARK: - Notification diffing

    /// Sessions in `current` that need attention now and whose id was not in
    /// `previousIDs` — i.e. newly needing the user, for local notifications.
    public nonisolated static func newlyNeedsYou(current: [SessionSummary], previousIDs: Set<String>) -> [SessionSummary] {
        current.filter { session in
            (session.attention == .needsApproval || session.attention == .needsInput)
                && !previousIDs.contains(session.id)
        }
    }

    // MARK: - Private helpers

    private nonisolated static func needsYouOrdering(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool {
        if lhs.attention != rhs.attention {
            return lhs.attention == .needsApproval
        }
        return byActivityDescending(lhs, rhs)
    }

    private nonisolated static func activeOrdering(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool {
        if let ordered = activityDateOrdering(lhs, rhs) {
            return ordered
        }
        let (ra, rb) = (attentionRank(lhs), attentionRank(rhs))
        if ra != rb { return ra < rb }
        return lhs.id < rhs.id
    }

    private nonisolated static func byActivityDescending(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool {
        activityDateOrdering(lhs, rhs) ?? (lhs.id < rhs.id)
    }

    private nonisolated static func activityDateOrdering(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool? {
        switch (lhs.activityDate, rhs.activityDate) {
        case let (l?, r?) where l != r: return l > r
        case (_?, nil): return true
        case (nil, _?): return false
        default: return nil
        }
    }

    private static func errorMessage(for error: Error) -> String {
        // DroverError is LocalizedError, so localizedDescription is the human
        // string. The old "\(error)" fallback printed raw enum reflection —
        // that is where the literal `transport("cancelled")` banner came from.
        if let droverError = error as? DroverError {
            return droverError.localizedDescription
        }
        return (error as NSError).localizedDescription
    }
}
