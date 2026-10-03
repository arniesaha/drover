import Foundation
import Testing
@testable import Drover
@testable import DroverKit

/// A generated fleet served by keyset cursor, like the hub: the cursor is the
/// index of the next row, and every call is recorded.
private actor FakeHistory: HistoryLoading {
    let total: Int
    private(set) var requests: [(filter: HistoryFilter, cursor: String?)] = []
    var failNext: Error?

    init(total: Int) { self.total = total }

    func historyPage(filter: HistoryFilter, cursor: String?, limit: Int) async throws -> HistoryPage {
        requests.append((filter, cursor))
        if let error = failNext {
            failNext = nil
            throw error
        }
        let matching = filter.query.isEmpty ? total : min(total, 7)
        let start = cursor.flatMap(Int.init) ?? 0
        let end = min(start + limit, matching)
        let items = (start..<end).map { index in
            HistoryItem(
                id: "s\(index)", title: "Session \(index)",
                // Six sessions an hour, newest first: several day sections.
                lastActivity: Date(timeIntervalSince1970: 1_790_000_000 - Double(index) * 600)
            )
        }
        let next = end < matching ? String(end) : nil
        return HistoryPage(items: items, nextCursor: next, hasMore: next != nil)
    }

    func historyFacets() async throws -> HistoryFacets {
        HistoryFacets(hosts: [HistoryHost(id: "old", name: "Old", retired: true)])
    }

    func setFailure(_ error: Error) { failNext = error }
}

@MainActor
struct HistoryModelTests {
    /// Scroll from the top to the very end of 1,000 sessions and back. Full
    /// rows in memory never exceed the window, every session is reached once
    /// in order, and rows scrolled back into view are refetched by cursor.
    @Test func memoryWindowStaysBoundedAcrossAThousandRows() async throws {
        let fake = FakeHistory(total: 1000)
        let model = HistoryModel(loader: fake)
        await model.start()
        #expect(model.pager.rowCount == 30)
        #expect(model.facets.hosts.first?.retired == true)

        var position = 0
        var peakResident = 0
        while position < model.pager.rowCount {
            let row = model.pager.rows[position]
            await model.rowAppeared(row)
            #expect(model.pager.rows[position].item != nil, "visible row \(position) must be resident")
            peakResident = max(peakResident, model.pager.residentRowCount)
            position += 1
        }
        #expect(model.pager.rowCount == 1000)
        #expect(!model.pager.hasMore)
        #expect(model.pager.rows.map(\.id) == (0..<1000).map { "s\($0)" })
        #expect(peakResident <= HistoryPager.maxResidentRows)
        #expect(model.pager.rows[0].item == nil, "the top is far away: evicted")

        // Scroll back to the top: the skeleton refetches page 0 by its cursor.
        let before = await fake.requests.count
        for position in stride(from: 999, through: 0, by: -1) {
            await model.rowAppeared(model.pager.rows[position])
            #expect(model.pager.residentRowCount <= HistoryPager.maxResidentRows)
        }
        #expect(model.pager.rows[0].item?.title == "Session 0")
        let refills = await fake.requests.dropFirst(before).map(\.cursor)
        #expect(refills.contains(nil))  // page 0's own cursor
        #expect(Set(refills).count == refills.count, "each evicted page refetched once")
        #expect(model.sections.count > 1)
        #expect(model.sections.flatMap(\.rows).count == 1000)
    }

    @Test func prefetchesFiveRowsFromTheEnd() async throws {
        let fake = FakeHistory(total: 100)
        let model = HistoryModel(loader: fake)
        await model.start()
        await model.rowAppeared(model.pager.rows[24])
        #expect(model.pager.rowCount == 30)
        await model.rowAppeared(model.pager.rows[25])
        #expect(model.pager.rowCount == 60)
        #expect(await fake.requests.map(\.cursor) == [nil, "30"])
    }

    @Test func searchIsDebouncedIntoOneRequest() async throws {
        let fake = FakeHistory(total: 100)
        let model = HistoryModel(loader: fake, searchDebounce: .milliseconds(40))
        await model.start()
        model.searchText = "p"
        model.searchText = "pa"
        model.searchText = "pager"
        try await Task.sleep(for: .milliseconds(250))
        let queries = await fake.requests.map(\.filter.query)
        #expect(queries == ["", "pager"])
        #expect(model.pager.rowCount == 7 && !model.pager.hasMore)
    }

