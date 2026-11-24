import Testing
@testable import Drover

struct HomeSessionContentStateTests {
    @Test(arguments: [
        (false, false, false, false, false, false, HomeSessionContentState.loading),
        (true, false, false, false, false, false, .error),
        (true, true, false, true, false, false, .actionRequired),
        (true, true, false, false, true, true, .error),
        (true, true, false, false, false, true, .filteredEmpty),
        (true, true, false, false, false, false, .empty),
        (true, true, true, false, false, false, .running),
        // Actionable attention wins over an unavailable refresh and the
        // friendly empty state, so the user can still reach Inbox.
        (true, false, false, true, true, true, .actionRequired),
    ])
    func selectsOnlyGenuineEmptyState(
        hasLoadedOnce: Bool,
        isReachable: Bool,
        hasRunningSessions: Bool,
        hasActionRequiredSessions: Bool,
        hasError: Bool,
        isFiltered: Bool,
        expected: HomeSessionContentState
    ) {
        #expect(HomeSessionContentState.select(
            hasLoadedOnce: hasLoadedOnce,
            isReachable: isReachable,
            hasRunningSessions: hasRunningSessions,
            hasActionRequiredSessions: hasActionRequiredSessions,
            hasError: hasError,
            isFiltered: isFiltered
        ) == expected)
    }
}
