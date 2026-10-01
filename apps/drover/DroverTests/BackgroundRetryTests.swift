import DroverKit
import Foundation
import Testing
@testable import Drover

struct BackgroundRetryTests {
    @Test func refreshScheduleHonorsServerDeadline() {
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        #expect(BackgroundRefresh.nextRefreshDate(now: now, retryDeadline: nil) == now.addingTimeInterval(900))
        #expect(BackgroundRefresh.nextRefreshDate(now: now, retryDeadline: now.addingTimeInterval(30)) == now.addingTimeInterval(900))
        #expect(BackgroundRefresh.nextRefreshDate(now: now, retryDeadline: now.addingTimeInterval(3600)) == now.addingTimeInterval(3600))
    }

    @Test func busyCopyIsCalmAndRoundsUp() {
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        #expect(RetryPolicy.busyMessage(until: now.addingTimeInterval(30.1), now: now) == "Hub busy, retrying in 31s")
    }
}
