import Foundation

#if DEBUG
/// Synthetic directory metadata for the existing isolated UI-test transport.
/// No filesystem access, credentials, file contents or network connections.
enum FolderBrowserFixture {
    static func response(url: URL) -> FixtureHubResponse {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let path = items.first { $0.name == "path" }?.value ?? ""
        let filter = items.first { $0.name == "filter" }?.value ?? ""
        if path == "/fixture/locked" {
            return .json(status: 403, ["error": "permission_denied"])
        }
        if path == "/fixture/offline" {
            return .json(status: 502, ["error": "unreachable"])
        }
        let children: [(String, Bool)]
        switch path {
        case "": children = []
        case "/fixture": children = [("locked", false), ("offline", false), ("project", true)]
        case "/fixture/project": children = [("Sources", false), ("tools", true)]
        case "/fixture/project/Sources", "/fixture/project/tools": children = []
        default: return .json(status: 400, ["error": "not_found"])
        }
        return .json(status: 200, [
            "roots": [["name": "Home", "path": "/fixture"]],
            "path": path.isEmpty ? NSNull() : path as Any,
            "parent": path.isEmpty || path == "/fixture" ? NSNull() : (path as NSString).deletingLastPathComponent as Any,
            "entries": children.filter { filter.isEmpty || $0.0.localizedCaseInsensitiveContains(filter) }.map { name, git in
                ["name": name, "path": path + "/" + name, "is_dir": true,
                 "is_git_repo": git, "hidden": false] as [String: Any]
            },
            "truncated": false,
        ])
    }
}
#endif
