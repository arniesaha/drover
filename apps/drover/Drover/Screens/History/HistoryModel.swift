import Foundation
import Observation
import DroverKit

/// What `HistoryModel` needs from the hub. `DroverClient` is the real one;
/// tests substitute a generated fleet.
protocol HistoryLoading: Sendable {
    func historyPage(filter: HistoryFilter, cursor: String?, limit: Int) async throws -> HistoryPage
    func historyFacets() async throws -> HistoryFacets
}

extension DroverClient: HistoryLoading {}

/// One day of history rows, newest day first.
struct HistorySection: Identifiable, Equatable {
    let day: Date
    let rows: [HistoryPager.Row]
    var id: Date { day }
}

/// Session history for the History screen: cursor paging with a bounded
/// memory window (docs/design/session-history.md).
///
/// The list asks for the next page when a row within
/// `HistoryPager.prefetchDistance` of the end appears. At most
/// `HistoryPager.maxResidentRows` full rows are held; pages far from the row
/// on screen are reduced to skeletons (id + day) so the list keeps its
/// geometry, and a skeleton that scrolls back into view refetches its page by
/// the cursor it was first fetched with.
@MainActor
@Observable
final class HistoryModel {
    private(set) var pager: HistoryPager
    private(set) var facets = HistoryFacets()
    private(set) var filter = HistoryFilter()
    private(set) var hasLoadedOnce = false
    private(set) var errorMessage: String?
    private(set) var busyUntil: Date?

    /// Bound to `.searchable`; applied to the filter after a debounce.
    var searchText = "" {
        didSet {
            guard searchText != oldValue else { return }
            scheduleSearch()
        }
    }

    @ObservationIgnored private let loader: any HistoryLoading
    @ObservationIgnored private let searchDebounce: Duration
    @ObservationIgnored private var searchTask: Task<Void, Never>?
    @ObservationIgnored private var retryTask: Task<Void, Never>?
    @ObservationIgnored private let calendar: Calendar

    init(
        loader: any HistoryLoading,
        pager: HistoryPager = HistoryPager(),
        searchDebounce: Duration = .milliseconds(300),
        calendar: Calendar = .current
    ) {
        self.loader = loader
        self.pager = pager
        self.searchDebounce = searchDebounce
        self.calendar = calendar
    }

    var isLoading: Bool { pager.isLoading }
    var isEmpty: Bool { hasLoadedOnce && pager.isEmpty && !pager.hasMore }

    /// Rows grouped under day headers. Skeleton rows keep their day, so a
    /// section never jumps when its page is evicted or refilled.
    var sections: [HistorySection] {
        var out: [HistorySection] = []
        var currentDay: Date?
        var bucket: [HistoryPager.Row] = []
        for row in pager.rows {
            let day = calendar.startOfDay(for: row.lastActivity ?? .distantPast)
            if day != currentDay {
                if let currentDay { out.append(HistorySection(day: currentDay, rows: bucket)) }
                currentDay = day
                bucket = []
            }
            bucket.append(row)
        }
        if let currentDay { out.append(HistorySection(day: currentDay, rows: bucket)) }
        return out
    }

    // MARK: Lifecycle

    func start() async {
        guard !hasLoadedOnce, pager.rows.isEmpty else { return }
        await loadNextPage()
        await loadFacets()
    }

    /// Pull to refresh: start again from the newest session.
    func refresh() async {
        pager.reset()
        errorMessage = nil
        await loadNextPage()
        await loadFacets()
    }

    func apply(_ newFilter: HistoryFilter) async {
        var next = newFilter
        next.query = searchText
        guard next != filter else { return }
        filter = next
        pager.reset()
        errorMessage = nil
        await loadNextPage()
    }

    /// A list row appeared: keep the window around it, refetch it if it is a
    /// skeleton, and prefetch once the end is near.
    func rowAppeared(_ row: HistoryPager.Row) async {
        pager.evict(around: row.pageIndex)
        if row.item == nil, let request = pager.refillRequest(for: row.pageIndex) {
            await perform(request)
            pager.evict(around: row.pageIndex)
        }
        if pager.shouldPrefetch(afterAppearanceOf: row.position) {
            await loadNextPage()
            // The new page counts against the window as soon as it lands.
            pager.evict(around: row.pageIndex)
        }
    }

