import Foundation

/// The Home body has more than one way to contain no running rows. Keep those
/// states separate so an unavailable refresh or an actionable session never
/// turns into the friendly empty-state artwork.
enum HomeSessionContentState: Equatable {
    case loading
    case error
    case actionRequired
    case filteredEmpty
    case empty
    case running

    static func select(
        hasLoadedOnce: Bool,
        isReachable: Bool,
        hasRunningSessions: Bool,
        hasActionRequiredSessions: Bool,
        hasError: Bool,
        isFiltered: Bool
    ) -> Self {
        guard hasLoadedOnce else { return .loading }
        if hasRunningSessions { return .running }
        // Attention remains actionable even if a later refresh becomes
        // unavailable. Never replace it with an empty or error-only body.
        if hasActionRequiredSessions { return .actionRequired }
        guard isReachable, !hasError else { return .error }
        if isFiltered { return .filteredEmpty }
        return .empty
    }
}
