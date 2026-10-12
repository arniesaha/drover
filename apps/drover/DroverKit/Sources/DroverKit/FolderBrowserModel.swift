import Foundation
import Observation

public struct FolderLocation: Decodable, Sendable, Equatable, Identifiable, Hashable {
    public let name: String
    public let path: String
    public var id: String { path }

    public init(name: String, path: String) {
        self.name = name
        self.path = path
    }
}

public struct FolderEntry: Decodable, Sendable, Equatable, Identifiable {
    public let name: String
    public let path: String
    public let isDir: Bool
    public let isGitRepo: Bool
    public let hidden: Bool
    public var id: String { path }

    private enum CodingKeys: String, CodingKey {
        case name, path, hidden
        case isDir = "is_dir"
        case isGitRepo = "is_git_repo"
    }
}

/// Strict decoding: incomplete listings must never enable folder selection.
public struct FolderListing: Decodable, Sendable, Equatable {
    public let roots: [FolderLocation]
    public let path: String?
    public let parent: String?
    public let entries: [FolderEntry]
    public let truncated: Bool
    public let nextCursor: String?
    private enum CodingKeys: String, CodingKey {
        case roots, path, parent, entries, truncated
        case nextCursor = "next_cursor"
    }
}

public enum FolderBrowserFailure: Sendable, Equatable {
    case staleListing, invalidName, folderExists, outsideAllowedFolders, symlinkNotAllowed, hiddenFolderNotAllowed, permissionDenied, authentication, offline, unsupported, hostUnavailable, unavailable, invalidResponse

    public static func classify(_ error: Error) -> Self {
        switch error as? DroverError {
        case .conflict, .httpStatus(409, _): return .folderExists
        case .badRequest("invalid_cursor"): return .staleListing
        case .badRequest("invalid_name"): return .invalidName
        case .unauthorized, .httpStatus(401, _): return .authentication
        case .httpStatus(403, let detail):
            switch detail {
            case "outside_allowed_folders": return .outsideAllowedFolders
            case "symlink_not_allowed": return .symlinkNotAllowed
            case "hidden_folder_not_allowed": return .hiddenFolderNotAllowed
            default: return .permissionDenied
            }
        case .httpStatus(408, _), .httpStatus(502, _), .httpStatus(503, _),
             .httpStatus(504, _), .transport, .busy: return .offline
        case .httpStatus(405, _), .httpStatus(501, _): return .unsupported
        case .unavailable(let detail):
            let detail = detail.lowercased()
            if detail.contains("unknown harness host") { return .hostUnavailable }
            if detail.contains("outside") { return .outsideAllowedFolders }
            if detail.contains("permission") { return .permissionDenied }
            if detail.contains("offline") || detail.contains("unreachable") { return .offline }
            if ["not_found", "not_directory", "invalid_path", "unavailable"].contains(detail)
                || detail.contains("not a directory") || detail.contains("not a folder")
                || detail.contains("no such file") || detail.contains("path not found") { return .unavailable }
            // A generic 404 (including an HTML router response) means the
            // folder endpoint is missing, not that the selected host vanished.
            return .unsupported
        case .badRequest: return .unavailable
        default: return .invalidResponse
        }
    }

    public var message: String {
        switch self {
        case .staleListing: "This folder changed while browsing. Reload folders to continue."
        case .invalidName: "Enter a folder name without slashes, a leading dot, or surrounding spaces."
        case .folderExists: "A folder or file with this name already exists. Choose another name."
        case .outsideAllowedFolders: "This folder is outside the host’s allowed folders. Choose a folder from the listed locations."
        case .symlinkNotAllowed: "This folder uses a symbolic link, which this host does not allow for browsing."
        case .hiddenFolderNotAllowed: "This host does not allow browsing hidden folders."
        case .permissionDenied: "You do not have permission to browse this folder."
        case .authentication: "Check the connection credential in Settings."
        case .offline: "Host offline or unreachable. Try again when it reconnects."
        case .unsupported: "This host needs a newer Drover version to browse folders. You can still enter a path manually."
        case .hostUnavailable: "This host is no longer available. Select another host."
        case .unavailable: "This path is missing or is not a folder."
        case .invalidResponse: "The host returned an invalid folder listing."
        }
    }
}

/// One navigation destination owns one model. Host and path never change,
/// so navigating back restores the previous folder and cannot mix hosts.
@MainActor @Observable
public final class FolderBrowserModel {
    private let client: DroverClient
    public let hostID: String
    public let path: String
    public var filter = ""
    public private(set) var listing: FolderListing?
    public private(set) var failure: FolderBrowserFailure?
    public private(set) var isLoading = false
    public private(set) var isLoadingMore = false
    public private(set) var pageFailure: FolderBrowserFailure?
    public private(set) var isCreating = false
    public private(set) var creationFailure: FolderBrowserFailure?
    private var loadedFilter = ""
    private var generation = 0

