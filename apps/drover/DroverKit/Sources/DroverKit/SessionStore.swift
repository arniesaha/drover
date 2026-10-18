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
    private var optimisticEnded: [String: SessionSummary] = [:]

    public func markSessionEnded(_ id: String, ended: Bool) {
        guard let index = snapshot?.sessions.firstIndex(where: { $0.id == id }) else { return }
        if ended {
            if let status = snapshot?.sessions[index].status,
               ["completed", "terminated", "errored", "failed"].contains(status) { return }
            if optimisticEnded[id] == nil { optimisticEnded[id] = snapshot?.sessions[index] }
            snapshot?.sessions[index].status = "terminated"
            snapshot?.sessions[index].awaiting = nil
        } else if let previous = optimisticEnded.removeValue(forKey: id) {
            snapshot?.sessions[index] = previous
        }
    }

    @ObservationIgnored
    private var cachedInboxSessions: [SessionSummary] = []

    public private(set) var snapshot: HarnessSnapshot? {
        didSet {
            cachedInboxSessions = Self.activeSessions(from: snapshot?.sessions ?? [], existing: cachedInboxSessions)
        }
    }
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
        let sessions = SessionBuckets(sessions: snapshot?.sessions ?? []).needsYou
        return sessions.sorted(by: Self.needsYouOrdering)
    }

    public var activeSessions: [SessionSummary] {
        if cachedInboxSessions.isEmpty, let sessions = snapshot?.sessions, !sessions.isEmpty {
            cachedInboxSessions = Self.activeSessions(from: sessions, existing: cachedInboxSessions)
        }
        return cachedInboxSessions
    }

    public nonisolated static func activeSessions(
        from sessions: [SessionSummary],
        existing: [SessionSummary] = [],
        hysteresis: TimeInterval = SessionOrdering.defaultHysteresis
    ) -> [SessionSummary] {
        let active = sessions.filter {
            switch $0.attention {
            case .needsApproval, .needsInput, .working: return true
            case .done, .errored: return false
            }
        }
        return SessionOrdering.sort(active, existing: existing, hysteresis: hysteresis)
    }

    public var working: [SessionSummary] {
        (snapshot?.sessions ?? [])
            .filter { $0.attention == .working }
            .sorted(by: Self.byActivityDescending)
    }

    /// The inbox list, ordered deterministically: needs-you first (approval then
    /// input), followed by running sessions, with recency sort stabilized by
    /// hysteresis so timestamp jitter does not cause rows to swap on refresh.
    public var inboxSessions: [SessionSummary] {
        activeSessions
    }

    public nonisolated static func inboxSessions(
        from sessions: [SessionSummary],
        existing: [SessionSummary] = [],
        hysteresis: TimeInterval = SessionOrdering.defaultHysteresis
    ) -> [SessionSummary] {
        activeSessions(from: sessions, existing: existing, hysteresis: hysteresis)
    }

    /// Filter state applied to inbox and finished lists.
    public var filter = FleetFilterState()

    /// Inbox sessions matching current filters.
    public var filteredInboxSessions: [SessionSummary] {
        filter.filter(inboxSessions)
    }

    /// Finished sessions matching current filters.
    public var filteredFinished: [SessionSummary] {
        filter.filter(finished)
    }

    /// Bird's-eye overview of active sessions across hosts and projects.
    public var overview: FleetOverviewPresentation {
        FleetOverviewPresentation(snapshot: snapshot)
    }

    public var finished: [SessionSummary] {
        SessionBuckets(sessions: snapshot?.sessions ?? []).finished
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
            guard $0.hostRetiredAt == nil else { return false }
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

    /// Coalesce concurrent refreshes onto one request.
    ///
    /// The store is `@MainActor`, but `refresh()` suspends at the network call,
    /// so the poll loop, pull-to-refresh, a Retry tap and a scene-phase change
    /// can each start their own while one is already running. Every extra one is
    /// load on a hub that is, by hypothesis, already too slow to answer -- and
    /// when they supersede each other they land as cancellations, which is what
    /// drives the fast-retry path below. The hub logged 51 cancelled
    /// `/harness/hosts` requests in a single second this way (drover#331).
    ///
    /// Joining the in-flight request gives every caller the same answer it would
    /// have got, without asking for it again.
    private var inFlightRefresh: Task<Void, Never>?

    public func refresh() async {
        if let existing = inFlightRefresh {
            await existing.value
            return
        }
        let task = Task { [weak self] in
            guard let self else { return }
            await self.performRefresh()
        }
        inFlightRefresh = task
        await task.value
        inFlightRefresh = nil
    }

    public private(set) var busyUntil: Date?

    private func performRefresh() async {
        guard let client else { return }
        refreshAttempts += 1
        do {
            var fresh = try await client.snapshot()
            for index in fresh.sessions.indices where optimisticEnded[fresh.sessions[index].id] != nil {
                let session = fresh.sessions[index]
                if ["completed", "terminated", "errored", "failed"].contains(session.status) {
                    optimisticEnded.removeValue(forKey: session.id)
                } else {
                    fresh.sessions[index].status = "terminated"
                    fresh.sessions[index].awaiting = nil
                }
            }
            snapshot = fresh
            busyUntil = nil
            lastError = nil
            lastRefreshFailure = nil
            isReachable = true
            hasLoadedOnce = true
            refreshAttempts = 0
            cancelledFirstLoads = 0
            fastRetryStartedAt = nil
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
            busyUntil = nil
            if case DroverError.busy(let deadline) = error { busyUntil = deadline }
            cancelledFirstLoads = 0
            fastRetryStartedAt = nil
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
        if cancelledFirstLoads == 0 { fastRetryStartedAt = Date() }
        cancelledFirstLoads += 1
        guard cancelledFirstLoads >= Self.cancelledFirstLoadLimit else { return }
        isReachable = false
        lastRefreshFailure = .cancellation
        lastError = "The first load kept being interrupted before it landed · the hub may be busy."
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
        guard let session = snapshot?.sessions.first(where: { $0.id == sessionID }),
              let host = snapshot?.hosts.first(where: { $0.id == session.hostID }),
              host.offer(named: targetHarness ?? session.harness)?.launchMode == .structured else {
            lastError = HarnessCapabilityCopy.noLaunchableHarness
            lastRefreshFailure = nil
            return nil
        }
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
                lastError = "Couldn't start handoff · is the host online?"
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
        if let busyUntil { return max(seconds, busyUntil.timeIntervalSinceNow) }
        guard !hasLoadedOnce, cancelledFirstLoads > 0, lastError == nil else { return seconds }
        // Bounded by the clock, not just by a run of cancellations. The counter
        // resets on any honest failure, so a hub that alternates between timing
        // out and cancelling never reaches the limit that would slow this down,
        // and the phone keeps asking four times a second for as long as it is
        // held open. That is load the hub cannot afford from the one client
        // waiting on it (drover#331).
        guard let since = fastRetryStartedAt,
            Date().timeIntervalSince(since) < Self.fastRetryWindow
        else { return seconds }
        return min(Self.cancelledFirstLoadRetry, seconds)
    }

    /// When the current run of quick retries began, or nil if none is running.
    private var fastRetryStartedAt: Date?

    /// How long quick retries may continue before the ordinary cadence resumes.
    ///
    /// Long enough to cover the launch-time churn this exists for (#85) and far
    /// short of the point where a struggling hub is being polled into the
    /// ground.
    private static let fastRetryWindow: TimeInterval = 5

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
        let tierL = SessionOrdering.attentionTier(lhs)
        let tierR = SessionOrdering.attentionTier(rhs)
        if tierL != tierR { return tierL < tierR }
        if let ordered = activityDateOrdering(lhs, rhs) {
            return ordered
        }
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
            case .httpStatus, .busy:
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
