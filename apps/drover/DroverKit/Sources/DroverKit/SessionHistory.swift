import Foundation

// Session history across hosts: `GET /sessions/history` (keyset-paged) and
// `GET /sessions/history/facets`. Contract and limits:
// docs/design/session-history.md.

// MARK: - Wire models

public enum HistoryState: String, Sendable, CaseIterable, Hashable, Codable {
    case running, awaiting, finished, failed

    public var label: String { rawValue.capitalized }
}

public struct HistoryHost: Sendable, Hashable, Decodable, Identifiable {
    public let id: String
    public let name: String
    /// Retired, or no longer known to the hub. Its history stays readable.
    public let retired: Bool

    public init(id: String, name: String, retired: Bool = false) {
        self.id = id
        self.name = name
        self.retired = retired
    }

    private enum CodingKeys: String, CodingKey { case id, name, retired }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? container.decode(String.self, forKey: .id)) ?? ""
        name = (try? container.decode(String.self, forKey: .name)) ?? id
        retired = (try? container.decode(Bool.self, forKey: .retired)) ?? false
    }
}

public struct HistoryTokens: Sendable, Equatable, Decodable {
    public let input: Int?
    public let output: Int?
    public let cacheRead: Int?
    public let cacheWrite: Int?

    public var total: Int { (input ?? 0) + (output ?? 0) }

    private enum CodingKeys: String, CodingKey {
        case input, output
        case cacheRead = "cache_read"
        case cacheWrite = "cache_write"
    }
}

/// One history row. A fixed, small shape: no transcript, no events.
public struct HistoryItem: Sendable, Identifiable, Equatable, Decodable {
    public let id: String
    public let title: String
    public let harness: String
    public let model: String?
    public let host: HistoryHost
    public let repo: String?
    public let branch: String?
    public let state: HistoryState
    public let status: String?
    public let startedAt: Date?
    public let endedAt: Date?
    public let lastActivity: Date?
    public let summary: String?
    public let hasTranscript: Bool
    public let tokens: HistoryTokens?

    public init(
        id: String, title: String, harness: String = "agent", model: String? = nil,
        host: HistoryHost = HistoryHost(id: "host", name: "Host"),
        repo: String? = nil, branch: String? = nil, state: HistoryState = .finished,
        status: String? = nil, startedAt: Date? = nil, endedAt: Date? = nil,
        lastActivity: Date?, summary: String? = nil, hasTranscript: Bool = true,
        tokens: HistoryTokens? = nil
    ) {
        self.id = id
        self.title = title
        self.harness = harness
        self.model = model
        self.host = host
        self.repo = repo
        self.branch = branch
        self.state = state
        self.status = status
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.lastActivity = lastActivity
        self.summary = summary
        self.hasTranscript = hasTranscript
        self.tokens = tokens
    }

    private enum CodingKeys: String, CodingKey {
        case id, title, harness, model, host, repo, branch, state, status, summary, tokens
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case lastActivity = "last_activity"
        case hasTranscript = "has_transcript"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        harness = (try? container.decode(String.self, forKey: .harness)) ?? "agent"
        title = (try? container.decode(String.self, forKey: .title)) ?? "\(harness) session"
        model = try? container.decode(String.self, forKey: .model)
        host = (try? container.decode(HistoryHost.self, forKey: .host))
            ?? HistoryHost(id: "", name: "Unknown host", retired: true)
        repo = try? container.decode(String.self, forKey: .repo)
        branch = try? container.decode(String.self, forKey: .branch)
        // A state this build does not know is still a live session.
        let rawState = (try? container.decode(String.self, forKey: .state)) ?? ""
        state = HistoryState(rawValue: rawState) ?? .running
        status = try? container.decode(String.self, forKey: .status)
        startedAt = WireDate.parse(try? container.decode(String.self, forKey: .startedAt))
        endedAt = WireDate.parse(try? container.decode(String.self, forKey: .endedAt))
        lastActivity = WireDate.parse(try? container.decode(String.self, forKey: .lastActivity))
        summary = try? container.decode(String.self, forKey: .summary)
        hasTranscript = (try? container.decode(Bool.self, forKey: .hasTranscript)) ?? false
        tokens = try? container.decode(HistoryTokens.self, forKey: .tokens)
    }
}

public struct HistoryPage: Sendable, Equatable, Decodable {
    public let items: [HistoryItem]
    public let nextCursor: String?
    public let hasMore: Bool
    public let pageSize: Int
    public let truncated: Bool
    /// Rows the decoder had to skip. The page still advances by its cursor.
    public let skippedRows: Int

    public init(items: [HistoryItem], nextCursor: String?, hasMore: Bool,
                pageSize: Int = HistoryPager.pageSize, truncated: Bool = false) {
        self.items = items
        self.nextCursor = nextCursor
        self.hasMore = hasMore
        self.pageSize = pageSize
        self.truncated = truncated
        self.skippedRows = 0
    }