    public init(client: DroverClient, hostID: String, path: String) {
        self.client = client
        self.hostID = hostID
        self.path = path
    }

    public var canSelect: Bool {
        !path.isEmpty && listing?.path != nil && !isLoading && !isCreating && failure == nil
    }

    public var filteredEntries: [FolderEntry] {
        (listing?.entries ?? []).filter {
            $0.isDir && !$0.hidden && (filter.isEmpty || $0.name.localizedCaseInsensitiveContains(filter))
        }
    }

    public var breadcrumbs: [FolderLocation] {
        guard let listing, let current = listing.path,
              let root = listing.roots
                .filter({ current == $0.path || current.hasPrefix($0.path == "/" ? "/" : $0.path + "/") })
                .max(by: { $0.path.count < $1.path.count }) else { return [] }
        var result = [root]
        let suffix = current.dropFirst(root.path.count).split(separator: "/")
        var ancestor = root.path
        for name in suffix {
            ancestor = (ancestor == "/" ? "" : ancestor) + "/" + name
            result.append(FolderLocation(name: String(name), path: ancestor))
        }
        return result
    }

    public func refresh() async {
        generation &+= 1
        let requestGeneration = generation
        isLoading = true
        isLoadingMore = false
        pageFailure = nil
        failure = nil
        let requestedFilter = filter
        do {
            let response = try await client.listFolders(hostID: hostID, path: path, filter: requestedFilter)
            guard requestGeneration == generation else { return }
            if Task.isCancelled {
                isLoading = false
                return
            }
            listing = response
            loadedFilter = requestedFilter
        } catch {
            guard requestGeneration == generation else { return }
            if Task.isCancelled || (error as? DroverError)?.isCancellation == true {
                isLoading = false
                return
            }
            failure = FolderBrowserFailure.classify(error)
        }
        isLoading = false
    }

    public func loadMore() async {
        guard !isLoading, !isLoadingMore, filter == loadedFilter,
              let previous = listing, let cursor = previous.nextCursor else { return }
        let requestGeneration = generation
        let requestedFilter = loadedFilter
        isLoadingMore = true
        pageFailure = nil
        do {
            let response = try await client.listFolders(hostID: hostID, path: path, filter: requestedFilter, cursor: cursor)
            guard generation == requestGeneration, filter == requestedFilter, !Task.isCancelled else {
                if generation == requestGeneration { isLoadingMore = false }
                return
            }
            guard response.path == previous.path, response.roots == previous.roots else {
                throw DroverError.decoding("Page destination changed")
            }
            let ids = Set(previous.entries.map(\.id))
            listing = FolderListing(roots: response.roots, path: response.path, parent: response.parent,
                                    entries: previous.entries + response.entries.filter { !ids.contains($0.id) },
                                    truncated: response.truncated, nextCursor: response.nextCursor)
        } catch {
            guard generation == requestGeneration else { return }
            if filter == requestedFilter && !Task.isCancelled && (error as? DroverError)?.isCancellation != true {
                pageFailure = FolderBrowserFailure.classify(error)
            }
        }
        isLoadingMore = false
    }

    public func prepareCreation() { creationFailure = nil }

    public func createFolder(name: String) async -> String? {
        guard canSelect, !isCreating, let parent = listing?.path else { return nil }
        isCreating = true
        creationFailure = nil
        defer { isCreating = false }
        do {
            let folder = try await client.createFolder(hostID: hostID, path: parent, name: name)
            let expected = (parent == "/" ? "" : parent) + "/" + name
            guard folder.path == expected else { throw DroverError.decoding("Created folder destination changed") }
            return folder.path
        } catch {
            creationFailure = FolderBrowserFailure.classify(error)
            return nil
        }
    }

}

/// A saved location's path remains the selection value; its label describes the project.
public enum FolderRecentPresentation {
    public static func name(path: String, sessions: [SessionSummary] = []) -> String {
        if let repo = sessions.first(where: { $0.cwd == path })?.repoName,
           !repo.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return repo
        }
        let parts = path.split(separator: "/").map(String.init)
        if let index = parts.lastIndex(where: { [".worktrees", "worktrees"].contains($0) }), index > 0 {
            let parent = parts[index - 1]
            return parent == ".drover" ? "Project worktree" : parent
        }
        return parts.last ?? "/"
    }
}
