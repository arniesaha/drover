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

    @Test func errorMappingCoversEndpointPermissionsNetworkAndPaths() {
        let cases: [(DroverError, FolderBrowserFailure)] = [
            (.unavailable("Not Found"), .unsupported),
            (.unavailable("{\"reason\":\"unsupported\"}"), .unsupported),
            (.unavailable("does not support path completion"), .unsupported),
            (.httpStatus(405, ""), .unsupported), (.httpStatus(501, ""), .unsupported),
            (.unavailable("unknown harness host: fixture"), .hostUnavailable),
            (.unauthorized, .authentication), (.httpStatus(401, ""), .authentication),
            (.httpStatus(403, ""), .permissionDenied),
            (.transport("offline"), .offline), (.transport("timed out"), .offline),
            (.httpStatus(504, ""), .offline), (.busy(until: .now), .offline),
            (.badRequest("invalid path"), .unavailable),
            (.unavailable("path not found"), .unavailable),
            (.unavailable("not_found"), .unavailable),
            (.unavailable("not_directory"), .unavailable),
            (.unavailable("not a directory"), .unavailable),
            (.unavailable("outside allowed roots"), .permissionDenied),
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
}
}
