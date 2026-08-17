import Foundation

// MARK: - PathCompletionEntry

/// One directory the host offered for the text typed so far.
public struct PathCompletionEntry: Sendable, Equatable, Decodable, Identifiable {
    /// The leaf name, e.g. "arnabmac".
    public var name: String
    /// The absolute path, e.g. "/Users/arnabmac".
    public var path: String

    public var id: String { path }

    public init(name: String, path: String) {
        self.name = name
        self.path = path
    }
}

// MARK: - PathCompletion

/// The host's answer to "what directories could this half-typed path be?".
///
/// A parent that does not exist, or that the harness user cannot read, is not
/// a failure: the server answers 200 with no entries and an `error` key
/// ("not_found" / "permission_denied"), because a half-typed path is the
/// normal state of a field being typed into. Only an unreachable or timed-out
/// host produces a thrown error at the client.
public struct PathCompletion: Sendable, Equatable, Decodable {
    /// The directory the entries were listed from.
    public var parent: String
    public var entries: [PathCompletionEntry]
    /// True when the host truncated a very large directory listing.
    public var truncated: Bool
    /// "not_found" / "permission_denied" when the parent could not be listed.
    public var error: String?

    public init(
        parent: String,
        entries: [PathCompletionEntry],
        truncated: Bool = false,
        error: String? = nil
    ) {
        self.parent = parent
        self.entries = entries
        self.truncated = truncated
        self.error = error
    }

    private enum CodingKeys: String, CodingKey {
        case parent, entries, truncated, error
    }

    /// Hand-rolled and forgiving for the same reason `HarnessSnapshot` is: a
    /// field the server adds later, or one entry the client cannot read, must
    /// not throw away the whole response mid-keystroke.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        parent = (try? container.decode(String.self, forKey: .parent)) ?? ""
        entries = (try? container.decode([PathCompletionEntry].self, forKey: .entries)) ?? []
        truncated = (try? container.decode(Bool.self, forKey: .truncated)) ?? false
        error = try? container.decode(String.self, forKey: .error)
    }
}

// MARK: - PathExistsResponse

/// `{"exists": {"/a": true, "/b": false}}` — true only when the path exists on
/// the host *and* is a directory.
public struct PathExistsResponse: Sendable, Equatable, Decodable {
    public var exists: [String: Bool]

    public init(exists: [String: Bool]) {
        self.exists = exists
    }

    private enum CodingKeys: String, CodingKey {
        case exists
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        exists = (try? container.decode([String: Bool].self, forKey: .exists)) ?? [:]
    }
}
