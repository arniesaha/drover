import Foundation
import Testing
@testable import DroverKit

struct ResultReadReceiptsTests {
    private func result(_ id: String = "gpu", seq: Int = 82, host: String = "gpu") -> SessionSummary {
        var session = SessionSummary.fixture(id: id, status: "running", awaiting: "input", hostID: host)
        session.notificationKind = .finished
        session.recapSourceSeq = 1 // Deliberately stale async recap must NOT suppress a new result.
        session.attentionID = "completed-\(seq)"
        session.lastActivity = Date(timeIntervalSince1970: Double(seq))
        return session
    }

    @Test func pollingRelaunchPartialSnapshotsAndNewSessionNeverReadAResult() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        var working = result(); working.awaiting = nil; working.notificationKind = nil
        receipts.bootstrap([working])
        let completed = result()
        #expect(receipts.unread(in: [completed]).map(\.id) == ["gpu"])
        #expect(receipts.unread(in: []).isEmpty)
        receipts.bootstrap([])
        #expect(ResultReadReceipts(hub: "studio", defaults: defaults).unread(in: [completed]).count == 1)
        #expect(receipts.unread(in: [working, completed]).count == 1)
        #expect(completed.attention == .done)
    }

    @Test func actualVisibleGenerationOnlyAndSecondTurnResurfaces() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        let completed = result()
        receipts.bootstrap([completed])
        let response = HarnessMessage(seq: 82, type: .assistantOutput, text: "Final response")
        let completion = HarnessMessage(id: "completed-82", seq: 83, type: .status,
                                       payload: ["turn_complete": .bool(true)])
        let messages = [response, completion]
        receipts.viewed(sessionID: "gpu", response: HarnessMessage(seq: 40, type: .assistantOutput, text: "Earlier"), messages: messages)
        #expect(receipts.unread(in: [completed]).count == 1)
        receipts.viewed(sessionID: "gpu", response: HarnessMessage(seq: 82, type: .toolResult, text: "Not a response"), messages: messages)
        #expect(receipts.unread(in: [completed]).count == 1)
        receipts.viewed(sessionID: "gpu", response: HarnessMessage(seq: 82, type: .assistantOutput, text: "Final response"), messages: messages)
        #expect(receipts.unread(in: [completed]).isEmpty)
        #expect(receipts.unread(in: [result(seq: 120)]).count == 1)
        // Opening/reading a working turn never acknowledges its future completion.
        let workingMessages = [HarnessMessage(seq: 120, type: .assistantOutput, text: "Streaming")]
        receipts.viewed(sessionID: "gpu", response: workingMessages[0], messages: workingMessages)
        #expect(receipts.unread(in: [result(seq: 120)]).count == 1)
        #expect(ResultReadReceipts(hub: "other", defaults: defaults).unread(in: [completed]).count == 1)
        var question = result(seq: 120); question.notificationKind = .input
        #expect(question.attention == .needsInput)
        #expect(receipts.unread(in: [question]).isEmpty)
    }

    @Test func migrationDoesNotFloodAndDismissIsGenerationSpecific() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        let history = (1...23).map { result("session-\($0)", seq: $0) }
        receipts.bootstrap(history)
        #expect(receipts.unread(in: history).map(\.id) == ["session-23"])
        receipts.dismiss(history.last!)
        #expect(receipts.unread(in: history).isEmpty)
        #expect(receipts.unread(in: [result("session-23", seq: 24)]).count == 1)
    }
    @Test func terminationIsNotAResponseAndDoesNotReplaceActualCompletion() {
        let stop = HarnessMessage(id: "stop", seq: 679, type: .status,
                                  payload: ["session_status": .string("terminated"), "turn_complete": .bool(true)])
        let exit = HarnessMessage(id: "exit", seq: 680, type: .status,
                                  payload: ["exited": .number(143)])
        #expect(ResultReadReceipts.completedResponse(in: [stop, exit]) == nil)
        let response = HarnessMessage(id: "answer", seq: 675, type: .assistantOutput, text: "Actual result")
        #expect(ResultReadReceipts.completedResponse(in: [response, stop, exit]) == nil)
        let complete = HarnessMessage(id: "original", seq: 677, type: .status,
                                      payload: ["turn_complete": .bool(true), "awaiting": .string("input")])
        let messages = [response, complete, stop, exit]
        #expect(ResultReadReceipts.completedResponse(in: messages)?.completion.id == "original")
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        var session = result(); session.attentionID = "original"
        receipts.viewed(sessionID: session.id, response: response, messages: messages)
        #expect(receipts.unread(in: [session]).isEmpty)
        let newTurn = HarnessMessage(seq: 681, type: .userInput, text: "Next task")
        #expect(ResultReadReceipts.completedResponse(in: messages + [newTurn]) == nil)
    }
    @Test func terminalEvidenceDoesNotInventResultsAndRetainsUnreadThroughStop() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        receipts.bootstrap([])
        var stopped = result(); stopped.status = "terminated"; stopped.attentionID = "stop"
        let response = HarnessMessage(seq: 82, type: .assistantOutput, text: "Actual result")
        let completion = HarnessMessage(id: "completed-82", seq: 83, type: .status,
                                       payload: ["turn_complete": .bool(true)])
        let stop = HarnessMessage(id: "stop", seq: 84, type: .status,
                                 payload: ["session_status": .string("terminated"), "turn_complete": .bool(true)])
        #expect(receipts.unread(in: [stopped]).isEmpty)
        receipts.recordResponseEvidence(stopped, messages: [stop])
        #expect(receipts.unread(in: [stopped]).isEmpty)
        #expect(!receipts.needsResponseEvidence(stopped))
        receipts.recordResponseEvidence(stopped, messages: [response, completion, stop])
        #expect(receipts.unread(in: [stopped]).count == 1)
        receipts.viewed(sessionID: stopped.id, response: response, messages: [response, completion, stop])
        #expect(ResultReadReceipts(hub: "studio", defaults: defaults).unread(in: [stopped]).isEmpty)
        stopped.lastActivity = Date(timeIntervalSince1970: 900)
        #expect(receipts.needsResponseEvidence(stopped))
        receipts.recordResponseEvidence(stopped, messages: [response, completion, stop])
        #expect(receipts.unread(in: [stopped]).isEmpty)
    }

    @Test func firstInstallDoesNotPromoteOldTerminalPerHostExample() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let receipts = ResultReadReceipts(hub: "studio", defaults: defaults)
        var old = result("old-nas", host: "nas"); old.status = "terminated"
        receipts.bootstrap([old])
        let response = HarnessMessage(seq: 1, type: .assistantOutput, text: "Old result")
        let completion = HarnessMessage(id: "old-completion", seq: 2, type: .status,
                                       payload: ["turn_complete": .bool(true)])
        receipts.recordResponseEvidence(old, messages: [response, completion])
        #expect(receipts.unread(in: [old]).isEmpty)
        old.lastActivity = Date(timeIntervalSince1970: 900)
        receipts.recordResponseEvidence(old, messages: [response, completion])
        #expect(receipts.unread(in: [old]).isEmpty)
        let nextResponse = HarnessMessage(seq: 10, type: .assistantOutput, text: "New result")
        let nextCompletion = HarnessMessage(id: "new-completion", seq: 11, type: .status,
                                           payload: ["turn_complete": .bool(true)])
        receipts.recordResponseEvidence(old, messages: [nextResponse, nextCompletion])
        #expect(receipts.unread(in: [old]).count == 1)
    }

}
