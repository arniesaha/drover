import Foundation

/// How one session renders as a card in the fleet inbox.
///
/// There are two species because the data genuinely differs. A conversation
/// session has assistant text and can ask you for something; a shell session
/// has raw terminal output and never can — `awaiting` is only ever nil for
/// PTY, so `attention` there is only ever `.working` or `.done` and a terminal
/// card can never enter the needs-you bucket. Both species sit in the same
/// list ordered by activity; the form and the `action` verb carry the
/// difference.
///
/// Every card is one loud thing (`title`) over one quiet line (`subtitle`),
/// under a machine-string kicker. Nothing here is a middot soup of equal-weight
/// facts — that was the baseline's actual problem.
public struct SessionCardPresentation: Sendable, Equatable {
    public enum Species: Sendable, Equatable {
        case conversation
        case terminal
    }

    /// The single suggested action the card offers. One verb, never a menu.
    public enum Action: String, Sendable, Equatable {
        case approve = "Approve"
        case answer = "Answer"
        case watch = "Watch"
        /// A finished conversation can still be picked up — that is a handoff.
        case resume = "Resume"
        case attach = "Attach"
        case reopen = "Reopen"
    }

    public let species: Species
    public let harness: HarnessPresentation

    /// The machine string above the title — project name for a conversation,
    /// working directory for a shell. `nil` when there is nothing to show, or
    /// when the path has been promoted into the title (see below).
    public let kicker: String?

    /// The one loud thing on the row.
    public let title: String

    /// `state · host · harness`, quiet. Never repeats what the title says.
    /// State is placed first so it is never truncated on narrow screens.
    public let subtitle: String

    /// The single verb the card offers — or `nil` when the snapshot it was
    /// drawn from is stale.
    ///
    /// Every verb here is derived from `attention`, and `attention` is exactly
    /// what a snapshot nobody could refresh cannot vouch for. A card once read
    /// "asked a question" with an **Answer** beside it while the session had
    /// long since gone back to work; acting on that pushes a turn into a
    /// session mid-work (#81). Offering nothing is the honest answer — the
    /// card still opens on tap, and the session screen fetches live state.
    public let action: Action?

    /// Terminal cards only: `$` while attached, `‹` once exited.
    public let sigil: String?

    /// True when `title` is *not* derived from session content — the session
    /// has told us nothing yet. Shipping builds hit this constantly (the hub
    /// sends no preview until the first assistant text), and the old fallback
    /// filled the loudest line on the card with "Codex session in drover",
    /// which carries no information at all. The view quiets a placeholder
    /// title instead of shouting it.
    public let isTitlePlaceholder: Bool

    public let projectName: String?

    /// True when this card describes a snapshot we could not refresh. Nothing
    /// on it is wrong on purpose — it is simply older than it looks.
    public let isStale: Bool

    /// What a stale card says where its verb would have been: how old the
    /// snapshot itself is. Nil while fresh.
    public let staleNote: String?

    /// The session's activity age, measured against the snapshot rather than
    /// against now, so a frozen card stops counting up. Nil while fresh — a
    /// live card keeps the ticking relative formatter (#81).
    public let frozenActivityText: String?

    public init(
        session: SessionSummary,
        hostTitle: String,
        freshness: SnapshotFreshness = .live
    ) {
        harness = HarnessPresentation(session.harness)
        species = session.isStructured ? .conversation : .terminal
        projectName = Self.nonEmpty(session.repoName) ?? Self.projectName(for: session.cwd)
        isStale = freshness.isStale
        staleNote = freshness.staleNote
        frozenActivityText = freshness.frozenActivityText(for: session.activityDate)

        let attention = session.attention
        let state = Self.statePhrase(attention, species: species)
        let shortHost = Self.shortHostTitle(hostTitle)
        let path = Self.abbreviatedPath(session.cwd)
        let harnessName = Self.nonEmpty(harness.name)
        let hostName = Self.nonEmpty(shortHost)
        var subtitleParts: [String] = []

        switch species {
        case .conversation:
            kicker = projectName
            sigil = nil
            let verb: Action = switch attention {
            case .needsApproval: .approve
            case .needsInput: .answer
            case .working: .watch
            case .done, .errored: .resume
            }
            action = freshness.isStale ? nil : verb
            if let rawTitle = Self.firstLine(of: session.recap) ?? Self.firstLine(of: session.preview) {
                let cleaned = Self.cleanMarkdownTitle(rawTitle)
                if !cleaned.isEmpty {
                    title = cleaned
                    isTitlePlaceholder = false
                    subtitleParts = [state, hostName, harnessName].compactMap { $0 }
                } else {
                    title = state.capitalizedFirst
                    isTitlePlaceholder = true
                    subtitleParts = [harnessName, hostName].compactMap { $0 }
                }
            } else {
                // With no preview the state *is* the only real information, so
                // it gets promoted to the loud line rather than duplicated into
                // a manufactured one — and then dropped from the subtitle so
                // the row still says exactly one thing.
                title = state.capitalizedFirst
                isTitlePlaceholder = true
                subtitleParts = [harnessName, hostName].compactMap { $0 }
            }

        case .terminal:
            sigil = (attention == .working) ? "$" : "‹"
            let verb: Action = (attention == .working) ? .attach : .reopen
            action = freshness.isStale ? nil : verb
            if let lastLine = Self.lastLine(of: session.preview) {
                kicker = path
                title = lastLine
                isTitlePlaceholder = false
                subtitleParts = [state, hostName, harnessName].compactMap { $0 }
            } else {
                // Nothing has been printed yet, so the working directory is
                // the loudest true thing about this shell. It moves up into
                // the title rather than being repeated as both kicker and
                // title.
                kicker = nil
                title = path ?? "\(harness.name) session"
                isTitlePlaceholder = true
                subtitleParts = [state, hostName, harnessName, "no output yet"].compactMap { $0 }
            }
        }

        subtitle = subtitleParts.joined(separator: " · ")
    }

