import Foundation
import Testing
@testable import DroverKit

// Tests for TranscriptItem.group — the view-layer fold that coalesces
// consecutive thinking messages into a single collapsible run.

private func thinking(_ id: String, seq: Int, text: String = "hmm") -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .assistantOutput, text: text,
                   payload: ["thinking": .bool(true)])
}

private func output(_ id: String, seq: Int) -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .assistantOutput, text: "answer")
}

private func thinkingTokens(_ id: String, seq: Int, estimated: Int) -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .status, text: "thinking_tokens",
                   payload: ["subtype": .string("thinking_tokens"),
                             "estimated_tokens": .number(Double(estimated))])
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
        #expect(items == [.thinkingRun([t1, t2], estimatedTokens: nil), .message(answer)])
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
        #expect(items == [.thinkingRun([t1], estimatedTokens: nil), .message(tool),
                          .thinkingRun([t2], estimatedTokens: nil)])
        #expect(items[0].id != items[2].id)
    }

    @Test func trailingRunIsFlushed() {
        let t1 = thinking("t1", seq: 2)
        let items = TranscriptItem.group([output("a", seq: 1), t1])
        #expect(items.last == .thinkingRun([t1], estimatedTokens: nil))
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

    @Test func thinkingTokensNeverRenderAsTheirOwnRow() {
        let t1 = thinking("t1", seq: 1)
        let tok = thinkingTokens("k1", seq: 2, estimated: 150)
        let items = TranscriptItem.group([t1, tok])
        #expect(items == [.thinkingRun([t1], estimatedTokens: 150)])
    }

    @Test func thinkingTokensDoNotBreakARun() {
        let t1 = thinking("t1", seq: 1)
        let tok = thinkingTokens("k1", seq: 2, estimated: 50)
        let t2 = thinking("t2", seq: 3)
        let items = TranscriptItem.group([t1, tok, t2])
        #expect(items == [.thinkingRun([t1, t2], estimatedTokens: 50)])
    }

    @Test func thinkingTokensKeepTheMaxAcrossTheRun() {
        // estimated_tokens is a running total; out-of-order deltas must not
        // lower the number already reached.
        let t1 = thinking("t1", seq: 1)
        let items = TranscriptItem.group([
            t1,
            thinkingTokens("k1", seq: 2, estimated: 50),
            thinkingTokens("k2", seq: 3, estimated: 1_200),
            thinkingTokens("k3", seq: 4, estimated: 900),
        ])
        #expect(items == [.thinkingRun([t1], estimatedTokens: 1_200)])
    }

    @Test func thinkingTokensAfterARunClosesAttachToThatRun() {
        let t1 = thinking("t1", seq: 1)
        let answer = output("a", seq: 2)
        let tok = thinkingTokens("k1", seq: 3, estimated: 700)
        let items = TranscriptItem.group([t1, answer, tok])
        #expect(items == [.thinkingRun([t1], estimatedTokens: 700), .message(answer)])
    }

    @Test func thinkingTokensWithNoRunAtAllAreDropped() {
        let tok = thinkingTokens("k1", seq: 1, estimated: 500)
        #expect(TranscriptItem.group([tok]).isEmpty)
        #expect(TranscriptItem.latestRowID(of: [tok]) == nil)
    }

    @Test func thinkingFlagOnlyCountsForAssistantOutput() {
        // A status/tool message carrying thinking:true (defensive) must not
        // be folded into a run.
        let odd = HarnessMessage(id: "s", seq: 1, type: .status,
                                 payload: ["thinking": .bool(true)])
        #expect(TranscriptItem.group([odd]) == [.statusRun([odd])])
        #expect(!odd.isThinking)
    }
}

@Suite struct StatusFoldingTests {
    private func status(_ id: String, seq: Int, _ text: String) -> HarnessMessage {
        HarnessMessage(id: id, seq: seq, type: .status, text: text,
                       payload: ["subtype": .string(text)])
    }

