import Foundation
import Testing
@testable import DroverKit

private let listingJSON = Data("""
{"roots":[{"name":"Home","path":"/workspace"}],"path":"/workspace/project",
 "parent":"/workspace","entries":[
 {"name":"Sources","path":"/workspace/project/Sources","is_dir":true,"is_git_repo":false,"hidden":false},
 {"name":"tools","path":"/workspace/project/tools","is_dir":true,"is_git_repo":true,"hidden":false}],
 "truncated":false}
""".utf8)

extension MockNetworkTests {
@Suite(.serialized)
struct FolderBrowserTests {
    let mock = MockNetwork()

    @Test @MainActor func transientFirstListingStaysLoadingUntilHostStateAndRetryComplete() async {
        let release = DispatchSemaphore(value: 0)
        nonisolated(unsafe) var lists = 0
        let resolvingHost = MockFlag()
        mock.handler = { request in
            if request.url?.path == "/harness" {
                resolvingHost.raise()
                #expect(release.wait(timeout: .now() + 5) == .success)
                return (200, Data(#"{"hosts":[{"host_id":"test-host","status":"online"}],"sessions":[]}"#.utf8))
            }
            lists += 1
            #expect(request.timeoutInterval == 20)
            return lists == 1 ? (502, Data(#"{"error":"unreachable"}"#.utf8)) : (200, listingJSON)
        }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        let refresh = Task { await model.refresh() }
        let waitDeadline = Date().addingTimeInterval(2)
        while !resolvingHost.isRaised && Date() < waitDeadline { await Task.yield() }
        #expect(resolvingHost.isRaised)
        #expect(model.isLoading && model.failure == nil && !model.canSelect)
        release.signal()
        await refresh.value
        #expect(lists == 2 && model.failure == nil && model.canSelect)
    }

    @Test @MainActor func aKnownOfflineHostIsNotRetried() async {
        nonisolated(unsafe) var lists = 0
        mock.handler = { request in
            if request.url?.path == "/harness" {
                return (200, Data(#"{"hosts":[{"host_id":"test-host","status":"offline"}],"sessions":[]}"#.utf8))
            }
            lists += 1
            return (502, Data(#"{"error":"unreachable"}"#.utf8))
        }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        await model.refresh()
        #expect(lists == 1 && model.failure == .offline && !model.isLoading)
    }

    @Test @MainActor func anOnlineHostWithPersistentListingFailureRetriesOnlyOnce() async {
        nonisolated(unsafe) var lists = 0
        mock.handler = { request in
            if request.url?.path == "/harness" {
                return (200, Data(#"{"hosts":[{"host_id":"test-host","status":"online"}],"sessions":[]}"#.utf8))
            }
            lists += 1
            return (504, Data(#"{"error":"timeout"}"#.utf8))
        }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        await model.refresh()
        #expect(lists == 2 && model.failure == .offline && !model.isLoading)
    }

    @Test @MainActor func sharedReadCooldownKeepsLoadingAndRetriesAfterItsDeadline() async {
        let client = mock.client()
        let deadline = Date().addingTimeInterval(0.05)
        await client.deferReads(until: deadline)
        mock.handler = { _ in
            #expect(Date() >= deadline)
            return (200, listingJSON)
        }
        let model = FolderBrowserModel(client: client, hostID: "test-host", path: "/workspace/project")
        await model.refresh()
        #expect(model.failure == nil && model.canSelect)
    }

    @Test func errorMappingCoversEndpointPermissionsNetworkAndPaths() {
        let cases: [(DroverError, FolderBrowserFailure)] = [
            (.unavailable("Not Found"), .unsupported),
            (.unavailable("{\"reason\":\"unsupported\"}"), .unsupported),
            (.unavailable("does not support path completion"), .unsupported),
            (.httpStatus(405, ""), .unsupported), (.httpStatus(501, ""), .unsupported),
            (.unavailable("unknown harness host: fixture"), .hostUnavailable),
            (.unauthorized, .authentication), (.httpStatus(401, ""), .authentication),
            (.httpStatus(403, ""), .permissionDenied),
            (.httpStatus(403, "outside_allowed_folders"), .outsideAllowedFolders),
            (.httpStatus(403, "symlink_not_allowed"), .symlinkNotAllowed),
            (.httpStatus(403, "hidden_folder_not_allowed"), .hiddenFolderNotAllowed),
            (.transport("offline"), .offline), (.transport("timed out"), .offline),
            (.httpStatus(504, ""), .offline), (.busy(until: .now), .hubBusy), (.httpStatus(503, ""), .hubBusy),
            (.badRequest("invalid path"), .unavailable),
            (.unavailable("path not found"), .unavailable),
            (.unavailable("not_found"), .unavailable),
            (.unavailable("not_directory"), .unavailable),
            (.unavailable("not a directory"), .unavailable),
            (.unavailable("outside allowed roots"), .outsideAllowedFolders),
            (.decoding("malformed"), .invalidResponse),
        ]
        for (error, expected) in cases { #expect(FolderBrowserFailure.classify(error) == expected) }
        #expect(FolderBrowserFailure.unsupported.message.contains("newer Drover version"))
    }

    @Test @MainActor func listingUsesSelectedHostAndEscapesQuery() async throws {
        mock.handler = { request in
            #expect(request.url?.path == "/harness/hosts/test-host/fs/list")
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            #expect(items.contains(URLQueryItem(name: "path", value: "/workspace/a & b")))
            #expect(items.contains(URLQueryItem(name: "filter", value: "tools & tests")))
            return (200, listingJSON)
        }
        let response = try await mock.client().listFolders(hostID: "test-host", path: "/workspace/a & b", filter: "tools & tests")
        #expect(response.entries.count == 2)
        #expect(response.entries[1].isGitRepo)
    }

    @Test @MainActor func browserRecentsIgnoreManualPrefixAndStayHostScoped() throws {
        let snapshot = try HarnessSnapshot.decode(from: Data("""
        {"hosts": [{"host_id":"test-host","status":"online"}],"sessions":[],
         "cwd_suggestions":[
          {"path":"/workspace/project","source":"recent session","host_id":"test-host"},
          {"path":"/workspace/other","source":"favorite","host_id":"test-host"},
          {"path":"/workspace/foreign","source":"recent session","host_id":"other-host"}]}
        """.utf8))
        let model = LaunchModel(client: mock.client(), snapshot: snapshot)
        model.cwd = "/unmatched"
        #expect(model.cwdSuggestions.isEmpty)
        #expect(model.savedCwdSuggestions == ["/workspace/project", "/workspace/other"])
        model.hostID = "other-host"
        #expect(model.savedCwdSuggestions == ["/workspace/foreign"])
    }

    @Test @MainActor func loadedFolderEnablesSelectionAndLocalFilter() async {
        mock.handler = { _ in (200, listingJSON) }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        #expect(!model.canSelect)
        await model.refresh()
        #expect(model.canSelect)
        #expect(model.breadcrumbs.map(\.path) == ["/workspace", "/workspace/project"])
        model.filter = "SOURCE"
        #expect(model.filteredEntries.map(\.name) == ["Sources"])
        model.filter = "missing"
        #expect(model.filteredEntries.isEmpty)
    }

    @Test @MainActor func permissionFailureDisablesSelectionAndRetryRecovers() async {
        mock.handler = { _ in (403, Data("{\"error\":\"permission_denied\"}".utf8)) }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        await model.refresh()
        #expect(model.failure == .permissionDenied)
        #expect(!model.canSelect)
        mock.handler = { _ in (200, listingJSON) }
        await model.refresh()
        #expect(model.failure == nil)
        #expect(model.canSelect)
    }

    @Test @MainActor func entryAndOfflineStatesCannotSelect() async {
        mock.handler = { _ in (200, Data("{\"roots\":[],\"path\":null,\"parent\":null,\"entries\":[],\"truncated\":false}".utf8)) }
        let entry = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "")
        await entry.refresh()
        #expect(!entry.canSelect)
        mock.handler = { _ in (502, Data("{\"error\":\"unreachable\"}".utf8)) }
        let offline = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace")
        await offline.refresh()
        #expect(offline.failure == .offline)
        #expect(!offline.canSelect)
    }

    @Test @MainActor func malformedListingFailsClosed() async {
        mock.handler = { _ in (200, Data("{\"entries\":[]}".utf8)) }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace")
        await model.refresh()
        #expect(model.failure != nil)
        #expect(!model.canSelect)
    }

    @Test @MainActor func unknownHostIsUnavailableRatherThanUnsupported() async {
        mock.handler = { _ in (404, Data("{\"error\":\"unknown harness host: test-host\"}".utf8)) }
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "")
        await model.refresh()
        #expect(model.failure == .hostUnavailable)
    }

    @Test @MainActor func olderHostAndAuthenticationAreDistinct() async {
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "")
        mock.handler = { _ in (404, Data("{\"reason\":\"unsupported\"}".utf8)) }
        await model.refresh()
        #expect(model.failure == .unsupported)
        mock.handler = { _ in (401, Data("{\"error\":\"authentication required\"}".utf8)) }
        await model.refresh()
        #expect(model.failure == .authentication)
    }

    @Test @MainActor func creationUsesSelectedHostAndCurrentDirectory() async throws {
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, listingJSON) }
        await model.refresh()
        mock.handler = { request in
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/harness/hosts/test-host/fs/create")
            let data = request.httpBody ?? request.bodyStreamData()
            let body = try! JSONSerialization.jsonObject(with: data) as! [String: String]
            #expect(body == ["path": "/workspace/project", "name": "new & tools"])
            return (201, Data("{\"name\":\"new & tools\",\"path\":\"/workspace/project/new & tools\"}".utf8))
        }
        #expect(await model.createFolder(name: "new & tools") == "/workspace/project/new & tools")
        #expect(!model.isCreating)
        #expect(model.creationFailure == nil)
    }

    @Test @MainActor func creationFailuresKeepPickerOpenAndCanRetry() async {
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, listingJSON) }
        await model.refresh()
        for (status, code, expected) in [(400, "invalid_name", FolderBrowserFailure.invalidName),
                                          (409, "folder_exists", .folderExists), (403, "permission_denied", .permissionDenied)] {
            mock.handler = { _ in (status, Data("{\"error\":\"\(code)\"}".utf8)) }
            #expect(await model.createFolder(name: "new") == nil)
            #expect(model.creationFailure == expected)
            #expect(model.canSelect)
        }
        mock.handler = { _ in (201, Data("{\"name\":\"new\",\"path\":\"/workspace/project/new\"}".utf8)) }
        #expect(await model.createFolder(name: "new") != nil)
        #expect(model.creationFailure == nil)
    }

    @Test @MainActor func creationCannotSelectUnexpectedDestinationOrUnloadedFolder() async {
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        #expect(await model.createFolder(name: "new") == nil)
        mock.handler = { _ in (200, listingJSON) }
        await model.refresh()
        mock.handler = { _ in (201, Data("{\"name\":\"new\",\"path\":\"/other/new\"}".utf8)) }
        #expect(await model.createFolder(name: "new") == nil)
        #expect(model.creationFailure == .invalidResponse)
    }

    @Test @MainActor func pagingAppendsDeduplicatesAndStopsAtEnd() async {
        let first = Data(String(decoding: listingJSON, as: UTF8.self)
            .replacingOccurrences(of: "\"truncated\":false", with: "\"truncated\":true,\"next_cursor\":\"page & 2\"").utf8)
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, first) }
        await model.refresh()
        mock.handler = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            #expect(items.contains(URLQueryItem(name: "cursor", value: "page & 2")))
            #expect(items.contains(URLQueryItem(name: "limit", value: "100")))
            return (200, Data("""
            {"roots":[{"name":"Home","path":"/workspace"}],"path":"/workspace/project",
             "parent":"/workspace","entries":[
             {"name":"tools","path":"/workspace/project/tools","is_dir":true,"is_git_repo":true,"hidden":false},
             {"name":"next","path":"/workspace/project/next","is_dir":true,"is_git_repo":false,"hidden":false}],
             "truncated":false,"next_cursor":null}
            """.utf8))
        }
        await model.loadMore()
        #expect(model.filteredEntries.map(\.name) == ["Sources", "tools", "next"])
        #expect(model.listing?.nextCursor == nil)
        mock.handler = { _ in Issue.record("No request after last page"); return (500, Data()) }
        await model.loadMore()
    }

    @Test @MainActor func pagingFailurePreservesEntriesAndFilterRefreshResetsPages() async {
        let first = Data(String(decoding: listingJSON, as: UTF8.self)
            .replacingOccurrences(of: "\"truncated\":false", with: "\"truncated\":true,\"next_cursor\":\"page2\"").utf8)
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, first) }
        await model.refresh()
        mock.handler = { _ in (502, Data("{\"error\":\"offline\"}".utf8)) }
        await model.loadMore()
        #expect(model.pageFailure == .offline)
        #expect(model.listing?.entries.count == 2)
        #expect(model.canSelect)
        model.filter = "SOURCE"
        mock.handler = { _ in Issue.record("No page request for changed filter"); return (500, Data()) }
        await model.loadMore()
        mock.handler = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            #expect(items.contains(URLQueryItem(name: "cursor", value: "")))
            #expect(items.contains(URLQueryItem(name: "filter", value: "SOURCE")))
            return (200, listingJSON)
        }
        await model.refresh()
        #expect(model.pageFailure == nil)
        #expect(model.listing?.nextCursor == nil)
        #expect(model.filteredEntries.map(\.name) == ["Sources"])
    }