    // MARK: - Derivations

    private static func statePhrase(_ attention: AttentionState, species: Species) -> String {
        switch species {
        case .terminal:
            switch attention {
            case .errored: return "exited with an error"
            case .done: return "exited"
            default: return "attached"
            }
        case .conversation:
            switch attention {
            case .needsApproval: return "needs approval"
            case .needsInput: return "asked a question"
            case .working: return "running"
            case .done: return "finished"
            case .errored: return "errored"
            }
        }
    }

    private static func projectName(for cwd: String?) -> String? {
        guard let cwd = nonEmpty(cwd) else { return nil }
        return nonEmpty(URL(fileURLWithPath: cwd).lastPathComponent)
    }

    /// `/Users/sample-user/src/drover` → `~/src/drover`. The hub reports the remote
    /// host's absolute path and we don't know that machine's home directory,
    /// so this matches the two conventional roots rather than comparing
    /// against *this* device's home — which would never match.
    private static func abbreviatedPath(_ cwd: String?) -> String? {
        guard let cwd = nonEmpty(cwd) else { return nil }
        for root in ["/Users/", "/home/"] {
            guard cwd.hasPrefix(root) else { continue }
            let rest = cwd.dropFirst(root.count)
            guard let slash = rest.firstIndex(of: "/") else { return "~" }
            return "~" + rest[slash...]
        }
        return cwd
    }

    private static func firstLine(of text: String?) -> String? {
        lines(of: text)?.first
    }

    private static func lastLine(of text: String?) -> String? {
        lines(of: text)?.last
    }

    private static func lines(of text: String?) -> [String]? {
        guard let text = nonEmpty(text) else { return nil }
        let lines = text
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return lines.isEmpty ? nil : lines
    }

    private static func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }

    // MARK: - Markdown Title Cleaning

    /// Strips leading markdown headings (`# `, `## `, etc.) and emphasis wrappers
    /// (`**`, `*`, `__`, `_`, `` ` ``, `~~`) from derived card titles so raw
    /// formatting syntax does not leak into the inbox view.
    public static func cleanMarkdownTitle(_ text: String) -> String {
        var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }

        // Strip leading markdown headings: #, ##, ###, etc.
        if trimmed.hasPrefix("#") {
            let withoutHashes = trimmed.drop(while: { $0 == "#" })
            trimmed = withoutHashes.trimmingCharacters(in: .whitespaces)
        }

        // Strip blockquote markers if present
        while trimmed.hasPrefix(">") {
            trimmed = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
        }

        // Strip matching pairs of wrapping emphasis / formatting
        let wrappers = ["***", "___", "**", "__", "*", "_", "```", "`", "~~"]
        var changed = true
        while changed {
            changed = false
            for w in wrappers {
                if trimmed.count >= w.count * 2 && trimmed.hasPrefix(w) && trimmed.hasSuffix(w) {
                    trimmed = String(trimmed.dropFirst(w.count).dropLast(w.count)).trimmingCharacters(in: .whitespaces)
                    changed = true
                    break
                }
            }
        }

        // Strip any remaining dangling leading/trailing emphasis markers
        for w in wrappers {
            if trimmed.hasPrefix(w) {
                trimmed = String(trimmed.dropFirst(w.count)).trimmingCharacters(in: .whitespaces)
            }
            if trimmed.hasSuffix(w) {
                trimmed = String(trimmed.dropLast(w.count)).trimmingCharacters(in: .whitespaces)
            }
        }

        return trimmed
    }

    // MARK: - Host Shortening

    /// Shortens machine titles so the footer subtitle never truncates the state
    /// on iPhone screen widths (e.g. "Arnabs-Mac-Studio" -> "Studio").
    public static func shortHostTitle(_ title: String) -> String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return trimmed }

        // Hyphenated names e.g. "Arnabs-Mac-Studio" -> "Studio"
        let parts = trimmed.split(separator: "-").map(String.init)
        if parts.count > 1, let last = parts.last, !last.isEmpty, last.count <= 10 {
            return last
        }

        // Space-separated names containing "Studio" e.g. "Arnab's Mac Studio" -> "Studio"
        if trimmed.localizedCaseInsensitiveContains("Studio") {
            return "Studio"
        }

        return trimmed
    }
}

private extension String {
    /// Uppercases only the first character — `capitalized` would title-case
    /// every word and turn "needs approval" into "Needs Approval".
    var capitalizedFirst: String {
        guard let first else { return self }
        return first.uppercased() + dropFirst()
    }
}
