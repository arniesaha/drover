import Foundation
import Observation

/// Observable façade over a `NexusClient` snapshot: buckets sessions by what
/// the user needs to do about them, polls on an interval, and never loses
/// the last-known-good snapshot just because a refresh failed.
@MainActor
@Observable
public final class SessionStore {
    private let client: NexusClient?

    public private(set) var snapshot: HarnessSnapshot?
    public private(set) var lastError: String?
    public private(set) var isReachable: Bool = false

    // `nonisolated(unsafe)` solely so `deinit` (nonisolated in Swift 6) can
    // cancel it; every other access is from `@MainActor` methods, and deinit
    // runs with exclusive access to the dying object, so there is no race.
    private nonisolated(unsafe) var pollingTask: Task<Void, Never>?

    public init(client: NexusClient? = nil) {
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

    public var working: [SessionSummary] {
        (snapshot?.sessions ?? [])
            .filter { $0.attention == .working }
            .sorted(by: Self.byLastActivityDescending)
    }

    public var finished: [SessionSummary] {
        (snapshot?.sessions ?? [])
            .filter { $0.attention == .done || $0.attention == .errored }
            .sorted(by: Self.byLastActivityDescending)
    }

    // MARK: - Refresh

    public func refresh() async {
        guard let client else { return }
        do {
            let fresh = try await client.snapshot()
            snapshot = fresh
            lastError = nil
            isReachable = true
        } catch {
            isReachable = false
            lastError = Self.errorMessage(for: error)
            // Deliberately keep the cached `snapshot` as-is.
        }
    }

    // MARK: - Handoff

    /// Server-side handoff from the session list: returns the new session's
    /// id, or nil on failure with the explanation routed through `lastError`
    /// (the same banner refresh failures use). 409/400 carry a server-authored
    /// message surfaced verbatim; anything else gets a generic hint.
    public func continueSession(_ sessionID: String) async -> String? {
        guard let client else { return nil }
        do {
            let newSessionID = try await client.continueSession(sessionID: sessionID)
            lastError = nil
            return newSessionID
        } catch {
            switch error {
            case NexusError.conflict(let message), NexusError.badRequest(let message):
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

    private static func needsYouOrdering(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool {
        if lhs.attention != rhs.attention {
            return lhs.attention == .needsApproval
        }
        return byLastActivityDescending(lhs, rhs)
    }

    private static func byLastActivityDescending(_ lhs: SessionSummary, _ rhs: SessionSummary) -> Bool {
        switch (lhs.lastActivity, rhs.lastActivity) {
        case let (l?, r?): return l > r
        case (nil, nil): return false
        case (nil, _): return false
        case (_, nil): return true
        }
    }

    private static func errorMessage(for error: Error) -> String {
        if let nexusError = error as? NexusError, nexusError == .unauthorized {
            return "token rejected — check Settings"
        }
        return "\(error)"
    }
}
