import Foundation
import Testing
@testable import DroverKit

@Suite struct StepRunGroupingTests {
    private func action(_ seq: Int, id toolID: String, at offset: TimeInterval? = nil) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolAction, role: "assistant", text: "Bash",
                       timestamp: offset.map { Date(timeIntervalSince1970: $0) },
                       payload: ["tool": .string("Bash"), "tool_use_id": .string(toolID)])
    }

    private func result(
        _ seq: Int, id toolID: String, at offset: TimeInterval? = nil,
        exitCode: Int? = nil
    ) -> HarnessMessage {
        var payload: [String: JSONValue] = ["tool_use_id": .string(toolID)]
        if let exitCode { payload["exit_code"] = .number(Double(exitCode)) }
        return HarnessMessage(seq: seq, type: .toolResult, role: "tool", text: "ok",
                              timestamp: offset.map { Date(timeIntervalSince1970: $0) },
                              payload: payload)
    }

    /// The whole point of the fold: six git calls in a row are one line of
    /// transcript, not six.
    @Test func consecutiveStepsCollapseIntoOneRun() {
        let messages = (1...6).flatMap { index in
            [action(index * 2 - 1, id: "t\(index)"), result(index * 2, id: "t\(index)")]
        }

        let items = TranscriptItem.group(messages)

        #expect(items.count == 1)
        guard case .stepRun(let steps) = items[0] else { return #expect(Bool(false)) }
        #expect(steps.count == 6)
        #expect(steps.allSatisfy { $0.result != nil })
    }

    /// A real answer between two tool calls is a boundary — otherwise the
    /// whole turn would fold into a single row and hide the output.
    @Test func aNonStepMessageBreaksTheRun() {
        let items = TranscriptItem.group([
            action(1, id: "t1"), result(2, id: "t1"),
            HarnessMessage(seq: 3, type: .assistantOutput, text: "Pushed."),
            action(4, id: "t2"), result(5, id: "t2"),
        ])

        #expect(items.count == 3)
        guard case .stepRun(let first) = items[0], case .stepRun(let last) = items[2] else {
            return #expect(Bool(false))
        }
        #expect(first.count == 1)
        #expect(last.count == 1)
    }

    /// Actions and results interleave freely on the wire; each result must
    /// still land on its own step.
    @Test func interleavedResultsAttachToTheRightStep() {
        let items = TranscriptItem.group([
            action(1, id: "t1"), action(2, id: "t2"),
            result(3, id: "t2"), result(4, id: "t1"),
        ])

        guard case .stepRun(let steps) = items[0] else { return #expect(Bool(false)) }
        #expect(steps.count == 2)
        #expect(steps[0].result?.seq == 4)
        #expect(steps[1].result?.seq == 3)
    }

    /// A thinking run between tool calls also breaks the step run, but the
    /// late result still has to find its step in the already-closed run.
    @Test func resultAttachesAcrossAClosedRunBoundary() {
        let thinking = HarnessMessage(seq: 2, type: .assistantOutput, text: "hm",
                                      payload: ["thinking": .bool(true)])
        let items = TranscriptItem.group([
            action(1, id: "t1"), thinking, action(3, id: "t2"), result(4, id: "t1"),
        ])

        #expect(items.count == 3)
        guard case .stepRun(let first) = items[0] else { return #expect(Bool(false)) }
        #expect(first[0].result?.seq == 4)
    }

    /// The run keeps the first action's identity so SwiftUI updates the row
    /// in place as later steps and results stream into it.
    @Test func runIdentityIsTheFirstActionAndSurvivesGrowth() {
        let a1 = action(1, id: "t1")
        #expect(TranscriptItem.group([a1]).last?.id == a1.id)
        #expect(TranscriptItem.group([a1, action(2, id: "t2")]).last?.id == a1.id)
        #expect(TranscriptItem.group([a1, action(2, id: "t2"), result(3, id: "t2")]).last?.id == a1.id)
    }

    /// Auto-scroll targets the row a message *rendered into*; a result that
    /// lands in an earlier run must not drag the view back up there.
    @Test func latestRowIDPointsAtTheRunAResultLandedIn() {
        let a = action(1, id: "t1")
        let out = HarnessMessage(seq: 2, type: .assistantOutput, text: "mid")
        #expect(TranscriptItem.latestRowID(of: [a, out, result(3, id: "t1")]) == a.id)
    }
}

@Suite struct FoldSummaryTests {
    private func step(
        _ id: String, start: TimeInterval, end: TimeInterval?, exitCode: Int? = nil
    ) -> ToolStep {
        var payload: [String: JSONValue] = ["tool_use_id": .string(id)]
        if let exitCode { payload["exit_code"] = .number(Double(exitCode)) }
        return ToolStep(
            action: HarnessMessage(seq: 1, type: .toolAction, text: "Bash",
                                   timestamp: Date(timeIntervalSince1970: start),
                                   payload: ["tool_use_id": .string(id)]),
            result: end.map {
                HarnessMessage(seq: 2, type: .toolResult, text: "ok",
                               timestamp: Date(timeIntervalSince1970: $0), payload: payload)
            }
        )
    }

    @Test func cleanRunReportsCountDurationAndOutcome() {
        let steps = [
            step("a", start: 0, end: 3),
            step("b", start: 3, end: 42),
        ]

        #expect(FoldSummary.steps(steps) == "2 steps · 42s · all clean")
    }

    @Test func failuresAreCountedInsteadOfClean() {
        let steps = [
            step("a", start: 0, end: 1),
            step("b", start: 1, end: 2, exitCode: 1),
            step("c", start: 2, end: 3, exitCode: 127),
        ]

        #expect(FoldSummary.steps(steps) == "3 steps · 3.0s · 2 failed")
    }

    /// A step still in flight has not failed — a long command must not flash
    /// as an error while it runs.
    @Test func aRunningStepReportsRunningAndNeverFails() {
        let steps = [step("a", start: 0, end: 1), step("b", start: 1, end: nil)]

        #expect(steps[1].isFailed == false)
        #expect(FoldSummary.steps(steps).hasSuffix("running…"))
    }

    /// Wall-clock across the run, not the sum of the steps — overlapping
    /// tool calls otherwise report a wait longer than the one that happened.
    @Test func durationIsWallClockAcrossOverlappingSteps() {
        let steps = [
            step("a", start: 0, end: 30),
            step("b", start: 0, end: 30),
        ]

        #expect(FoldSummary.steps(steps) == "2 steps · 30s · all clean")
    }

    @Test func durationsReadAtTheRightPrecision() {
        #expect(FoldSummary.duration(0.34) == "0.3s")
        #expect(FoldSummary.duration(3.15) == "3.1s")
        #expect(FoldSummary.duration(42) == "42s")
        #expect(FoldSummary.duration(78) == "1m 18s")
    }

    @Test func thinkingRunReportsDurationAndTokens() {
        let run = [
            HarnessMessage(seq: 1, type: .assistantOutput, text: "a",
                           timestamp: Date(timeIntervalSince1970: 0),
                           payload: ["thinking": .bool(true)]),
            HarnessMessage(seq: 2, type: .assistantOutput, text: "b",
                           timestamp: Date(timeIntervalSince1970: 12),
                           payload: ["thinking": .bool(true)]),
        ]

        #expect(FoldSummary.thinking(run: run, estimatedTokens: 1200, isStreaming: false)
                == "Thought for 12s · 1.2K tokens")
        #expect(FoldSummary.thinking(run: run, estimatedTokens: nil, isStreaming: true)
                == "Thinking…")
    }

    @Test func statusRunSurfacesTheLastUpdate() {
        let run = (1...3).map {
            HarnessMessage(seq: $0, type: .status, text: "event",
                           payload: ["description": .string("indexed \($0) files")])
        }

        #expect(FoldSummary.status(run: run) == "3 status updates · last: event — indexed 3 files")
        #expect(FoldSummary.status(run: [run[0]]) == "event — indexed 1 files")
    }
}
