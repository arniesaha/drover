import Foundation
import Testing
@testable import NexusKit

// Tests for TranscriptItem.group — the view-layer fold that coalesces
// consecutive thinking messages into a single collapsible run.

private func thinking(_ id: String, seq: Int, text: String = "hmm") -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .assistantOutput, text: text,
                   payload: ["thinking": .bool(true)])
}

private func output(_ id: String, seq: Int) -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .assistantOutput, text: "answer")
}

@Suite struct TranscriptGroupingTests {
    @Test func passesNonThinkingMessagesThroughUnchanged() {
        let messages = [
            output("a", seq: 1),
            HarnessMessage(id: "b", seq: 2, type: .toolAction),
        ]
        let items = TranscriptItem.group(messages)
        #expect(items == [.message(messages[0]), .message(messages[1])])
    }

    @Test func coalescesConsecutiveThinkingIntoOneRun() {
        let t1 = thinking("t1", seq: 1)
        let t2 = thinking("t2", seq: 2)
        let answer = output("a", seq: 3)
        let items = TranscriptItem.group([t1, t2, answer])
        #expect(items == [.thinkingRun([t1, t2]), .message(answer)])
    }

    @Test func runIdentityIsStableAsChunksArrive() {
        let t1 = thinking("t1", seq: 1)
        let t2 = thinking("t2", seq: 2)
        let before = TranscriptItem.group([t1])
        let after = TranscriptItem.group([t1, t2])
        #expect(before[0].id == after[0].id)
    }

    @Test func nonThinkingMessageEndsARun() {
        let t1 = thinking("t1", seq: 1)
        let tool = HarnessMessage(id: "tool", seq: 2, type: .toolAction)
        let t2 = thinking("t2", seq: 3)
        let items = TranscriptItem.group([t1, tool, t2])
        #expect(items == [.thinkingRun([t1]), .message(tool), .thinkingRun([t2])])
        #expect(items[0].id != items[2].id)
    }

    @Test func trailingRunIsFlushed() {
        let t1 = thinking("t1", seq: 2)
        let items = TranscriptItem.group([output("a", seq: 1), t1])
        #expect(items.last == .thinkingRun([t1]))
    }

    @Test func latestRowIDIsLastMessageForNormalTail() {
        let messages = [thinking("t1", seq: 1), output("a", seq: 2)]
        #expect(TranscriptItem.latestRowID(of: messages) == "a")
    }

    @Test func latestRowIDIsRunStartForThinkingTail() {
        let messages = [output("a", seq: 1), thinking("t1", seq: 2), thinking("t2", seq: 3)]
        #expect(TranscriptItem.latestRowID(of: messages) == "t1")
        #expect(TranscriptItem.latestRowID(of: []) == nil)
    }

    @Test func thinkingFlagOnlyCountsForAssistantOutput() {
        // A status/tool message carrying thinking:true (defensive) must not
        // be folded into a run.
        let odd = HarnessMessage(id: "s", seq: 1, type: .status,
                                 payload: ["thinking": .bool(true)])
        #expect(TranscriptItem.group([odd]) == [.message(odd)])
        #expect(!odd.isThinking)
    }
}
