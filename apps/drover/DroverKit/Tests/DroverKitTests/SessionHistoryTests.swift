import Foundation
import Testing
@testable import DroverKit

// MARK: - Decoding

struct SessionHistoryDecodingTests {
    private func fixture() throws -> Data {
        let url = try #require(droverKitFixtureURL("session-history-page"))
        return try Data(contentsOf: url)
    }

    @Test func decodesTheWireFixtureLeniently() throws {
        let page = try JSONDecoder().decode(HistoryPage.self, from: fixture())
        #expect(page.items.map(\.id) == ["h-1", "h-2"])
        #expect(page.skippedRows == 1)
        #expect(page.nextCursor == "eyJ2IjoxfQ" && page.hasMore)
        #expect(page.pageSize == 30 && !page.truncated)

        let first = page.items[0]
        #expect(first.title == "Refactor the pager window")
        #expect(first.host == HistoryHost(id: "mac-mini", name: "Studio Mac", retired: false))
        #expect(first.repo == "arniesaha/drover" && first.branch == "feat/session-history")
        #expect(first.state == .finished && first.hasTranscript)
        #expect(first.tokens?.total == 129_500)
        #expect(first.startedAt == WireDate.parse("2026-10-02T09:12:03.12+00:00"))
        #expect(first.lastActivity == WireDate.parse("2026-10-02T10:02:44+00:00"))

        let second = page.items[1]
        #expect(second.host.retired)
        // A state this build doesn't know is still treated as live.
        #expect(second.state == .running)
        #expect(second.repo == nil && second.tokens == nil && !second.hasTranscript)
    }

    @Test func pageWithoutCursorHasNoMore() throws {
        let data = Data(#"{"items": [], "next_cursor": null, "has_more": true}"#.utf8)
        let page = try JSONDecoder().decode(HistoryPage.self, from: data)
        #expect(!page.hasMore && page.items.isEmpty)
    }

    @Test func facetsDecodeWithDefaults() throws {
        let data = Data(#"{"hosts": [{"id": "old", "name": "Old", "retired": true}], "repos": ["o/r"]}"#.utf8)
        let facets = try JSONDecoder().decode(HistoryFacets.self, from: data)
        #expect(facets.hosts.first?.retired == true)
        #expect(facets.harnesses.isEmpty && facets.repos == ["o/r"])
    }

    @Test func filterQueryItemsAreStableAndTrimmed() {
        let filter = HistoryFilter(
            hosts: ["b", "a"], harnesses: ["x"], repos: ["o/r"],
            states: [.failed, .awaiting],
            since: ISO8601DateFormatter().date(from: "2026-09-21T13:46:40Z"),
            query: "  pager window \n"
        )
        let pairs = filter.queryItems.map { "\($0.0)=\($0.1 ?? "")" }
        #expect(pairs == [
            "host=a", "host=b", "harness=x", "repo=o/r", "state=awaiting", "state=failed",
            "since=2026-09-21T13:46:40Z", "q=pager window",
        ])
        #expect(filter.activeCount == 7)
        #expect(HistoryFilter(query: "  ").isEmpty)
    }
}

// MARK: - Client

extension MockNetworkTests {
@Suite(.serialized)
struct SessionHistoryClientTests {
    let mock = MockNetwork()

    @Test func historyPageBuildsTheFilteredCursorQuery() async throws {
        nonisolated(unsafe) var seen: URL?
        mock.handler = { request in
            seen = request.url
            return (200, Data(#"{"items": [], "next_cursor": null, "has_more": false}"#.utf8))
        }
        let filter = HistoryFilter(hosts: ["mac mini"], states: [.running], query: "fix ws")
        _ = try await mock.client().historyPage(filter: filter, cursor: "abc_-", limit: 500)
        let url = try #require(seen)
        #expect(url.path == "/sessions/history")
        #expect(url.query == "host=mac%20mini&state=running&q=fix%20ws&limit=50&cursor=abc_-")
    }

    @Test func historyHasItsOwnRetryLane() async throws {
        nonisolated(unsafe) var paths: [String] = []
        mock.responseHeaders = ["Retry-After": "120"]
        defer { mock.responseHeaders = nil }
        mock.handler = { request in
            let path = request.url?.path ?? ""
            paths.append(path)
            if path.hasPrefix("/sessions/history") { return (503, Data()) }
            return (200, Data(#"{"hosts":[],"sessions":[]}"#.utf8))
        }
        let gate = HubRetryGate()
        let client = mock.client(retryGate: gate)
        do { _ = try await client.historyPage(filter: HistoryFilter(), cursor: nil); Issue.record("Expected busy") }
        catch DroverError.busy(let deadline) { #expect(deadline.timeIntervalSinceNow >= 119) }
        // Cooldown applies to history reads (facets included)...
        do { _ = try await client.historyFacets(); Issue.record("Cooldown bypassed") }
        catch DroverError.busy { }
        // ...but never to the fleet poll.
        _ = try await client.snapshot()
        #expect(paths == ["/sessions/history", "/harness"])
        #expect(DroverClient.retryLane(
            for: URL(string: "http://h/sessions/history/facets")!, baseURL: URL(string: "http://h")!
        ).lastPathComponent == "history-lane")
    }

    @Test func unsupportedHubIsAPlainStatusNotBusy() async throws {
        mock.handler = { _ in (501, Data(#"{"error": "session history needs the PostgreSQL control store"}"#.utf8)) }
        await #expect(throws: DroverError.httpStatus(501, "session history needs the PostgreSQL control store")) {
            _ = try await mock.client().historyPage(filter: HistoryFilter(), cursor: nil)
        }
    }
}
}
