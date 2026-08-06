import Foundation
import Testing
@testable import DroverKit

/// The regression this suite exists for: `segment` parsed inline markdown
/// only, so every block construct — tables, lists, quotes, rules — reached
/// the screen as its literal source characters. Switching the parser to
/// `.full` is *not* the fix; Foundation strips the delimiters and newlines
/// and `Text` ignores presentation intents, so a table becomes one run-on
/// string. These are parsed here instead, following the ATX-heading
/// precedent already in the file.
@Suite struct DisplayBlocksMarkdownTests {
    // MARK: - Tables

    @Test func twoColumnTableParsesAndPrefersStacking() {
        let blocks = DisplayBlock.segment("""
        | Setting | Value |
        |---------|-------|
        | Reconnect delay | 30s cap |
        | Heartbeat | 5s |
        """)

        guard case .table(let table) = blocks.first else { return #expect(Bool(false)) }
        #expect(table.headers == ["Setting", "Value"])
        #expect(table.rows == [["Reconnect delay", "30s cap"], ["Heartbeat", "5s"]])
        #expect(table.prefersStackedLayout, "a two-column table on a phone is a definition list")
    }

    @Test func wideTableBecomesAScrollArtifact() {
        let blocks = DisplayBlock.segment("""
        | host | sessions | waiting | p95 turn | uptime |
        | --- | --- | --- | --- | --- |
        | mac-mini | 4 | 3 | 42s | 6d |
        """)

        guard case .table(let table) = blocks.first else { return #expect(Bool(false)) }
        #expect(table.columnCount == 5)
        #expect(table.prefersStackedLayout == false)
    }

    /// Alignment colons are legal GFM and must not stop the row being read as
    /// a delimiter.
    @Test func alignmentColonsStillDelimitATable() {
        let blocks = DisplayBlock.segment("""
        | left | right |
        |:-----|------:|
        | a | b |
        """)

        guard case .table = blocks.first else { return #expect(Bool(false)) }
    }

    /// A pipe in a sentence is not a table — without the delimiter row this
    /// has to stay prose, or ordinary shell output would start rendering as
    /// tables.
    @Test func proseContainingAPipeStaysProse() {
        let blocks = DisplayBlock.segment("Run `git log | head -5` to check.")

        guard case .text = blocks.first else { return #expect(Bool(false)) }
        #expect(blocks.count == 1)
    }

    @Test func raggedRowsAreNormalizedToTheHeaderWidth() {
        let table = TableBlock(headers: ["a", "b", "c"], rows: [["1"], ["1", "2", "3", "4"]])

        #expect(table.normalizedRows == [["1", "", ""], ["1", "2", "3"]])
    }

    @Test func tsvCarriesTheHeaderAndEveryRow() {
        let table = TableBlock(headers: ["host", "n"], rows: [["mac", "4"]])

        #expect(table.tsv == "host\tn\nmac\t4")
    }

    // MARK: - Lists

    @Test func bulletsParseWithOneLevelOfNesting() {
        let blocks = DisplayBlock.segment("""
        - socket close — retried silently
          - with jittered backoff
        - auth rejection — surfaces in the blocked slot
        """)

        guard case .list(let list) = blocks.first else { return #expect(Bool(false)) }
        #expect(list.items.count == 3)
        #expect(list.items.map(\.depth) == [0, 1, 0])
        #expect(list.items.allSatisfy { $0.ordinal == nil })
        #expect(String(list.items[1].content.characters) == "with jittered backoff")
    }

    @Test func orderedListsKeepTheirNumbers() {
        let blocks = DisplayBlock.segment("""
        1. Rebase onto main.
        2. Re-run the daemon suite.
        3. Push and request review.
        """)

        guard case .list(let list) = blocks.first else { return #expect(Bool(false)) }
        #expect(list.items.map(\.ordinal) == [1, 2, 3])
    }

    /// Markers are drawn from the model, so the literal characters must not
    /// survive into the content.
    @Test func listMarkersAreStrippedFromTheContent() {
        let blocks = DisplayBlock.segment("- first bullet")

        guard case .list(let list) = blocks.first else { return #expect(Bool(false)) }
        #expect(String(list.items[0].content.characters) == "first bullet")
    }

    /// A bare dash is a thematic break, not an empty list item.
    @Test func aBareDashDoesNotStartAList() {
        let blocks = DisplayBlock.segment("Some prose\n\n---\n\nmore prose")

        #expect(blocks.contains(.rule))
    }

    // MARK: - Quotes and rules

    @Test func consecutiveQuoteLinesJoinIntoOneBlock() {
        let blocks = DisplayBlock.segment("""
        > force-with-lease still fails
        > if the remote moved after your last fetch.
        """)

        guard case .quote(let quote) = blocks.first else { return #expect(Bool(false)) }
        #expect(blocks.count == 1, "a wrapped quotation is one accent bar, not two")
        #expect(String(quote.characters).contains("force-with-lease still fails"))
    }

    @Test func thematicBreaksParseInEveryMarker() {
        for source in ["---", "***", "___", "- - -"] {
            #expect(DisplayBlock.segment("a\n\n\(source)\n\nb").contains(.rule), "\(source)")
        }
    }

    // MARK: - Composition

    /// The whole point: a real answer mixes a heading, prose, a table, a list
    /// and a code block, and has to come out as five blocks in order rather
    /// than one pile of text.
    @Test func aRealAnswerSegmentsIntoOrderedBlocks() {
        let blocks = DisplayBlock.segment("""
        ## What changed

        The daemon dropped its socket without clearing the queue.

        | Setting | Value |
        |---|---|
        | Backoff cap | 30s |

        - queue drained on close
        - backoff resets on heartbeat

        ```swift
        self._backoff = 1.0
        ```
        """)

        #expect(blocks.count == 5)
        guard case .heading(let level, _) = blocks[0] else { return #expect(Bool(false)) }
        #expect(level == 2)
        guard case .text = blocks[1] else { return #expect(Bool(false)) }
        guard case .table = blocks[2] else { return #expect(Bool(false)) }
        guard case .list = blocks[3] else { return #expect(Bool(false)) }
        guard case .code(let language, _) = blocks[4] else { return #expect(Bool(false)) }
        #expect(language == "swift")
    }

    /// Fences still win: a table drawn inside a code block is code.
    @Test func fencedContentIsNeverReparsedAsBlocks() {
        let blocks = DisplayBlock.segment("""
        ```
        | a | b |
        |---|---|
        - not a list
        ```
        """)

        #expect(blocks.count == 1)
        guard case .code = blocks[0] else { return #expect(Bool(false)) }
    }
}