    @Test @MainActor func refreshDiscardsAnInFlightPageAndPreventsDuplicateLoads() async throws {
        let first = Data(String(decoding: listingJSON, as: UTF8.self)
            .replacingOccurrences(of: "\"truncated\":false", with: "\"truncated\":true,\"next_cursor\":\"page2\"").utf8)
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, first) }
        await model.refresh()
        let started = MockFlag()
        mock.responseDelay = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            if items.contains(URLQueryItem(name: "cursor", value: "page2")) {
                #expect(!started.isRaised)
                started.raise()
                return 0.1
            }
            return nil
        }
        mock.handler = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            if items.contains(URLQueryItem(name: "cursor", value: "page2")) {
                return (200, first)
            }
            return (200, listingJSON)
        }
        let page = Task { await model.loadMore() }
        for _ in 0..<100 where !started.isRaised { try await Task.sleep(for: .milliseconds(1)) }
        #expect(started.isRaised)
        await model.loadMore()
        model.filter = "tools"
        await model.refresh()
        await page.value
        #expect(model.listing?.nextCursor == nil)
        #expect(model.listing?.entries.count == 2)
        #expect(!model.isLoadingMore)
        #expect(model.pageFailure == nil)
    }

    @Test @MainActor func staleCursorExplainsReloadAndPreservesSelection() async {
        let first = Data(String(decoding: listingJSON, as: UTF8.self)
            .replacingOccurrences(of: "\"truncated\":false", with: "\"truncated\":true,\"next_cursor\":\"page2\"").utf8)
        let model = FolderBrowserModel(client: mock.client(), hostID: "test-host", path: "/workspace/project")
        mock.handler = { _ in (200, first) }
        await model.refresh()
        mock.handler = { _ in (400, Data("{\"error\":\"invalid_cursor\"}".utf8)) }
        await model.loadMore()
        #expect(model.pageFailure == .staleListing)
        #expect(model.pageFailure?.message.contains("Reload folders") == true)
        #expect(model.canSelect)
        #expect(model.listing?.entries.count == 2)
    }

}
}