    private enum CodingKeys: String, CodingKey {
        case items, truncated
        case nextCursor = "next_cursor"
        case hasMore = "has_more"
        case pageSize = "page_size"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let wrapped = (try? container.decode([LenientHistoryItem].self, forKey: .items)) ?? []
        items = wrapped.compactMap(\.value)
        skippedRows = wrapped.count - items.count
        nextCursor = try? container.decode(String.self, forKey: .nextCursor)
        // No cursor means no way forward, whatever the flag says.
        hasMore = ((try? container.decode(Bool.self, forKey: .hasMore)) ?? false) && nextCursor != nil
        pageSize = (try? container.decode(Int.self, forKey: .pageSize)) ?? HistoryPager.pageSize
        truncated = (try? container.decode(Bool.self, forKey: .truncated)) ?? false
    }
}

private struct LenientHistoryItem: Decodable {
    let value: HistoryItem?
    init(from decoder: Decoder) throws {
        value = try? decoder.singleValueContainer().decode(HistoryItem.self)
    }
}

public struct HistoryFacets: Sendable, Equatable, Decodable {
    public let hosts: [HistoryHost]
    public let harnesses: [String]
    public let repos: [String]

    public init(hosts: [HistoryHost] = [], harnesses: [String] = [], repos: [String] = []) {
        self.hosts = hosts
        self.harnesses = harnesses
        self.repos = repos
    }

    private enum CodingKeys: String, CodingKey { case hosts, harnesses, repos }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hosts = (try? container.decode([HistoryHost].self, forKey: .hosts)) ?? []
        harnesses = (try? container.decode([String].self, forKey: .harnesses)) ?? []
        repos = (try? container.decode([String].self, forKey: .repos)) ?? []
    }
}

// MARK: - Filter

public struct HistoryFilter: Sendable, Hashable {
    public var hosts: Set<String> = []
    public var harnesses: Set<String> = []
    public var repos: Set<String> = []
    public var states: Set<HistoryState> = []
    public var since: Date?
    public var until: Date?
    public var query: String = ""

    public init(
        hosts: Set<String> = [], harnesses: Set<String> = [], repos: Set<String> = [],
        states: Set<HistoryState> = [], since: Date? = nil, until: Date? = nil,
        query: String = ""
    ) {
        self.hosts = hosts
        self.harnesses = harnesses
        self.repos = repos
        self.states = states
        self.since = since
        self.until = until
        self.query = query
    }

    /// Facet filters only; the search text is shown separately.
    public var activeCount: Int {
        hosts.count + harnesses.count + repos.count + states.count
            + (since == nil ? 0 : 1) + (until == nil ? 0 : 1)
    }

    public var isEmpty: Bool {
        activeCount == 0 && query.trimmingCharacters(in: .whitespaces).isEmpty
    }

    nonisolated(unsafe) private static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    /// Query items in a stable order, so the same filter is always the same
    /// URL (and the server's cursor fingerprint matches).
    public var queryItems: [(String, String?)] {
        var items: [(String, String?)] = []
        items += hosts.sorted().map { ("host", $0) }
        items += harnesses.sorted().map { ("harness", $0) }
        items += repos.sorted().map { ("repo", $0) }
        items += states.map(\.rawValue).sorted().map { ("state", $0) }
        if let since { items.append(("since", Self.iso.string(from: since))) }
        if let until { items.append(("until", Self.iso.string(from: until))) }
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { items.append(("q", String(trimmed.prefix(200)))) }
        return items
    }
}

// MARK: - Pager

/// The minimum a row needs to keep its place in the list after its page is
/// evicted: identity and the day section it sits under.
public struct HistorySkeleton: Sendable, Equatable {
    public let id: String
    public let lastActivity: Date?
}

/// Keyset pages plus a bounded memory window.
///
/// Pages are appended in cursor order. Each remembers the cursor it was
/// fetched with, so a page whose rows were evicted can be fetched again
/// exactly where it was. Evicted pages keep a skeleton (id + day) so the list
/// keeps its geometry and day sections, and full rows stay at or under
/// `maxResidentRows`.
///
/// Pure and synchronous: callers (the app's `HistoryModel`) do the I/O and
/// feed results back with the request they made, so stale answers from an
/// older filter or a superseded fetch are dropped here.
public struct HistoryPager: Sendable, Equatable {
    public static let pageSize = 30
    public static let maxResidentRows = 300
    public static let prefetchDistance = 5

    public struct Request: Sendable, Equatable, Hashable {
        public let generation: Int
        public let pageIndex: Int
        public let cursor: String?
    }

    public struct Page: Sendable, Equatable {
        public let requestCursor: String?
        public var nextCursor: String?
        public var hasMore: Bool
        public var skeleton: [HistorySkeleton]
        /// Nil when evicted.
        public var items: [HistoryItem]?
    }

