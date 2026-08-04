import Testing
import NexusKit
@testable import Drover

struct StepCardTests {
    @Test func commandInputTitlesStepForAnyTool() {
        let action = HarnessMessage.fixture(
            seq: 1,
            type: .toolAction,
            text: "Bash",
            payload: [
                "tool": .string("Bash"),
                "input": .object(["command": .string("swift test\npytest")]),
            ])

        #expect(StepCardPresentation.title(for: action) == "swift test")
    }

    @Test func errorStatusMarksStepFailed() {
        let result = HarnessMessage.fixture(
            seq: 2,
            type: .toolResult,
            payload: ["status": .string("error")])

        #expect(StepCardPresentation.status(for: result).failed == true)
        #expect(StepCardPresentation.status(for: result).label == "failed")
    }
}
