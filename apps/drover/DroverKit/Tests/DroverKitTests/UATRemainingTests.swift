import Foundation
import Testing
@testable import DroverKit

@Test @MainActor func fleetCountsMatchSessionBuckets() throws {
    let snapshot = try HarnessSnapshot.decode(from: fleetSnapshotJSON)
    let buckets = SessionBuckets(sessions: snapshot.sessions)
    let summary = FleetSummaryPresentation(snapshot: snapshot)
    #expect(summary.needsCount == buckets.needsYou.count)
    #expect(summary.fleetLine.contains("\(buckets.finished.count) finished"))
    #expect(Set(buckets.needsYou.map(\.id)).isDisjoint(with: Set(buckets.finished.map(\.id))))
}

@Test(arguments: [MessageType.status, .raw, .unknown, .toolAction, .toolResult, .error, .approvalResponse])
func historyHidesInternalEvents(type: MessageType) {
    #expect(!HistoryPresentation.isReadable(HarnessMessage(id: "event", seq: 1, type: type, role: "system", text: "internal.event")))
}

@Test func historyShowsConversationOnly() {
    #expect(HistoryPresentation.isReadable(HarnessMessage(id: "user", seq: 1, type: .userInput, role: "user", text: "Hello")))
    #expect(HistoryPresentation.isReadable(HarnessMessage(id: "reply", seq: 2, type: .assistantOutput, text: "Hello back")))
    #expect(!HistoryPresentation.isReadable(HarnessMessage(id: "thinking", seq: 3, type: .assistantOutput, text: "Thinking", payload: ["thinking": .bool(true)])))
    let legacy = HarnessMessage(id: "legacy", seq: 4, type: .assistantOutput, role: "", text: "Readable answer")
    #expect(HistoryPresentation.isReadable(legacy))
    #expect(HistoryPresentation.speaker(for: legacy) == "Assistant")
    #expect(HistoryPresentation.speaker(for: HarnessMessage(id: "user", seq: 1, type: .userInput, text: "Hello")) == "You")
    #expect(!HistoryPresentation.isReadable(HarnessMessage(id: "empty", seq: 4, type: .assistantOutput, text: "  ")))
}

@Test func historyPluralization() {
    #expect(HistoryPresentation.dayRange(1) == "1 day")
    #expect(HistoryPresentation.dayRange(7) == "7 days")
    #expect(HistoryPresentation.footer(1) == "1 session · All loaded")
    #expect(HistoryPresentation.footer(2) == "2 sessions · All loaded")
}

@Test func recentFoldersNameTheProject() {
    #expect(FolderRecentPresentation.name(path: "/src/sample/.worktrees/internal-id") == "sample")
    #expect(FolderRecentPresentation.name(path: "/tmp/.drover/worktrees/internal-id") == "Project worktree")
    #expect(FolderRecentPresentation.name(path: "/src/sample") == "sample")
    let session = SessionSummary(id: "sample", hostID: "host", harness: "codex", mode: "structured", status: "running", awaiting: nil, cwd: "/tmp/.drover/worktrees/internal-id", lastActivity: nil, repoName: "sample")
    #expect(FolderRecentPresentation.name(path: session.cwd!, sessions: [session]) == "sample")
}

@Test(arguments: [
    "drover://example.test?v=1&code=one&code=two",
    "drover://user@example.test?v=1&code=one",
    "drover://example.test/path?v=1&code=one",
    "drover://example.test?v=1&code=one&tls=2",
    "drover://example.test?v=1&code=one&extra=value",
    "drover://example.test:0?v=1&code=one",
    "drover://example.test?v=1&code=one#fragment"
])
func pairingRejectsAmbiguousLinks(link: String) {
    #expect(PairingPayload(scanned: link) == nil)
}

@Test func pairingSupportsIPv6() {
    #expect(PairingPayload(scanned: "drover://[::1]:7080?v=1&code=sample")?.serverURL.absoluteString == "http://[::1]:7080")
}
