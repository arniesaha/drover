import Foundation

/// Something a session produced that you might want to act on outside the
/// app — a branch it pushed, a pull request it opened.
///
/// These are the transcript's machine strings promoted out of prose. A branch
/// name buried in the middle of a paragraph wraps, can't be tapped, and is
/// miserable to select on a phone; as a row it gets middle truncation and one
/// action.
public struct SessionArtifact: Sendable, Equatable, Identifiable {
    public enum Kind: String, Sendable, Equatable {
        case branch = "Branch"
        case pullRequest = "Pull request"
    }

    public let kind: Kind
    /// What the row shows — already shortened for display where that is
    /// lossless (a PR URL becomes `owner/repo #142`).
    public let value: String
    /// Present when the artifact can be opened rather than only copied.
    public let url: URL?

    public var id: String { "\(kind.rawValue):\(value)" }

    /// One verb per row: open it if it's addressable, otherwise copy it.
    public var action: String { url == nil ? "Copy" : "Open" }

    public init(kind: Kind, value: String, url: URL? = nil) {
        self.kind = kind
        self.value = value
        self.url = url
    }
}

/// Pulls artifacts out of a transcript.
///
/// This is deliberately heuristic and deliberately narrow. The hub does not
/// report a branch or a PR anywhere in its session record, so the only place
/// this information exists is in what the harness ran and what came back. It
/// recognises the two shapes that actually occur — a `git push` and a GitHub
/// pull-request URL — and stays quiet otherwise. Missing an artifact costs a
/// row; inventing one costs trust, so every pattern here is anchored.
public enum SessionArtifactExtractor {
    public static func artifacts(in messages: [HarnessMessage]) -> [SessionArtifact] {
        var found: [SessionArtifact] = []
        var seen: Set<String> = []

        func add(_ artifact: SessionArtifact) {
            // Later mentions win their position but not a duplicate row: a
            // branch pushed three times is still one branch.
            guard seen.insert(artifact.id).inserted else { return }
            found.append(artifact)
        }

        for message in messages {
            for command in commands(in: message) {
                if let branch = branch(inPushCommand: command) {
                    add(SessionArtifact(kind: .branch, value: branch))
                }
            }
            for url in pullRequestURLs(in: searchableText(of: message)) {
                guard let artifact = pullRequest(from: url) else { continue }
                add(artifact)
            }
        }
        return found
    }

    // MARK: - Branches

    /// Only from a command that actually pushes. Reading branch names out of
    /// `git checkout` or `git status` output would surface branches that were
    /// never published, which is the opposite of what an artifact row means.
    static func branch(inPushCommand command: String) -> String? {
        let tokens = command.split(whereSeparator: \.isWhitespace).map(String.init)
        guard tokens.contains("git"), tokens.contains("push") else { return nil }
        guard let remoteIndex = tokens.firstIndex(where: { $0 == "origin" || $0 == "upstream" }),
              tokens.indices.contains(remoteIndex + 1) else { return nil }

        let candidate = tokens[remoteIndex + 1]
        guard !candidate.hasPrefix("-") else { return nil }
        // `git push origin HEAD` and `:refs/...` deletions name no branch.
        guard candidate != "HEAD", !candidate.hasPrefix(":") else { return nil }
        // `local:remote` refspec — the remote half is the branch that exists.
        if let colon = candidate.firstIndex(of: ":") {
            let remote = String(candidate[candidate.index(after: colon)...])
            return remote.isEmpty ? nil : remote
        }
        return candidate
    }

    // MARK: - Pull requests

    static func pullRequestURLs(in text: String) -> [URL] {
        guard text.contains("/pull/") else { return [] }
        return text
            .split(whereSeparator: { $0.isWhitespace || $0 == "(" || $0 == ")" || $0 == "<" || $0 == ">" })
            .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: ".,;\"'`")) }
            .filter { $0.hasPrefix("https://github.com/") && $0.contains("/pull/") }
            .compactMap(URL.init(string:))
    }

    /// `https://github.com/arniesaha/drover/pull/142` → `arniesaha/drover #142`.
    /// Shortening is lossless here because the URL rides along for the action.
    ///
    /// nil for any `/pull/` path that does not name a numbered pull request.
    /// The one that matters is `/pull/new/<branch>` — the "create a pull
    /// request" link `git push` prints for every branch it pushes. Admitting
    /// it produced a second row per branch, captioned PULL REQUEST, showing a
    /// middle-truncated URL, opening a form for a PR that may already exist.
    static func pullRequest(from url: URL) -> SessionArtifact? {
        let parts = url.path.split(separator: "/").map(String.init)
        guard parts.count >= 4, parts[2] == "pull", Int(parts[3]) != nil else { return nil }
        return SessionArtifact(kind: .pullRequest,
                               value: "\(parts[0])/\(parts[1]) #\(parts[3])",
                               url: url)
    }

    // MARK: - Message plumbing

    private static func commands(in message: HarnessMessage) -> [String] {
        guard message.type == .toolAction,
              let command = message.payload["input"]?.objectValue?["command"]?.stringValue
        else { return [] }
        // One tool call often chains several commands; each is its own
        // candidate so `git add -A && git push origin x` is not missed.
        return command
            .components(separatedBy: CharacterSet(charactersIn: "\n;"))
            .flatMap { $0.components(separatedBy: "&&") }
    }

    /// A PR URL can appear in `gh pr create` output or in the assistant's own
    /// summary, so both are searched — but never in a tool *action*, where it
    /// would only be the command that was about to run.
    private static func searchableText(of message: HarnessMessage) -> String {
        switch message.type {
        case .toolResult, .assistantOutput: return message.text
        default: return ""
        }
    }
}
