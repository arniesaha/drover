import Foundation
import Testing
@testable import DroverKit

// Swift Testing macros cannot wrap mutating calls, so every pager call is made
// on its own line and the result checked after.
struct HistoryPagerTests {
    private func items(_ range: Range<Int>) -> [HistoryItem] {
        range.map {
            HistoryItem(id: "s\($0)", title: "t\($0)",
                        lastActivity: Date(timeIntervalSince1970: 2_000_000_000 - Double($0) * 60))
        }
    }

    private func page(_ range: Range<Int>, next: String?) -> HistoryPage {
        HistoryPage(items: items(range), nextCursor: next, hasMore: next != nil)
    }

    @Test func appendsPagesInCursorOrderWithoutDuplicateRequests() throws {
        var pager = HistoryPager()
        let firstRequest = pager.nextPageRequest()
        let first = try #require(firstRequest)
        #expect(first.cursor == nil && first.pageIndex == 0)
        let duplicate = pager.nextPageRequest()
        #expect(duplicate == nil)  // already in flight
        let appliedFirst = pager.apply(page(0..<30, next: "c1"), for: first)
        #expect(appliedFirst)
        let secondRequest = pager.nextPageRequest()
        let second = try #require(secondRequest)
        #expect(second.cursor == "c1" && second.pageIndex == 1)
        // A session that moved above the cursor shows up again: dropped.
        let overlap = HistoryPage(items: items(29..<40), nextCursor: nil, hasMore: false)
        let appliedSecond = pager.apply(overlap, for: second)
        #expect(appliedSecond)
        #expect(pager.rows.map(\.id) == (0..<40).map { "s\($0)" })
        #expect(pager.rows.map(\.position) == Array(0..<40))
        let afterEnd = pager.nextPageRequest()
        #expect(!pager.hasMore && afterEnd == nil)
    }

    @Test func staleAnswersAreIgnoredAfterReset() throws {
        var pager = HistoryPager()
        let oldRequest = pager.nextPageRequest()
        let old = try #require(oldRequest)
        pager.reset()
        let applied = pager.apply(page(0..<30, next: "c1"), for: old)
        #expect(!applied)
        #expect(pager.rows.isEmpty)
        let freshRequest = pager.nextPageRequest()
        let fresh = try #require(freshRequest)
        #expect(fresh.generation == old.generation + 1)
    }

    @Test func failureAllowsTheSamePageAgain() throws {
        var pager = HistoryPager()
        let firstRequest = pager.nextPageRequest()
        let request = try #require(firstRequest)
        pager.fail(request)
        let again = pager.nextPageRequest()
        #expect(again == request)
    }

    @Test func prefetchesWithinFiveRowsOfTheEnd() throws {
        var pager = HistoryPager()
        let firstRequest = pager.nextPageRequest()
        pager.apply(page(0..<30, next: "c1"), for: try #require(firstRequest))
        #expect(!pager.shouldPrefetch(afterAppearanceOf: 24))
        #expect(pager.shouldPrefetch(afterAppearanceOf: 25))
    }

    @Test func evictsFarPagesToSkeletonsAndRefillsByTheirCursor() throws {
        var pager = HistoryPager(maxResidentRows: 90, pageSize: 30)
        for index in 0..<6 {
            let request = pager.nextPageRequest()
            pager.apply(page(index * 30..<(index + 1) * 30, next: "c\(index + 1)"),
                        for: try #require(request))
            pager.evict(around: index)
            #expect(pager.residentRowCount <= 90)
        }
        // Geometry is preserved: every row still exists, far ones as skeletons.
        #expect(pager.rowCount == 180)
        #expect(pager.pages[0].items == nil && pager.pages[5].items != nil)
        #expect(pager.rows[0].item == nil && pager.rows[0].lastActivity != nil)

        // Scrolling back to the top refetches page 0 with its original cursor.
        let refillRequest = pager.refillRequest(for: 0)
        let refill = try #require(refillRequest)
        #expect(refill.cursor == nil && refill.pageIndex == 0)
        let duplicate = pager.refillRequest(for: 0)
        #expect(duplicate == nil)  // in flight
        pager.apply(page(0..<30, next: "c1"), for: refill)
        pager.evict(around: 0)
        #expect(pager.pages[0].items?.count == 30)
        #expect(pager.residentRowCount <= 90)
        #expect(pager.pages[5].items == nil)  // now the far end is evicted
        // The cursor chain is intact: every page refetches where it was.
        let middleRequest = pager.refillRequest(for: 2)
        #expect(try #require(middleRequest).cursor == "c2")
        #expect(pager.pages[0].nextCursor == "c1")
    }
}