    @Test func filterChangeRestartsFromTheTop() async throws {
        let fake = FakeHistory(total: 100)
        let model = HistoryModel(loader: fake)
        await model.start()
        await model.rowAppeared(model.pager.rows[29])
        await model.apply(HistoryFilter(states: [.failed]))
        #expect(model.pager.rowCount == 30)
        let last = await fake.requests.last
        #expect(last?.cursor == nil && last?.filter.states == [.failed])
    }

    @Test func busyHubIsReportedAndTheSamePageRetried() async throws {
        let fake = FakeHistory(total: 100)
        await fake.setFailure(DroverError.busy(until: Date().addingTimeInterval(0.05)))
        let model = HistoryModel(loader: fake)
        await model.start()
        #expect(model.busyUntil != nil && model.pager.rows.isEmpty)
        try await Task.sleep(for: .milliseconds(400))
        #expect(model.busyUntil == nil && model.pager.rowCount == 30)
        #expect(await fake.requests.map(\.cursor) == [nil, nil])
    }

    @Test func unsupportedHubSaysWhy() async throws {
        let fake = FakeHistory(total: 1)
        await fake.setFailure(DroverError.httpStatus(501, "nope"))
        let model = HistoryModel(loader: fake)
        await model.start()
        #expect(model.errorMessage == "History needs the PostgreSQL control store on this hub.")
    }

    @Test func dayTitlesNameRecentDays() {
        let calendar = Calendar(identifier: .gregorian)
        let now = Date(timeIntervalSince1970: 1_790_000_000)
        #expect(HistoryView.dayTitle(calendar.startOfDay(for: now), now: now, calendar: calendar) == "Today")
        let yesterday = calendar.date(byAdding: .day, value: -1, to: now)!
        #expect(HistoryView.dayTitle(yesterday, now: now, calendar: calendar) == "Yesterday")
        #expect(HistoryView.dayTitle(.distantPast, now: now, calendar: calendar) == "Unknown date")
    }
}

private actor FakeTranscript: TranscriptLoading {
    private(set) var requests: [MessagePageRequest] = []

    func messagePage(sessionID: String, request: MessagePageRequest) async throws -> MessagePage {
        requests.append(request)
        let upper: Int
        switch request {
        case .newest: upper = 1001
        case .older(let beforeSeq, _): upper = beforeSeq
        case .newer: upper = 0
        }
        let lower = max(1, upper - 200)
        let messages = (lower..<upper).map { seq in
            #"{"event_id": "e\#(seq)", "seq": \#(seq), "type": "assistant_output", "role": "assistant", "text": "m\#(seq)"}"#
        }
        let body = #"{"messages": [\#(messages.joined(separator: ","))], "page_min_seq": \#(lower), "page_max_seq": \#(upper - 1), "max_seq": 1000, "has_older": \#(lower > 1), "has_newer": false}"#
        return try MessagePage.decode(from: Data(body.utf8))
    }
}

@MainActor
struct HistoryTranscriptModelTests {
    @Test func pagesBackwardTwoHundredAtATimeUpToTheCap() async throws {
        let fake = FakeTranscript()
        let model = HistoryTranscriptModel(sessionID: "s1", loader: fake)
        await model.loadNewest()
        #expect(model.messages.count == 200 && model.messages.first?.seq == 801)
        #expect(model.canLoadOlder)
        for _ in 0..<10 { await model.loadOlder() }
        #expect(model.messages.count == HistoryTranscriptModel.maxMessages)
        #expect(model.isCapped && !model.canLoadOlder)
        #expect(model.messages.map(\.seq) == Array(1...1000))
        let requests = await fake.requests
        #expect(requests.first == .newest(limit: 200))
        #expect(requests.dropFirst().allSatisfy {
            if case .older(_, let limit) = $0 { return limit == 200 } else { return false }
        })
        #expect(requests.count == 5)
    }
}
