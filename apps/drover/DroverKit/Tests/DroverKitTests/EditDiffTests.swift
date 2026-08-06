import XCTest
@testable import DroverKit

final class EditDiffTests: XCTestCase {

    private func editMessage(
        type: MessageType = .toolAction, tool: String = "Edit",
        input: [String: JSONValue]
    ) -> HarnessMessage {
        .fixture(seq: 1, type: type,
                 payload: ["tool": .string(tool), "input": .object(input)])
    }

    func testEditPayloadProducesOneHunk() throws {
        let diff = try XCTUnwrap(EditDiff(message: editMessage(input: [
            "file_path": .string("/tmp/a.swift"),
            "old_string": .string("let x = 1\nlet y = 2"),
            "new_string": .string("let x = 3"),
        ])))
        XCTAssertEqual(diff.filePath, "/tmp/a.swift")
        XCTAssertEqual(diff.hunks.count, 1)
        XCTAssertEqual(diff.hunks[0].removed, ["let x = 1", "let y = 2"])
        XCTAssertEqual(diff.hunks[0].added, ["let x = 3"])
    }

    func testApprovalPromptIsAccepted() {
        XCTAssertNotNil(EditDiff(message: editMessage(
            type: .approvalPrompt,
            input: ["old_string": .string("a"), "new_string": .string("b")])))
    }

    func testMultiEditProducesHunkPerEdit() throws {
        let diff = try XCTUnwrap(EditDiff(message: editMessage(tool: "MultiEdit", input: [
            "file_path": .string("/tmp/b.py"),
            "edits": .array([
                .object(["old_string": .string("a"), "new_string": .string("b")]),
                .object(["old_string": .string("c"), "new_string": .string("d")]),
            ]),
        ])))
        XCTAssertEqual(diff.hunks.count, 2)
        XCTAssertEqual(diff.hunks[1].removed, ["c"])
    }

    func testUnknownToolReturnsNil() {
        XCTAssertNil(EditDiff(message: editMessage(
            tool: "Bash", input: ["command": .string("ls")])))
    }

    func testMissingKeysReturnNil() {
        XCTAssertNil(EditDiff(message: editMessage(input: ["old_string": .string("a")])))
    }

    func testNonToolMessageTypeReturnsNil() {
        XCTAssertNil(EditDiff(message: .fixture(seq: 1, type: .assistantOutput)))
    }

    func testEmptyMultiEditReturnsNil() {
        XCTAssertNil(EditDiff(message: editMessage(
            tool: "MultiEdit", input: ["edits": .array([])])))
    }

    func testMultiEditWithOneMalformedEditReturnsNil() {
        XCTAssertNil(EditDiff(message: editMessage(tool: "MultiEdit", input: [
            "edits": .array([
                .object(["old_string": .string("a"), "new_string": .string("b")]),
                .object(["old_string": .string("c")]),
            ]),
        ])))
    }

    func testDiffLinesInterleaveHunksWithSeparators() throws {
        let diff = try XCTUnwrap(EditDiff(message: editMessage(tool: "MultiEdit", input: [
            "edits": .array([
                .object(["old_string": .string("a"), "new_string": .string("b")]),
                .object(["old_string": .string("c"), "new_string": .string("d")]),
            ]),
        ])))
        let lines = diff.diffLines
        XCTAssertEqual(lines.map(\.kind),
                       [.remove, .add, .hunk, .remove, .add])
        XCTAssertEqual(lines[0].text, "- a")
        XCTAssertEqual(lines[1].text, "+ b")
    }

    func testEmptyOldStringYieldsNoRemovedLines() throws {
        let diff = try XCTUnwrap(EditDiff(message: editMessage(input: [
            "old_string": .string(""), "new_string": .string("new"),
        ])))
        XCTAssertEqual(diff.hunks[0].removed, [])
        XCTAssertEqual(diff.hunks[0].added, ["new"])
    }
}
