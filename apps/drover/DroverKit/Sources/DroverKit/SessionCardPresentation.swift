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

    /// `harness · host · state`, quiet. Never repeats what the title says.
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
        projectName = Self.projectName(for: session.cwd)
        isStale = freshness.isStale
        staleNote = freshness.staleNote
        frozenActivityText = freshness.frozenActivityText(for: session.activityDate)

        let attention = session.attention
        let state = Self.statePhrase(attention, species: species)
        let path = Self.abbreviatedPath(session.cwd)
        var subtitleParts = [Self.nonEmpty(harness.name), Self.nonEmpty(hostTitle)].compactMap { $0 }

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
            if let titleText = Self.firstLine(of: session.recap) ?? Self.firstLine(of: session.preview) {
                title = titleText
                isTitlePlaceholder = false
                subtitleParts.append(state)
            } else {
                // With no preview the state *is* the only real information, so
                // it gets promoted to the loud line rather than duplicated into
                // a manufactured one — and then dropped from the subtitle so
                // the row still says exactly one thing.
                title = state.capitalizedFirst
                isTitlePlaceholder = true
            }

        case .terminal:
            sigil = (attention == .working) ? "$" : "‹"
            let verb: Action = (attention == .working) ? .attach : .reopen
            action = freshness.isStale ? nil : verb
            if let lastLine = Self.lastLine(of: session.preview) {
                kicker = path
                title = lastLine
                isTitlePlaceholder = false
                subtitleParts.append(state)
            } else {
                // Nothing has been printed yet, so the working directory is
                // the loudest true thing about this shell. It moves up into
                // the title rather than being repeated as both kicker and
                // title.
                kicker = nil
                title = path ?? "\(harness.name) session"
                isTitlePlaceholder = true
                subtitleParts.append(state)
                subtitleParts.append("no output yet")
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

    /// `/Users/arnab/src/drover` → `~/src/drover`. The hub reports the remote
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
}

private extension String {
    /// Uppercases only the first character — `capitalized` would title-case
    /// every word and turn "needs approval" into "Needs Approval".
    var capitalizedFirst: String {
        guard let first else { return self }
        return first.uppercased() + dropFirst()
    }
}
