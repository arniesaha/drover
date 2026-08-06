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

// MARK: - TableBlock

/// A GFM table. Width decides how it renders, so the column count is part of
/// the model rather than a judgement call at the view layer: two or three
/// columns collapse to stacked label/value rows (a two-column table on a
/// phone is a definition list), four or more become a horizontally scrolling
/// artifact with the first column pinned.
public struct TableBlock: Sendable, Equatable {
    public var headers: [String]
    public var rows: [[String]]

    public init(headers: [String], rows: [[String]]) {
        self.headers = headers
        self.rows = rows
    }

    public var columnCount: Int { headers.count }

    /// The threshold is width, not taste: at four columns there is no phone
    /// width where a readable cell still fits.
    public var prefersStackedLayout: Bool { columnCount <= 3 }

    /// Rows padded/truncated to the header width so the view never indexes
    /// past the end of a ragged row.
    public var normalizedRows: [[String]] {
        rows.map { row in
            row.count == columnCount
                ? row
                : row.prefix(columnCount) + Array(repeating: "", count: max(0, columnCount - row.count))
        }
    }

    /// Tab-separated, header row first — what "Copy as TSV" puts on the
    /// pasteboard, and the only lossless way off a phone-shaped table.
    public var tsv: String {
        ([headers] + normalizedRows).map { $0.joined(separator: "\t") }.joined(separator: "\n")
    }
}

// MARK: - ListBlock

/// A bullet or ordered list, flattened with an explicit depth per item.
/// Markers are *drawn* from `ordinal`/`depth` rather than kept as the literal
/// "-" or "1." characters, which is what lets structure survive a long
/// wrapped line.
public struct ListBlock: Sendable, Equatable {
    public struct Item: Sendable, Equatable {
        public var depth: Int
        /// nil for a bullet, the number for an ordered item.
        public var ordinal: Int?
        public var content: AttributedString

        public init(depth: Int, ordinal: Int?, content: AttributedString) {
            self.depth = depth
            self.ordinal = ordinal
            self.content = content
        }
    }

    public var items: [Item]

    public init(items: [Item]) {
        self.items = items
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
    case table(TableBlock)
    case list(ListBlock)
    case quote(AttributedString)
    case rule

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

        // Index-based rather than for-in: a GFM table is only a table if the
        // *next* line is its delimiter, and a list run has to be consumed as a
        // unit, so both need lookahead.
        let allLines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var index = 0
        while index < allLines.count {
            let line = allLines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if !inFence, trimmed.hasPrefix("```") {
                flushProse()
                inFence = true
                let tag = trimmed.dropFirst(3).trimmingCharacters(in: .whitespaces)
                language = tag.isEmpty ? nil : tag
                index += 1
                continue
            }
            if inFence {
                if trimmed == "```" {
                    inFence = false
                    flushCode()
                } else {
                    codeLines.append(line)
                }
                index += 1
                continue
            }
            if let heading = headingBlock(from: trimmed) {
                flushProse()
                blocks.append(heading)
                index += 1
                continue
            }
            // Checked before the rule and the list: a table's delimiter row
            // (`|---|---|`) also looks like a thematic break, and its body
            // rows must not be mistaken for prose.
            if let (table, consumed) = tableBlock(from: allLines, at: index) {
                flushProse()
                blocks.append(.table(table))
                index += consumed
                continue
            }
            if isThematicBreak(trimmed) {
                flushProse()
                blocks.append(.rule)
                index += 1
                continue
            }
            if let (list, consumed) = listBlock(from: allLines, at: index) {
                flushProse()
                blocks.append(.list(list))
                index += consumed
                continue
            }
            if let (quote, consumed) = quoteBlock(from: allLines, at: index) {
                flushProse()
                blocks.append(.quote(quote))
                index += consumed
                continue
            }
            proseLines.append(line)
            index += 1
        }
        if inFence { flushCode() } else { flushProse() }
        return blocks
    }

    // MARK: - Block parsers

