import Foundation
import Testing
@testable import DroverKit

/// Covers the incremental rewrite of `pendingApproval` and the version-keyed
/// caches behind `items` / `latestRowID` / `artifacts` / `contextGauge`.
///
/// The old implementation rescanned the whole transcript on every appended
/// message and on every read; these tests pin the behaviour that had to
/// survive being made incremental.
@MainActor
@Suite struct ChatModelDerivedStateTests {
    private func prompt(_ seq: Int, request: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .approvalPrompt, text: "Bash",
                       payload: ["request_id": .string(request), "tool": .string("Bash")])
    }

    private func response(_ seq: Int, request: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .approvalResponse, text: "allow",
                       payload: ["request_id": .string(request)])
    }

    private func output(_ seq: Int, _ text: String = "hi") -> HarnessMessage {
        HarnessMessage(seq: seq, type: .assistantOutput, text: text)
    }

    // MARK: - Approvals

    @Test func aPromptBecomesPendingAndItsResponseClearsIt() {
        let model = ChatModel.fixture()

        model.ingest(.message(prompt(1, request: "r1")))
        #expect(model.pendingApproval?.seq == 1)

        model.ingest(.message(response(2, request: "r1")))
        #expect(model.pendingApproval == nil)
    }

    @Test func theHighestSeqUnansweredPromptWins() {
        let model = ChatModel.fixture()

        model.ingest(.message(prompt(1, request: "r1")))
        model.ingest(.message(prompt(2, request: "r2")))
        #expect(model.pendingApproval?.seq == 2)

        // Answering the newer one falls back to the older, still-open prompt
        // rather than clearing the slot.
        model.ingest(.message(response(3, request: "r2")))
        #expect(model.pendingApproval?.seq == 1)
    }

    /// A response can only answer a prompt that preceded it. One arriving
    /// first belongs to a prompt this client never saw and must not cancel a
    /// later, genuinely-open prompt with a recycled id.
    @Test func anEarlierResponseDoesNotAnswerALaterPrompt() {
        let model = ChatModel.fixture()

        model.ingest(.message(response(1, request: "r1")))
        model.ingest(.message(prompt(2, request: "r1")))

        #expect(model.pendingApproval?.seq == 2)
    }

    @Test func messagesWithoutARequestIDAreIgnored() {
        let model = ChatModel.fixture()
        model.ingest(.message(prompt(1, request: "r1")))

        model.ingest(.message(HarnessMessage(seq: 2, type: .approvalResponse, text: "allow")))

        #expect(model.pendingApproval?.seq == 1, "a response with no request_id answers nothing")
    }

    /// The seeded-backlog path takes one linear pass instead of the live
    /// per-message path, so it needs its own coverage.
    @Test func aSeededBacklogResolvesApprovalsOnce() {
        let answered = ChatModel.fixture(messages: [
            prompt(1, request: "r1"), response(2, request: "r1"),
        ])
        #expect(answered.pendingApproval == nil)

        let open = ChatModel.fixture(messages: [
            prompt(1, request: "r1"), response(2, request: "r1"), prompt(3, request: "r2"),
        ])
        #expect(open.pendingApproval?.seq == 3)
    }

    // MARK: - Derived caches

    @Test func derivedStateRefreshesWhenAMessageArrives() {
        let model = ChatModel.fixture()
        #expect(model.items.isEmpty)

        model.ingest(.message(output(1, "first")))
        #expect(model.items.count == 1)
        #expect(model.latestRowID != nil)

        model.ingest(.message(output(2, "second")))
        #expect(model.items.count == 2)
    }

    /// Reading the same derivation twice must produce the same value without
    /// depending on a recompute — the view body reads several of these per
    /// render.
    @Test func repeatedReadsAreStable() {
        let model = ChatModel.fixture()
        model.ingest(.message(output(1)))

        #expect(model.items == model.items)
        #expect(model.latestRowID == model.latestRowID)
        #expect(model.artifacts == model.artifacts)
    }

    @Test func versionAdvancesOncePerMessage() {
        let model = ChatModel.fixture()
        let start = model.messagesVersion

        model.ingest(.message(output(1)))
        model.ingest(.message(output(2)))

        #expect(model.messagesVersion == start + 2)
    }

    @Test func historyPageAdvancesVersionExactlyOnce() {
        let model = ChatModel.fixture()
        let page = (1...200).map { output($0, "message \($0)") }
        let start = model.messagesVersion

        model.ingest(.history(page, decodeIssues: []))

        #expect(model.messages.count == 200)
        #expect(model.messagesVersion == start + 1)
        #expect(model.historyPagesMerged == 1)
        #expect(model.lastHistoryMergeDuration != nil)
    }

    @Test func historyMergeSortsDeduplicatesAndRebuildsApprovalState() {
        let model = ChatModel.fixture(messages: [
            output(1, "one"), output(2, "stale two"),
        ])

        model.ingest(.history([
            output(4, "stale four"),
            output(2, "new two"),
            prompt(3, request: "r1"),
            output(4, "new four"),
        ], decodeIssues: []))

        #expect(model.messages.map(\.seq) == [1, 2, 3, 4])
        #expect(model.messages.first(where: { $0.seq == 2 })?.text == "new two")
        #expect(model.messages.first(where: { $0.seq == 4 })?.text == "new four")
        #expect(model.pendingApproval?.seq == 3)

        model.ingest(.history([response(5, request: "r1")], decodeIssues: []))
        #expect(model.pendingApproval == nil)
    }

    /// A non-message event must not invalidate the transcript caches — a
    /// reconnect blip should never cost a re-fold of the whole session.
    @Test func connectionEventsDoNotInvalidateTheTranscript() {
        let model = ChatModel.fixture()
        model.ingest(.message(output(1)))
        let version = model.messagesVersion

        model.ingest(.connection(true))
        model.ingest(.connection(false))

        #expect(model.messagesVersion == version)
    }

    @Test func artifactsSurfaceFromTheIngestedTranscript() {
        let model = ChatModel.fixture()

        model.ingest(.message(HarnessMessage(
            seq: 1, type: .toolAction, text: "Bash",
            payload: ["tool": .string("Bash"), "tool_use_id": .string("t1"),
                      "input": .object(["command": .string("git push origin drover/x")])])))

        #expect(model.artifacts.map(\.value) == ["drover/x"])
    }
}
