import Foundation
import Testing
@testable import DroverKit

@MainActor
@Suite struct SessionLoadPerformanceTests {
    @Test func productionShapedTranscriptMergesInBoundedPages() {
        let messages = (1...3_316).map { seq in
            let type: MessageType
            let text: String
            let payload: [String: JSONValue]
            switch seq % 4 {
            case 0:
                type = .status
                text = "Working through session history page \(seq)"
                payload = ["awaiting": .string("input")]
            case 1:
                type = .assistantOutput
                text = String(repeating: "Representative prose for cold-load measurement. ", count: 8)
                payload = [:]
            case 2:
                type = .toolAction
                text = "Bash"
                payload = [
                    "tool": .string("Bash"),
                    "tool_use_id": .string("tool-\(seq)"),
                    "input": .object(["command": .string("git status --short")]),
                ]
            default:
                type = .assistantOutput
                text = String(repeating: "thinking ", count: 32)
                payload = ["thinking": .bool(true)]
            }
            return HarnessMessage.fixture(
                seq: seq, type: type, text: text, payload: payload
            )
        }
        let pages = messages.chunked(maxCount: 200)
        let model = ChatModel.fixture()
        let startVersion = model.messagesVersion
        let clock = ContinuousClock()

        let duration = clock.measure {
            for page in pages {
                model.ingest(.history(page, decodeIssues: []))
            }
        }

        #expect(model.messages.count == 3_316)
        #expect(model.messagesVersion == startVersion + pages.count)
        #expect(model.historyPagesMerged == pages.count)
        print("Merged 3,316 production-shaped messages in \(pages.count) pages: \(duration)")
    }
}

private extension Array {
    func chunked(maxCount: Int) -> [[Element]] {
        stride(from: 0, to: count, by: maxCount).map { start in
            Array(self[start..<Swift.min(start + maxCount, count)])
        }
    }
}
