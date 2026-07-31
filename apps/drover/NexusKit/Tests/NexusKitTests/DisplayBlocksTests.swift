import XCTest
@testable import NexusKit

final class DisplayBlocksTests: XCTestCase {

    // MARK: - Segmentation

    func testPlainTextIsSingleTextBlock() {
        let blocks = DisplayBlock.segment("hello **world**")
        XCTAssertEqual(blocks.count, 1)
        guard case .text = blocks[0] else { return XCTFail("expected .text, got \(blocks[0])") }
    }

    func testSingleFenceWithLanguage() {
        let blocks = DisplayBlock.segment("before\n```swift\nlet x = 1\n```\nafter")
        XCTAssertEqual(blocks.count, 3)
        guard case .code(let language, let code) = blocks[1] else {
            return XCTFail("expected .code, got \(blocks[1])")
        }
        XCTAssertEqual(language, "swift")
        XCTAssertEqual(code, "let x = 1")
    }

    func testFenceWithoutLanguageHasNilLanguage() {
        let blocks = DisplayBlock.segment("```\nplain\n```")
        XCTAssertEqual(blocks.count, 1)
        guard case .code(let language, let code) = blocks[0] else {
            return XCTFail("expected .code, got \(blocks[0])")
        }
        XCTAssertNil(language)
        XCTAssertEqual(code, "plain")
    }

    func testMultipleFencesKeepProseBetween() {
        let blocks = DisplayBlock.segment("a\n```\none\n```\nb\n```\ntwo\n```")
        XCTAssertEqual(blocks.count, 4)
        guard case .text = blocks[0], case .code = blocks[1],
              case .text = blocks[2], case .code = blocks[3] else {
            return XCTFail("unexpected shapes: \(blocks)")
        }
    }

    func testDiffFenceBecomesDiffBlock() {
        let blocks = DisplayBlock.segment("```diff\n+added\n-removed\n@@ hunk @@\ncontext\n```")
        XCTAssertEqual(blocks.count, 1)
        guard case .diff(let lines) = blocks[0] else {
            return XCTFail("expected .diff, got \(blocks[0])")
        }
        XCTAssertEqual(lines.map(\.kind), [.add, .remove, .hunk, .context])
        XCTAssertEqual(lines[0].text, "+added")
    }

    func testUnterminatedFenceRendersRestAsCode() {
        let blocks = DisplayBlock.segment("prose\n```python\nx = 1\ny = 2")
        XCTAssertEqual(blocks.count, 2)
        guard case .code(let language, let code) = blocks[1] else {
            return XCTFail("expected .code, got \(blocks[1])")
        }
        XCTAssertEqual(language, "python")
        XCTAssertEqual(code, "x = 1\ny = 2")
    }

    func testFenceOnlyMessageHasNoTextBlocks() {
        let blocks = DisplayBlock.segment("```\ncode\n```")
        XCTAssertEqual(blocks.count, 1)
    }

    func testEmptyTextProducesNoBlocks() {
        XCTAssertEqual(DisplayBlock.segment(""), [])
    }

    func testBlankProseBetweenFencesIsDropped() {
        let blocks = DisplayBlock.segment("```\na\n```\n\n```\nb\n```")
        XCTAssertEqual(blocks.count, 2)
    }

    // MARK: - DiffLine classification

    func testDiffLineKinds() {
        XCTAssertEqual(DiffLine(line: "+add").kind, .add)
        XCTAssertEqual(DiffLine(line: "-rm").kind, .remove)
        XCTAssertEqual(DiffLine(line: "@@ -1,2 +1,2 @@").kind, .hunk)
        XCTAssertEqual(DiffLine(line: "+++ b/file").kind, .hunk)
        XCTAssertEqual(DiffLine(line: "--- a/file").kind, .hunk)
        XCTAssertEqual(DiffLine(line: "plain").kind, .context)
        XCTAssertEqual(DiffLine(line: "").kind, .context)
    }
}
