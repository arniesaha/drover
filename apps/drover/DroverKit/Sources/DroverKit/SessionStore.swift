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
    /// The kind of the most recent failed snapshot refresh. Presentation uses
    /// this alongside `lastError` so a Tailscale address cannot turn an
    /// authentication, decoding, HTTP, or cancellation error into a
    /// connectivity claim.
    public enum RefreshFailure: Sendable, Equatable {
        case transport
        case authentication
        case decoding
        case http
        case cancellation
        case other
    }

    private let client: DroverClient?

    public private(set) var snapshot: HarnessSnapshot?
    public private(set) var lastError: String?
    public private(set) var lastRefreshFailure: RefreshFailure?
    public private(set) var isReachable: Bool = false

    /// True when the underlying client is pointed at a Tailscale endpoint.
    public var isTailscaleAddress: Bool {
        client?.config.isTailscaleAddress ?? false
    }

    /// The host part if the underlying client is pointed at a Tailscale endpoint.
    public var tailscaleHost: String? {
        client?.config.tailscaleHost
    }

    /// The only condition that merits Tailscale-specific recovery copy.
    /// Merely being configured with a Tailscale address is not evidence that
    /// Tailscale caused an authentication, decoding, HTTP, or app cancellation.
    public var isTailscaleTransportFailure: Bool {
        isTailscaleAddress && lastRefreshFailure == .transport
    }

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

    /// The inbox list, as one uninterrupted run ordered by recency: whatever
    /// moved most recently is at the top, whether it is waiting on you or
    /// still working. Attention only breaks ties between sessions with the
    /// same (or no) activity date — see `activeOrdering`.
    ///
    /// The screen used to build this itself, as two `ForEach`es with four
    /// analytics sections between them, needs-you above and working below.
    /// That read as a sort bug: a session touched 27 minutes ago rendered
    /// three sections below one last touched two days ago (#80).
    ///
    /// Pinning capacity fixed the split; ordering by recency was the second
    /// half of the decision. Bucket-first was defensible while the analytics
    /// sections separated the two groups — once the list is contiguous, a
    /// two-day-old question sitting above live work is just stale-first.
    public var inboxSessions: [SessionSummary] {
        Self.inboxSessions(from: snapshot?.sessions ?? [])
    }

    public nonisolated static func inboxSessions(from sessions: [SessionSummary]) -> [SessionSummary] {
        activeSessions(from: sessions)
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

    /// How many times `refresh()` has been attempted since the last success.
    /// Reset on success, so it only ever describes an unresolved stretch.
    public private(set) var refreshAttempts = 0

    /// Why the last attempt did not produce a snapshot, including the
    /// cancellations that are otherwise invisible. Nil once one succeeds.
    public private(set) var lastRefreshOutcome: String?

    /// When the snapshot currently being rendered actually landed.
    ///
    /// Only a *success* moves this. Keeping the cached snapshot through a
    /// failure is deliberate (see `refresh()`); restamping it on failure would
    /// make the cache look freshly fetched, which is the same lie the ticking
    /// card timestamps were telling (#81).
    public private(set) var lastSuccessfulRefresh: Date?

    /// How far behind the rendered snapshot is, for the views drawn from it.
    /// `now` is a parameter so a card's staleness can be tested without
    /// waiting for a clock.
    public func freshness(now: Date = Date()) -> SnapshotFreshness {
        SnapshotFreshness(lastUpdate: lastSuccessfulRefresh, isReachable: isReachable, now: now)
    }

    /// Consecutive cancellations while nothing has ever loaded. Reset by a
    /// success and by an honest failure, so it only ever counts an unbroken
    /// run of first loads torn down before they landed.
    private var cancelledFirstLoads = 0

    /// How many of those it takes before the screen stops pretending it is
    /// merely slow. Two is already the point at which `connectingDetail`
    /// starts explaining itself; the third says there is nothing left to wait
    /// for.
    private static let cancelledFirstLoadLimit = 3

    /// The line the "Connecting…" screen shows once it has been sitting there
    /// long enough to owe an explanation.
    ///
    /// A wedged first load and an unreachable hub look identical from the
    /// phone — same spinner, same silence — which is exactly why the recurring
    /// report could not be diagnosed from the device (#85). One blip is
    /// normal, so this stays quiet until the second failure.
    public var connectingDetail: String? {
        guard !hasLoadedOnce, refreshAttempts >= 2 else { return nil }
        let plural = refreshAttempts == 1 ? "attempt" : "attempts"
        return "\(refreshAttempts) \(plural) · \(lastRefreshOutcome ?? "no response yet")"
    }

    public func refresh() async {
        guard let client else { return }
        refreshAttempts += 1
        do {
            let fresh = try await client.snapshot()
            snapshot = fresh
            lastError = nil
            lastRefreshFailure = nil
            isReachable = true
            hasLoadedOnce = true
            refreshAttempts = 0
            cancelledFirstLoads = 0
            lastRefreshOutcome = nil
            lastSuccessfulRefresh = Date()
        } catch {
            // A cancelled request means *we* tore it down (a superseded poll,
            // a dismissed screen) — the hub never said anything. Treating it
            // as a failure flashed an unreachable banner over a perfectly
            // healthy fleet.
            //
            // It is still recorded. Returning without a trace is what made a
            // load stuck behind repeated cancellations indistinguishable from
            // an unreachable hub, and left "Connecting…" with nothing to say.
            if Self.isCancellation(error) {
                lastRefreshOutcome = Self.cancelledOutcome
                noteCancelledFirstLoad()
                return
            }
            cancelledFirstLoads = 0
            isReachable = false
            lastRefreshFailure = Self.classify(error)
            lastError = Self.errorMessage(
                for: error,
                isTailscale: isTailscaleTransportFailure
            )
            lastRefreshOutcome = lastError
            // Deliberately keep the cached `snapshot` as-is.
        }
    }

    /// A first load lost to cancellation, again.
    ///
    /// Passing in silence is right for a *single* superseded poll and stays
    /// that way — over a fleet that has already loaded it is right every time.
    /// But before anything has landed, silence renders as an eternal spinner
    /// with no retry and no explanation, which self-heals only by luck (#85):
    /// a `/harness` taking seconds instead of milliseconds (#95) gives every
    /// scene-phase change a wide window to cancel in, and each cancellation
    /// looks exactly like the last.
    ///
    /// So an unbroken run of them stops being treated as a slow start and
    /// becomes what it already is in practice — a failure the user can retry.
    /// This is the existing unreachable presentation, not a new one.
    private func noteCancelledFirstLoad() {
        guard !hasLoadedOnce else { return }
        cancelledFirstLoads += 1
        guard cancelledFirstLoads >= Self.cancelledFirstLoadLimit else { return }
        isReachable = false
        lastRefreshFailure = .cancellation
        lastError = "The first load kept being interrupted before it landed — the hub may be busy."
    }

    private static let cancelledOutcome = "request cancelled"

    private nonisolated static func isCancellation(_ error: Error) -> Bool {
        if let droverError = error as? DroverError { return droverError.isCancellation }
        return (error as? URLError)?.code == .cancelled
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
            lastRefreshFailure = nil
            return continued
        } catch {
            switch error {
            case DroverError.conflict(let message), DroverError.badRequest(let message):
                lastError = message
            default:
                lastError = "Couldn't start handoff — is the host online?"
            }
            lastRefreshFailure = nil
            return nil
        }
    }

    // MARK: - Polling

    /// Starts the poll loop, or leaves a running one exactly as it is.
    ///
    /// This used to call `stopPolling()` first, which cancels the in-flight
    /// request — and `SessionsView` calls it from both `.task` and the
    /// scene-phase change, so an ordinary launch could cancel its own first
    /// load and render "Connecting…" over a healthy fleet (#85). Re-entry has
    /// nothing to do when a loop is already polling, so it now does nothing.
    /// Backgrounding still calls `stopPolling()`, which clears the task, so
    /// the foreground event that follows genuinely restarts the loop.
    public func startPolling(every seconds: Double = 5) {
        guard pollingTask == nil else { return }
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                // Belt two: if the store is gone, exit instead of spinning
                // forever as an empty loop. The `do` scope releases the
                // strong reference before the sleep so the loop never keeps
                // a dropped store alive across a poll interval.
                var delay = seconds
                do {
                    guard let self else { return }
                    await self.refresh()
                    delay = self.pollDelay(base: seconds)
                }
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(delay))
            }
        }
    }

    /// How long to wait before the next poll.
    ///
    /// A cancellation before anything has ever loaded retries almost at once
    /// rather than sleeping out the interval: the whole point of the first
    /// load is that there is nothing on screen until it lands, and making a
    /// torn-down attempt cost five seconds is what let a burst of launch-time
    /// churn starve it (#85). Everything else — a success, an honest failure
    /// — keeps the ordinary cadence.
    ///
    /// It also stops once the screen has given up (`lastError` is set): from
    /// there the user has a Retry button, and a hub slow enough to widen the
    /// cancellation window (#95) is the last thing that should be polled four
    /// times a second forever.
    private func pollDelay(base seconds: Double) -> Double {
        guard !hasLoadedOnce, cancelledFirstLoads > 0, lastError == nil else { return seconds }
        return min(Self.cancelledFirstLoadRetry, seconds)
    }

    private static let cancelledFirstLoadRetry = 0.25

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

    private static func errorMessage(for error: Error, isTailscale: Bool = false) -> String {
        // DroverError is LocalizedError, so localizedDescription is the human
        // string. The old "\(error)" fallback printed raw enum reflection —
        // that is where the literal `transport("cancelled")` banner came from.
        if let droverError = error as? DroverError {
            return droverError.localizedDescription(isTailscale: isTailscale)
        }
        if isTailscale {
            return DroverError.unreachableTailscaleDescription
        }
        return (error as NSError).localizedDescription
    }

    private nonisolated static func classify(_ error: Error) -> RefreshFailure {
        if let droverError = error as? DroverError {
            switch droverError {
            case .transport:
                return droverError.isCancellation ? .cancellation : .transport
            case .unauthorized:
                return .authentication
            case .decoding:
                return .decoding
            case .httpStatus:
                return .http
            case .conflict, .badRequest, .unavailable:
                return .other
            }
        }
        if let urlError = error as? URLError {
            return urlError.code == .cancelled ? .cancellation : .transport
        }
        return .other
    }
}