    /// A GFM table: a header row of pipe-separated cells, then a delimiter
    /// row of dashes (optionally colon-aligned), then body rows. Anything
    /// less is prose that merely contains a pipe.
    private static func tableBlock(from lines: [String], at start: Int) -> (TableBlock, Int)? {
        guard start + 1 < lines.count else { return nil }
        let header = lines[start].trimmingCharacters(in: .whitespaces)
        let delimiter = lines[start + 1].trimmingCharacters(in: .whitespaces)
        guard header.contains("|"), isTableDelimiter(delimiter) else { return nil }

        let headers = splitRow(header)
        guard !headers.isEmpty else { return nil }

        var rows: [[String]] = []
        var index = start + 2
        while index < lines.count {
            let candidate = lines[index].trimmingCharacters(in: .whitespaces)
            guard candidate.contains("|") else { break }
            rows.append(splitRow(candidate))
            index += 1
        }
        return (TableBlock(headers: headers, rows: rows), index - start)
    }

    private static func isTableDelimiter(_ line: String) -> Bool {
        guard line.contains("-"), line.contains("|") else { return false }
        return line.allSatisfy { "|-: \t".contains($0) }
    }

    /// Splits `| a | b |` into `["a", "b"]`, tolerating the optional leading
    /// and trailing pipes GFM allows.
    private static func splitRow(_ line: String) -> [String] {
        var body = line
        if body.hasPrefix("|") { body.removeFirst() }
        if body.hasSuffix("|") { body.removeLast() }
        return body.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    /// Three or more `-`, `*` or `_` alone on a line.
    private static func isThematicBreak(_ line: String) -> Bool {
        for marker in ["-", "*", "_"] {
            let stripped = line.replacingOccurrences(of: " ", with: "")
            if stripped.count >= 3, stripped.allSatisfy({ String($0) == marker }) {
                return true
            }
        }
        return false
    }

    /// A run of consecutive list items. Indentation maps to depth at two
    /// spaces per level, which covers both the 2- and 4-space conventions
    /// (4 spaces reads as depth 2, the deepest level the design draws).
    private static func listBlock(from lines: [String], at start: Int) -> (ListBlock, Int)? {
        var items: [ListBlock.Item] = []
        var index = start
        while index < lines.count, let item = listItem(lines[index]) {
            items.append(item)
            index += 1
        }
        guard !items.isEmpty else { return nil }
        return (ListBlock(items: items), index - start)
    }

    private static func listItem(_ line: String) -> ListBlock.Item? {
        let indent = line.prefix { $0 == " " }.count
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        let depth = min(indent / 2, 2)

        for marker in ["- ", "* ", "+ "] {
            if trimmed.hasPrefix(marker) {
                let content = String(trimmed.dropFirst(2)).trimmingCharacters(in: .whitespaces)
                guard !content.isEmpty else { return nil }
                return ListBlock.Item(depth: depth, ordinal: nil,
                                      content: parseInlineMarkdown(content))
            }
        }

        let digits = trimmed.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 9 else { return nil }
        let afterDigits = trimmed.dropFirst(digits.count)
        guard afterDigits.hasPrefix(". ") || afterDigits.hasPrefix(") ") else { return nil }
        let content = String(afterDigits.dropFirst(2)).trimmingCharacters(in: .whitespaces)
        guard !content.isEmpty, let ordinal = Int(digits) else { return nil }
        return ListBlock.Item(depth: depth, ordinal: ordinal,
                              content: parseInlineMarkdown(content))
    }

    /// Consecutive `>` lines join into one quote, so a wrapped quotation is
    /// one accent bar rather than one per line.
    private static func quoteBlock(from lines: [String], at start: Int) -> (AttributedString, Int)? {
        var quoted: [String] = []
        var index = start
        while index < lines.count {
            let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix(">") else { break }
            quoted.append(String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces))
            index += 1
        }
        guard !quoted.isEmpty else { return nil }
        let joined = quoted.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !joined.isEmpty else { return nil }
        return (parseInlineMarkdown(joined), index - start)
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