    @Test func consecutiveStatusMessagesCollapseIntoOneRun() {
        let s1 = status("s1", seq: 1, "hook_started")
        let s2 = status("s2", seq: 2, "hook_response")
        let s3 = status("s3", seq: 3, "init")
        #expect(TranscriptItem.group([s1, s2, s3]) == [.statusRun([s1, s2, s3])])
    }

    @Test func aNonStatusMessageBreaksTheRun() {
        let s1 = status("s1", seq: 1, "hook_started")
        let answer = output("a", seq: 2)
        let s2 = status("s2", seq: 3, "init")
        #expect(TranscriptItem.group([s1, answer, s2])
                == [.statusRun([s1]), .message(answer), .statusRun([s2])])
    }

    @Test func statusRunIdentityIsItsFirstMessage() {
        let s1 = status("s1", seq: 1, "hook_started")
        let s2 = status("s2", seq: 2, "init")
        #expect(TranscriptItem.group([s1]).first?.id == "s1")
        #expect(TranscriptItem.group([s1, s2]).first?.id == "s1")
    }

    @Test func latestRowIDTargetsTheRunStartForAStatusTail() {
        let answer = output("a", seq: 1)
        let s1 = status("s1", seq: 2, "hook_started")
        let s2 = status("s2", seq: 3, "init")
        #expect(TranscriptItem.latestRowID(of: [answer, s1, s2]) == "s1")
    }

    @Test func aStatusMessageEndsAThinkingRun() {
        let t1 = thinking("t1", seq: 1)
        let s1 = status("s1", seq: 2, "init")
        #expect(TranscriptItem.group([t1, s1])
                == [.thinkingRun([t1], estimatedTokens: nil), .statusRun([s1])])
    }

    @Test func thinkingTokensAreNeverPartOfAStatusRun() {
        let tok = thinkingTokens("k1", seq: 1, estimated: 50)
        let s1 = status("s1", seq: 2, "init")
        #expect(TranscriptItem.group([tok, s1]) == [.statusRun([s1])])
    }
}

@Suite struct StepPairingTests {
    private func action(_ seq: Int, id toolID: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolAction, role: "assistant",
                       text: "Bash", payload: ["tool": .string("Bash"),
                                               "tool_use_id": .string(toolID)])
    }
    private func result(_ seq: Int, id toolID: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolResult, role: "tool",
                       text: "ok", payload: ["tool_use_id": .string(toolID)])
    }

    @Test func pairsActionWithItsResult() {
        let a = action(1, id: "t1"), r = result(2, id: "t1")
        let items = TranscriptItem.group([a, r])
        #expect(items == [.stepRun([ToolStep(action: a, result: r)])])
    }

    @Test func stepRowIDIsStableWhenResultAttaches() {
        let a = action(1, id: "t1"), r = result(2, id: "t1")
        #expect(TranscriptItem.group([a]).last?.id == a.id)
        #expect(TranscriptItem.group([a, r]).last?.id == a.id)
    }

    @Test func pairsAcrossInterveningMessages() {
        let a = action(1, id: "t1")
        let thinking = HarnessMessage(seq: 2, type: .assistantOutput,
                                      text: "hm", payload: ["thinking": .bool(true)])
        let r = result(3, id: "t1")
        let items = TranscriptItem.group([a, thinking, r])
        #expect(items.count == 2)
        #expect(items[0] == .stepRun([ToolStep(action: a, result: r)]))
    }

    @Test func unmatchedResultStaysAMessage() {
        let r = result(1, id: "orphan")
        #expect(TranscriptItem.group([r]) == [.message(r)])
    }

    @Test func actionWithoutToolUseIDStaysAMessage() {
        let bare = HarnessMessage(seq: 1, type: .toolAction, text: "Bash")
        #expect(TranscriptItem.group([bare]) == [.message(bare)])
    }

    @Test func latestRowIDTargetsStepRowWhenResultIsNewest() {
        let a = action(1, id: "t1")
        let out = HarnessMessage(seq: 2, type: .assistantOutput, text: "mid")
        let r = result(3, id: "t1")
        #expect(TranscriptItem.latestRowID(of: [a, out, r]) == a.id)
    }
}
