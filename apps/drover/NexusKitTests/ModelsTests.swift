import Foundation
import Testing
@testable import NexusKit

@Test func snapshotDecodesLeniently() throws {
    let snap = try HarnessSnapshot.decode(from: snapshotJSON)
    #expect(snap.hosts.count == 1)
    #expect(snap.hosts[0].displayName == "Mac Mini")
    #expect(snap.hosts[0].harnesses.contains("gemini"))
    #expect(snap.sessions.count == 2)  // bogus element skipped, not fatal
    #expect(snap.sessions[0].attention == .needsApproval)
    #expect(snap.sessions[0].isStructured)
    #expect(snap.sessions[0].lastActivity != nil)
    #expect(snap.sessions[1].mode == nil)  // absent on the wire, not defaulted
    #expect(snap.sessions[1].isStructured == false)  // shell harness stays PTY
    #expect(snap.sessions[1].attention == .working)
}

/// Regression: a claude-code session with `mode: null` (or the key entirely
/// absent) is a legacy/pre-field session, not a PTY one — it must still
/// route to the structured Chat screen, not Terminal (where it would attach
/// and immediately end). Exercises the actual decode path, not just the
/// in-memory initializer.
@Test func modeNullOnStructuredCapableHarnessDecodesAsStructured() throws {
    let json = Data("""
    {"hosts": [], "sessions": [
      {"session_id": "s1", "host_id": "h1", "harness": "claude-code",
       "status": "running", "awaiting": null, "cwd": null, "last_activity": null}],
     "cwd_suggestions": []}
    """.utf8)
    let snap = try HarnessSnapshot.decode(from: json)
    #expect(snap.sessions[0].mode == nil)
    #expect(snap.sessions[0].isStructured == true)
}

@Test(arguments: [
    ("structured", "shell", true),
    ("structured", "claude-code", true),
    (nil as String?, "shell", false),
    (nil as String?, "claude-code", true),
    ("pty", "shell", false),
])
func isStructuredDerivation(mode: String?, harness: String, expected: Bool) {
    let session = SessionSummary(
        id: "s", hostID: "h", harness: harness, mode: mode,
        status: "running", awaiting: nil, cwd: nil, lastActivity: nil)
    #expect(session.isStructured == expected)
}

@Test(arguments: [
    ("completed", nil as String?, AttentionState.done),
    ("terminated", "approval", AttentionState.done),  // terminal wins
    ("errored", nil, AttentionState.errored),
    ("running", "approval", AttentionState.needsApproval),
    ("running", "input", AttentionState.needsInput),
    ("running", nil, AttentionState.working),
    ("starting", nil, AttentionState.working),
])
func attentionDerivation(status: String, awaiting: String?, expected: AttentionState) {
    let s = SessionSummary.fixture(status: status, awaiting: awaiting)
    #expect(s.attention == expected)
}

@Test func messagesDecodeLeniently() throws {
    let batch = try MessageBatch.decode(from: messagesJSON)
    #expect(batch.maxSeq == 3)
    #expect(batch.messages.count == 3)  // string element skipped
    #expect(batch.messages[0].type == .userInput)
    #expect(batch.messages[1].type == .approvalPrompt)
    #expect(batch.messages[1].payload["request_id"]?.stringValue == "req-1")
    #expect(batch.messages[2].type == .unknown)  // future type degrades
}

@Test func jsonValueRoundTrip() throws {
    let data = Data(#"{"a": 1, "b": [true, null, "x"], "c": {"d": 2.5}}"#.utf8)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: data)
    #expect(value["a"]?.stringValue == nil)
    #expect(value["c"]?.objectValue?["d"] == .number(2.5))
}
