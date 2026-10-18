import Foundation

/// Deterministic, jitter-tolerant session sorting.
///
/// Problem:
/// Running coding sessions emit frequent activity updates (heartbeats, streamed tokens),
/// causing raw timestamps to jitter by seconds on every poll. If the inbox list sorts
/// strictly by raw timestamp recency across all sessions, rows constantly swap positions
/// on every refresh, causing cards and status icons to fly past each other.
///
/// Solution:
/// 1. State tier hierarchy:
///    - Needs-you first: `needsApproval` (Tier 0), then `needsInput` (Tier 1)
///    - Running next: `working` (Tier 2)
///    - Finished last: `done`, `errored` (Tier 3)
///    Any state transition between tiers immediately re-sorts without delay.
///
/// 2. Hysteresis on activity recency within a tier:
///    Within the same tier, order by activity date. To prevent rapid swapping from
///    sub-minute timestamp jitter, an existing order is preserved unless a session's
///    activity timestamp moves by more than the hysteresis window (default 30 seconds).
///
/// 3. Deterministic tie-breaking:
///    Ties are broken by preserving previous relative ordering, and finally by `session.id`.
public enum SessionOrdering {
    public static let defaultHysteresis: TimeInterval = 30.0

    /// Returns the priority tier for a session's attention state.
    /// Lower number = higher priority (placed closer to top of the inbox).
    public static func attentionTier(_ session: SessionSummary) -> Int {
        switch session.attention {
        case .needsApproval: return 0
        case .needsInput: return 1
        case .working: return 2
        case .done, .errored: return 3
        }
    }

    /// Sorts sessions deterministically with hysteresis against an existing order.
    ///
    /// - Parameters:
    ///   - sessions: The candidate sessions to sort.
    ///   - existing: The previous ordered list of sessions, if any.
    ///   - hysteresis: Time window in seconds within which timestamp changes are
    ///     treated as jitter rather than intentional reordering.
    /// - Returns: Stably sorted list of sessions.
    public static func sort(
        _ sessions: [SessionSummary],
        existing: [SessionSummary] = [],
        hysteresis: TimeInterval = defaultHysteresis
    ) -> [SessionSummary] {
        guard sessions.count > 1 else { return sessions }

        let previousIndex: [String: Int] = Dictionary(
            uniqueKeysWithValues: existing.enumerated().map { ($0.element.id, $0.offset) }
        )
        let previousDates: [String: Date] = Dictionary(
            existing.compactMap { s in s.activityDate.map { (s.id, $0) } },
            uniquingKeysWith: { first, _ in first }
        )

        // Compute effective activity date with hysteresis.
        // If a session existed previously and its new timestamp differs by <= hysteresis,
        // keep the previous timestamp so its relative sort position is undisturbed.
        var effectiveDates: [String: Date] = [:]
        for session in sessions {
            guard let newDate = session.activityDate else { continue }
            if let oldDate = previousDates[session.id],
               abs(newDate.timeIntervalSince(oldDate)) <= hysteresis {
                effectiveDates[session.id] = oldDate
            } else {
                effectiveDates[session.id] = newDate
            }
        }

        return sessions.sorted { lhs, rhs in
            let tierL = attentionTier(lhs)
            let tierR = attentionTier(rhs)
            if tierL != tierR {
                return tierL < tierR
            }

            // Same tier: compare effective activity dates (newest first)
            let dateL = effectiveDates[lhs.id]
            let dateR = effectiveDates[rhs.id]

            switch (dateL, dateR) {
            case let (l?, r?) where l != r:
                return l > r
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            default:
                break
            }

            // Tie or equal dates: preserve previous relative ordering if both existed
            if let idxL = previousIndex[lhs.id], let idxR = previousIndex[rhs.id] {
                if idxL != idxR {
                    return idxL < idxR
                }
            } else if previousIndex[lhs.id] != nil {
                return true
            } else if previousIndex[rhs.id] != nil {
                return false
            }

            // Final deterministic tie-breaker: session id
            return lhs.id < rhs.id
        }
    }
}
