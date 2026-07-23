import Foundation
import Testing
@testable import NexusKit

// Shared fixtures/factories reused across NexusKit test files.
// Real wire shapes captured from the deployed backend.

/// Root suite for every test that installs `MockURLProtocol.handler`. The
/// handler is a single global slot, and Swift Testing runs suites from
/// different files concurrently — so a request issued by one suite can land
/// in another suite's handler (a bodyless auth-poll GET arriving in a POST
/// handler crashes its `try!` body parse). `.serialized` applies recursively,
/// so nesting every handler-using suite here keeps them mutually exclusive.
@Suite(.serialized) enum MockNetworkTests {}

let snapshotJSON = Data("""
{"hosts": [{"host_id": "mac-mini", "status": "online",
  "capabilities": {"display_name": "Mac Mini", "harnesses": [
    {"name": "shell", "enabled": true},
    {"name": "claude-code", "enabled": true},
    {"name": "gemini", "enabled": true}]}}],
 "sessions": [
  {"session_id": "harness-1", "host_id": "mac-mini", "harness": "gemini",
   "mode": "structured", "status": "running", "awaiting": "approval",
   "cwd": "/Users/arnabmac/jenny/nexus",
   "last_activity": "2026-07-07T01:23:45.678901+00:00"},
  {"session_id": "harness-2", "host_id": "mac-mini", "harness": "shell",
   "status": "running", "awaiting": null, "cwd": null,
   "last_activity": null},
  {"bogus": true}],
 "cwd_suggestions": [
  {"path": "/Users/arnabmac/jenny/nexus", "source": "recent session", "host_id": "mac-mini"},
  {"path": "/Volumes/M2 1/drover", "source": "favorite"},
  {"path": "/home/arnab/elsewhere", "source": "recent session", "host_id": "nas"}]}
""".utf8)

/// Multi-host snapshot for host-switch tests (Task 8). Kept separate from
/// `snapshotJSON`, which other tests assert has exactly one host.
/// - "nas" does NOT offer mac-mini's default ("claude-code") → switch resets.
/// - "studio" DOES offer "claude-code" → switch preserves the selection.
let multiHostSnapshotJSON = Data("""
{"hosts": [
  {"host_id": "mac-mini", "status": "online",
   "capabilities": {"display_name": "Mac Mini", "harnesses": [
     {"name": "shell", "enabled": true},
     {"name": "claude-code", "enabled": true},
     {"name": "gemini", "enabled": true}]}},
  {"host_id": "nas", "status": "online",
   "capabilities": {"display_name": "NAS", "harnesses": [
     {"name": "shell", "enabled": true},
     {"name": "codex", "enabled": true}]}},
  {"host_id": "studio", "status": "online",
   "capabilities": {"display_name": "Studio", "harnesses": [
     {"name": "claude-code", "enabled": true},
     {"name": "gemini", "enabled": true}]}}],
 "sessions": [],
 "cwd_suggestions": []}
""".utf8)

let messagesJSON = Data("""
{"messages": [
  {"event_id": "harness-event-a", "seq": 1, "type": "user_input",
   "role": "user", "text": "hi", "turn_id": "turn-1",
   "ts": "2026-07-07T01:00:00+00:00", "payload": {}},
  {"event_id": "harness-event-b", "seq": 2, "type": "approval_prompt",
   "role": "system", "text": "approval needed: Bash",
   "payload": {"request_id": "req-1", "tool": "Bash",
               "input": {"command": "ls"}}},
  {"event_id": "harness-event-c", "seq": 3, "type": "sparkle_new_kind",
   "role": "assistant", "text": "??", "payload": {}},
  "not even an object"],
 "max_seq": 3}
""".utf8)

extension SessionSummary {
    static func fixture(id: String = "fixture-session", status: String, awaiting: String?) -> SessionSummary {
        SessionSummary(
            id: id,
            hostID: "fixture-host",
            harness: "shell",
            mode: "pty",
            status: status,
            awaiting: awaiting,
            cwd: nil,
            lastActivity: nil
        )
    }
}

// MARK: - NexusClient test factory (Task 4+)

/// Shared `NexusClient` factory wired to `MockURLProtocol` so Tasks 4-10 all
/// build clients the same way instead of redefining this per-file.
func client() -> NexusClient {
    NexusClient(config: ServerConfig(urlString: "http://test.local:7080")!,
                token: "test-token", session: MockURLProtocol.session())
}

// MARK: - HarnessMessage/ChatModel test factories (Task 7+)

extension HarnessMessage {
    static func fixture(seq: Int, type: MessageType, payload: [String: JSONValue] = [:]) -> HarnessMessage {
        HarnessMessage(seq: seq, type: type, payload: payload)
    }
}

extension ChatModel {
    /// A `ChatModel` with no live stream — `ingest(_:)` drives it directly
    /// in tests, no `MockURLProtocol.handler` needed unless the test also
    /// calls a network action (`sendTurn`/`approve`/etc.).
    static func fixture(messages: [HarnessMessage] = []) -> ChatModel {
        ChatModel(client: client(), sessionID: "fixture-session", initialMessages: messages)
    }
}
