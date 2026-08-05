import Foundation

// MARK: - DiffLine

/// One rendered line of a diff, classified by its unified-diff prefix.
public struct DiffLine: Sendable, Equatable {
    public enum Kind: Sendable, Equatable {
        case add, remove, hunk, context
    }

    public var kind: Kind
    public var text: String

    public init(kind: Kind, text: String) {
        self.kind = kind
        self.text = text
    }

    /// Classify by prefix. File headers (`+++`/`---`) must be checked before
    /// the single-character `+`/`-` prefixes they also match.
    public init(line: String) {
        text = line
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") {
            kind = .hunk
        } else if line.hasPrefix("+") {
            kind = .add
        } else if line.hasPrefix("-") {
            kind = .remove
        } else {
            kind = .context
        }
    }
}

// MARK: - DisplayBlock

/// One renderable segment of a chat message: prose (inline markdown already
/// parsed), a fenced code block, or a fenced diff. Segmented once at decode —
/// see `HarnessMessage.displayBlocks` — never in a view body.
public enum DisplayBlock: Sendable, Equatable {
    case text(AttributedString)
    case code(language: String?, code: String)
    case diff([DiffLine])
    case heading(level: Int, content: AttributedString)

    /// Splits message text at ``` fences. Rules (all covered by tests):
    /// - A line whose trimmed form starts with ``` opens a fence; the rest of
    ///   that line (trimmed) is the language tag, empty → nil.
    /// - Inside a fence, only a line whose trimmed form is exactly ``` closes
    ///   it; anything else (including lines starting with ```) is body.
    /// - An unterminated fence swallows the rest of the text as its body —
    ///   deterministic handling for mid-stream cutoffs.
    /// - `diff`-tagged fences become `.diff` with per-line classification.
    /// - An ATX heading line (`#{1..6} title`) outside a fence becomes a
    ///   `.heading` with the title inline-parsed.
    /// - Prose runs that are blank after trimming produce no block.
    public static func segment(_ text: String) -> [DisplayBlock] {
        var blocks: [DisplayBlock] = []
        var proseLines: [String] = []
        var codeLines: [String] = []
        var language: String?
        var inFence = false

        func flushProse() {
            let joined = proseLines.joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            proseLines = []
            guard !joined.isEmpty else { return }
            blocks.append(.text(parseInlineMarkdown(joined)))
        }

        func flushCode() {
            let body = codeLines.joined(separator: "\n")
            codeLines = []
            if language == "diff" {
                let lines = body.split(separator: "\n", omittingEmptySubsequences: false)
                    .map { DiffLine(line: String($0)) }
                blocks.append(.diff(lines))
            } else {
                blocks.append(.code(language: language, code: body))
            }
            language = nil
        }

        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if !inFence, trimmed.hasPrefix("```") {
                flushProse()
                inFence = true
                let tag = trimmed.dropFirst(3).trimmingCharacters(in: .whitespaces)
                language = tag.isEmpty ? nil : tag
            } else if inFence, trimmed == "```" {
                inFence = false
                flushCode()
            } else if inFence {
                codeLines.append(line)
            } else if let heading = headingBlock(from: trimmed) {
                flushProse()
                blocks.append(heading)
            } else {
                proseLines.append(line)
            }
        }
        if inFence { flushCode() } else { flushProse() }
        return blocks
    }

    /// A prose line of the form `#{1..6} title` becomes a heading block; the
    /// title still gets the inline markdown parse. No space after the hashes,
    /// or 7+ hashes, means it's ordinary prose (matches CommonMark ATX rules).
    private static func headingBlock(from trimmed: String) -> DisplayBlock? {
        let hashes = trimmed.prefix(while: { $0 == "#" })
        guard (1...6).contains(hashes.count) else { return nil }
        let rest = trimmed.dropFirst(hashes.count)
        guard rest.first == " " else { return nil }
        let title = rest.trimmingCharacters(in: .whitespaces)
        guard !title.isEmpty else { return nil }
        return .heading(level: hashes.count, content: parseInlineMarkdown(title))
    }

    /// The same inline-only markdown parse `displayText` has always used —
    /// shared so prose blocks and the legacy whole-message string can't drift.
    static func parseInlineMarkdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options))
            ?? AttributedString(text)
    }
}
