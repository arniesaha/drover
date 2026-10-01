import Foundation
import Testing
@testable import DroverKit

@MainActor
struct SessionActivityPresentationTests {
    private let start = Date(timeIntervalSince1970: 1_800_000_000)

    private func action(_ seq: Int, _ id: String, tool: String = "Bash") -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolAction, timestamp: start,
                       payload: ["tool_use_id": .string(id), "tool": .string(tool),
                                 "input": .object(["command": .string("swift test")])])
    }

    @Test func activeToolUsesItsCommandAndResultPairing() {
        let model = ChatModel.fixture()
        model.ingest(.connection(true))
        model.ingest(.message(action(1, "a")))
        #expect(model.activity.title == "Running command")
        #expect(model.activity.detail == "swift test")
        #expect(model.activity.activeToolIDs == ["a"])
        #expect(model.activity.startedAt == start)
        model.ingest(.message(HarnessMessage(seq: 2, type: .toolResult,
            timestamp: start.addingTimeInterval(3), payload: ["tool_use_id": .string("a")])))
        #expect(model.activity.activeToolIDs.isEmpty)
        #expect(model.activity.completedSteps == 1)
        #expect(model.activity.isActive)
    }

    @Test func completedTurnStopsAnUnmatchedOldTool() {
        let model = ChatModel.fixture(messages: [action(1, "old"), HarnessMessage(
            seq: 2, type: .status, payload: ["turn_complete": .bool(true)])])
        model.ingest(.connection(true))
        #expect(model.activity.title == "Turn complete")
        #expect(!model.activity.isActive)
        #expect(model.activity.activeToolIDs.isEmpty)
        model.ingest(.message(HarnessMessage(seq: 3, type: .userInput, timestamp: start)))
        #expect(model.activity.title == "Preparing")
        #expect(model.activity.completedSteps == 0)
        #expect(model.activity.activeToolIDs.isEmpty)
    }

    @Test func reconnectAndApprovalOverrideActivityWithoutChangingTranscriptVersion() {
        let model = ChatModel.fixture(messages: [action(1, "a")])
        model.ingest(.connection(true))
        let version = model.messagesVersion
        model.ingest(.connection(false))
        #expect(model.activity.title == "Reconnecting")
        #expect(!model.activity.isActive)
        #expect(model.messagesVersion == version)
        model.ingest(.connection(true))
        #expect(model.activity.isActive)
        model.ingest(.message(HarnessMessage(seq: 2, type: .approvalPrompt,
            payload: ["request_id": .string("r1")])))
        #expect(model.activity.title == "Needs approval")
        #expect(!model.activity.isActive)
    }

    @Test func oldResultsAndUnknownTimestampsDoNotInventProgress() {
        let model = ChatModel.fixture(messages: [HarnessMessage(seq: 1, type: .toolResult)])
        model.ingest(.connection(true))
        #expect(!model.activity.isActive)
        #expect(model.activity.startedAt == nil)
        #expect(model.activity.updatedAt == nil)
    }

    @Test func readToolDoesNotPretendToBeWritingOrStreaming() {
        let model = ChatModel.fixture(messages: [action(1, "read", tool: "Read")])
        model.ingest(.connection(true))
        #expect(model.activity.title == "Reading files")
    }

    @Test func firstToolAfterCompletionStartsANewObservedRun() {
        let model = ChatModel.fixture(messages: [action(1, "old"), HarnessMessage(
            seq: 2, type: .status, payload: ["turn_complete": .bool(true)])])
        model.ingest(.connection(true))
        let nextStart = start.addingTimeInterval(100)
        var next = action(3, "new")
        next.timestamp = nextStart
        model.ingest(.message(next))
        #expect(model.activity.startedAt == nextStart)
        #expect(model.activity.activeToolIDs == ["new"])
    }

    @Test func failedClaudeResultDoesNotReadAsSuccessfulCompletion() {
        let model = ChatModel.fixture(messages: [HarnessMessage(seq: 1, type: .status,
            payload: ["turn_complete": .bool(true), "result": .object(["is_error": .bool(true)])])])
        model.ingest(.connection(true))
        #expect(model.activity.phase == .failed)
        #expect(!model.activity.isActive)
    }

    @Test func parallelToolsKeepOnlyUnfinishedActionsActive() {
        let model = ChatModel.fixture(messages: [action(1, "a"), action(2, "b", tool: "Read")])
        model.ingest(.connection(true))
        model.ingest(.message(HarnessMessage(seq: 3, type: .toolResult,
            payload: ["tool_use_id": .string("b")])))
        #expect(model.activity.activeToolIDs == ["a"])
        #expect(model.activity.title == "Running command")
        #expect(model.activity.completedSteps == 1)
    }

    @Test func deepSeekPollingFailureIsAnErrorEvenWhenTurnComplete() {
        let activity = SessionActivityPresentation(messages: [HarnessMessage(
            seq: 1, type: .status, payload: ["turn_complete": .bool(true),
                "awaiting": .string("input"), "error": .bool(true)])])
        #expect(activity.phase == .failed)
    }

    @Test func initialAwaitingInputIsReadyRatherThanACompletedTurn() {
        let activity = SessionActivityPresentation(messages: [HarnessMessage(
            seq: 1, type: .status, text: "ready", payload: ["awaiting": .string("input")])])
        #expect(activity.phase == .ready)
    }
}