    func loadNextPage() async {
        guard let request = pager.nextPageRequest() else { return }
        await perform(request)
    }

    func retry() async {
        errorMessage = nil
        await loadNextPage()
    }

    // MARK: Private

    private func perform(_ request: HistoryPager.Request) async {
        let requestFilter = filter
        do {
            let page = try await loader.historyPage(
                filter: requestFilter, cursor: request.cursor, limit: pager.pageSize
            )
            if pager.apply(page, for: request) {
                hasLoadedOnce = true
                errorMessage = nil
                busyUntil = nil
            }
        } catch DroverError.busy(let deadline) {
            pager.fail(request)
            busyUntil = deadline
            scheduleRetry(at: deadline)
        } catch {
            pager.fail(request)
            guard request.generation == pager.generation else { return }
            errorMessage = Self.message(for: error)
        }
    }

    private func loadFacets() async {
        if let loaded = try? await loader.historyFacets() { facets = loaded }
    }

    private func scheduleSearch() {
        searchTask?.cancel()
        let text = searchText
        let debounce = searchDebounce
        searchTask = Task { [weak self] in
            try? await Task.sleep(for: debounce)
            guard !Task.isCancelled, let self else { return }
            var next = self.filter
            next.query = text
            guard next != self.filter else { return }
            self.filter = next
            self.pager.reset()
            self.errorMessage = nil
            await self.loadNextPage()
        }
    }

    private func scheduleRetry(at deadline: Date) {
        retryTask?.cancel()
        retryTask = Task { [weak self] in
            try? await RetryPolicy.wait(until: deadline)
            guard !Task.isCancelled, let self else { return }
            self.busyUntil = nil
            await self.loadNextPage()
        }
    }

    static func message(for error: Error) -> String {
        if case DroverError.httpStatus(501, _) = error {
            return "History needs the PostgreSQL control store on this hub."
        }
        if let drover = error as? DroverError {
            return drover.localizedDescription(isTailscale: false)
        }
        return error.localizedDescription
    }
}

/// A read-only transcript for a history row, paged by sequence from the
/// existing messages endpoint: newest page first, then older pages on demand.
protocol TranscriptLoading: Sendable {
    func messagePage(sessionID: String, request: MessagePageRequest) async throws -> MessagePage
}

extension DroverClient: TranscriptLoading {}

@MainActor
@Observable
final class HistoryTranscriptModel {
    static let pageSize = 200
    static let maxMessages = 1000

    let sessionID: String
    private(set) var messages: [HarnessMessage] = []
    private(set) var hasOlder = false
    private(set) var isLoading = false
    private(set) var hasLoadedOnce = false
    private(set) var errorMessage: String?
    @ObservationIgnored private var oldestSeq: Int?
    @ObservationIgnored private let loader: any TranscriptLoading

    init(sessionID: String, loader: any TranscriptLoading) {
        self.sessionID = sessionID
        self.loader = loader
    }

    /// Past this many messages the screen points at the live session instead
    /// of growing without bound.
    var isCapped: Bool { messages.count >= Self.maxMessages }
    var canLoadOlder: Bool { hasOlder && !isCapped && !isLoading }

    func loadNewest() async {
        guard !hasLoadedOnce, !isLoading else { return }
        await load(.newest(limit: Self.pageSize))
    }

    func loadOlder() async {
        guard canLoadOlder, let oldestSeq else { return }
        await load(.older(beforeSeq: oldestSeq, limit: Self.pageSize))
    }

    private func load(_ request: MessagePageRequest) async {
        isLoading = true
        defer { isLoading = false }
        do {
            let page = try await loader.messagePage(sessionID: sessionID, request: request)
            let known = Set(messages.map(\.id))
            messages = page.messages.filter { !known.contains($0.id) } + messages
            oldestSeq = page.pageMinSeq ?? page.messages.first?.seq ?? oldestSeq
            hasOlder = page.hasOlder
            hasLoadedOnce = true
            errorMessage = nil
        } catch {
            errorMessage = HistoryModel.message(for: error)
        }
    }
}