    /// One list row: resident (has `item`) or a placeholder awaiting refetch.
    public struct Row: Sendable, Equatable, Identifiable {
        public let id: String
        public let pageIndex: Int
        public let position: Int
        public let lastActivity: Date?
        public let item: HistoryItem?
    }

    public private(set) var pages: [Page] = []
    public private(set) var generation = 0
    public private(set) var inFlight: Set<Int> = []
    public let maxResidentRows: Int
    public let pageSize: Int

    public init(maxResidentRows: Int = HistoryPager.maxResidentRows,
                pageSize: Int = HistoryPager.pageSize) {
        self.maxResidentRows = maxResidentRows
        self.pageSize = pageSize
    }

    public var hasMore: Bool { pages.last?.hasMore ?? true }
    public var isEmpty: Bool { pages.allSatisfy { $0.skeleton.isEmpty } }
    public var rowCount: Int { pages.reduce(0) { $0 + $1.skeleton.count } }
    public var residentRowCount: Int { pages.reduce(0) { $0 + ($1.items?.count ?? 0) } }
    public var isLoading: Bool { !inFlight.isEmpty }

    public var rows: [Row] {
        var out: [Row] = []
        out.reserveCapacity(rowCount)
        var position = 0
        for (index, page) in pages.enumerated() {
            for (offset, skeleton) in page.skeleton.enumerated() {
                out.append(Row(
                    id: skeleton.id, pageIndex: index, position: position,
                    lastActivity: skeleton.lastActivity, item: page.items?[offset]
                ))
                position += 1
            }
        }
        return out
    }

    /// Forget everything; answers to earlier requests are ignored from now on.
    public mutating func reset() {
        pages = []
        inFlight = []
        generation += 1
    }

    /// The next page to append, if one is due and not already being fetched.
    public mutating func nextPageRequest() -> Request? {
        guard hasMore else { return nil }
        let index = pages.count
        guard !inFlight.contains(index) else { return nil }
        inFlight.insert(index)
        return Request(generation: generation, pageIndex: index, cursor: pages.last?.nextCursor)
    }

    /// Prefetch once a row within `prefetchDistance` of the end appears.
    public func shouldPrefetch(afterAppearanceOf position: Int) -> Bool {
        hasMore && position >= rowCount - HistoryPager.prefetchDistance
    }

    /// Refetch an evicted page by the cursor it was first fetched with.
    public mutating func refillRequest(for pageIndex: Int) -> Request? {
        guard pages.indices.contains(pageIndex), pages[pageIndex].items == nil,
              !inFlight.contains(pageIndex) else { return nil }
        inFlight.insert(pageIndex)
        return Request(generation: generation, pageIndex: pageIndex,
                       cursor: pages[pageIndex].requestCursor)
    }

    /// The request failed; allow it to be asked for again.
    public mutating func fail(_ request: Request) {
        guard request.generation == generation else { return }
        inFlight.remove(request.pageIndex)
    }

    /// Apply a fetched page. Returns false when the answer is stale.
    @discardableResult
    public mutating func apply(_ page: HistoryPage, for request: Request) -> Bool {
        guard request.generation == generation, inFlight.contains(request.pageIndex) else {
            return false
        }
        inFlight.remove(request.pageIndex)
        // A session that gained activity moves above the cursor and can come
        // back on a later page. It must never render twice.
        var elsewhere = Set<String>()
        for (index, other) in pages.enumerated() where index != request.pageIndex {
            elsewhere.formUnion(other.skeleton.map(\.id))
        }
        let items = page.items.filter { !elsewhere.contains($0.id) }
        let fetched = Page(
            requestCursor: request.cursor,
            nextCursor: page.nextCursor,
            hasMore: page.hasMore,
            skeleton: items.map { HistorySkeleton(id: $0.id, lastActivity: $0.lastActivity) },
            items: items
        )
        if request.pageIndex == pages.count {
            pages.append(fetched)
        } else if pages.indices.contains(request.pageIndex) {
            // A refill keeps the cursor chain it already had: the page after
            // this one was fetched with the old next cursor and stays valid.
            var refilled = fetched
            refilled.nextCursor = pages[request.pageIndex].nextCursor
            refilled.hasMore = pages[request.pageIndex].hasMore
            pages[request.pageIndex] = refilled
        } else {
            return false
        }
        return true
    }

    /// Drop full rows from the pages furthest from `focusPage` until at most
    /// `maxResidentRows` remain. The focus page itself is never evicted.
    public mutating func evict(around focusPage: Int) {
        var resident = residentRowCount
        guard resident > maxResidentRows else { return }
        let candidates = pages.indices
            .filter { $0 != focusPage && pages[$0].items != nil }
            .sorted { abs($0 - focusPage) > abs($1 - focusPage) }
        for index in candidates where resident > maxResidentRows {
            resident -= pages[index].items?.count ?? 0
            pages[index].items = nil
        }
    }
}
