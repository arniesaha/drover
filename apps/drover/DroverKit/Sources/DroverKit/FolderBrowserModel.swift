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
}

public enum FolderBrowserFailure: Sendable, Equatable {
    case permissionDenied, authentication, offline, unsupported, hostUnavailable, unavailable, invalidResponse

    public var message: String {
        switch self {
        case .permissionDenied: "This folder is unavailable or outside the allowed locations."
        case .authentication: "Check the connection credential in Settings."
        case .offline: "Host offline or unreachable. Try again when it reconnects."
        case .unsupported: "Update this host to browse folders."
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
    private var generation = 0

    public init(client: DroverClient, hostID: String, path: String) {
        self.client = client
        self.hostID = hostID
        self.path = path
    }

    public var canSelect: Bool {
        !path.isEmpty && listing?.path != nil && !isLoading && failure == nil
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
        failure = nil
        do {
            let response = try await client.listFolders(hostID: hostID, path: path, filter: filter)
            guard requestGeneration == generation else { return }
            if Task.isCancelled {
                isLoading = false
                return
            }
            listing = response
        } catch {
            guard requestGeneration == generation else { return }
            if Task.isCancelled || (error as? DroverError)?.isCancellation == true {
                isLoading = false
                return
            }
            switch error as? DroverError {
            case .unauthorized: failure = .authentication
            case .httpStatus(403, _): failure = .permissionDenied
            case .httpStatus(502, _), .httpStatus(504, _), .transport, .busy: failure = .offline
            case .unavailable(let detail):
                failure = detail.contains("does not support path completion") || detail.contains("unsupported")
                    ? .unsupported : .hostUnavailable
            case .badRequest: failure = .unavailable
            default: failure = .invalidResponse
            }
        }
        isLoading = false
    }
}
