import Foundation
import Testing
@testable import DroverKit

// Shared fixtures/factories reused across DroverKit test files.
// Real wire shapes captured from the deployed backend.

/// Root suite for every test that installs `MockURLProtocol.handler`. The
/// handler is a single global slot, and Swift Testing runs suites from
/// different files concurrently — so a request issued by one suite can land
/// in another suite's handler (a bodyless auth-poll GET arriving in a POST
/// handler crashes its `try!` body parse). `.serialized` applies recursively,
/// so nesting every handler-using suite here keeps them mutually exclusive.
@Suite(.serialized) enum MockNetworkTests {}

#if !SWIFT_PACKAGE
private final class DroverKitTestBundleToken {}
#endif

/// SwiftPM keeps copied fixtures under a `Fixtures` directory, while the
/// XcodeGen unit-test target copies the same JSON files into its bundle root.
func droverKitFixtureURL(_ name: String, withExtension extensionName: String = "json") -> URL? {
    #if SWIFT_PACKAGE
    Bundle.module.url(
        forResource: name, withExtension: extensionName, subdirectory: "Fixtures"
    )
    #else
    Bundle(for: DroverKitTestBundleToken.self).url(
        forResource: name, withExtension: extensionName
    )
    #endif
}

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
    static func fixture(id: String = "fixture-session", status: String, awaiting: String?, hostID: String = "fixture-host") -> SessionSummary {
        SessionSummary(
            id: id,
            hostID: hostID,
            harness: "shell",
            mode: "pty",
            status: status,
            awaiting: awaiting,
            cwd: nil,
            lastActivity: nil
        )
    }
}

extension HostSummary {
    static func fixture(
        id: String = "fixture-host",
        displayName: String = "Fixture Host",
        status: String = "online",
        connectionKind: String = "direct",
        lastSeenAt: Date? = nil,
        harnesses: [String] = ["claude-code"]
    ) -> HostSummary {
        HostSummary(
            id: id,
            displayName: displayName,
            status: status,
            connectionKind: connectionKind,
            lastSeenAt: lastSeenAt,
            harnesses: harnesses
        )
    }
}

/// Fleet-shaped snapshot: one healthy direct host, one stale direct host,
/// one offline relay host — sessions spread across them plus one session
/// on a host the hub no longer lists ("ghost-host").
let fleetSnapshotJSON = Data("""
{
  "hosts": [
    {"host_id": "mac-mini", "status": "online", "connection_kind": "direct",
     "capabilities": {"display_name": "Mac Mini",
                      "harnesses": [{"name": "claude-code", "enabled": true}]}},
    {"host_id": "nas", "status": "stale", "connection_kind": "direct",
     "last_seen_at": "2026-07-30 09:00:00+00:00", "stale_after_seconds": 45,
     "capabilities": {"display_name": "NAS",
                      "harnesses": [{"name": "claude-code", "enabled": true}]}},
    {"host_id": "work-laptop", "status": "offline", "connection_kind": "relay",
     "last_seen_at": "2026-07-30 08:30:00+00:00",
     "capabilities": {"display_name": "Work Laptop",
                      "harnesses": [{"name": "claude-code", "enabled": true}]}}
  ],
  "sessions": [
    {"session_id": "mac-running", "host_id": "mac-mini", "harness": "claude-code",
     "mode": "structured", "status": "running", "awaiting": null,
     "cwd": "/tmp/a", "last_activity": "2026-07-30 10:10:00+00:00"},
    {"session_id": "mac-input", "host_id": "mac-mini", "harness": "claude-code",
     "mode": "structured", "status": "running", "awaiting": "input",
     "cwd": "/tmp/b", "last_activity": "2026-07-30 10:00:00+00:00"},
    {"session_id": "nas-done", "host_id": "nas", "harness": "shell",
     "mode": "pty", "status": "completed", "awaiting": null,
     "cwd": "/tmp/c", "last_activity": "2026-07-30 09:00:00+00:00"},
    {"session_id": "ghost-running", "host_id": "ghost-host", "harness": "codex",
     "mode": "structured", "status": "running", "awaiting": null,
     "cwd": "/tmp/d", "last_activity": "2026-07-30 10:05:00+00:00"}
  ],
  "cwd_suggestions": []
}
""".utf8)

// MARK: - DroverClient test factory (Task 4+)

/// Shared `DroverClient` factory wired to `MockURLProtocol` so Tasks 4-10 all
/// build clients the same way instead of redefining this per-file.
func client() -> DroverClient {
    DroverClient(config: ServerConfig(urlString: "http://test.local:7080")!,
                token: "test-token", session: MockURLProtocol.session())
}

// MARK: - HarnessMessage/ChatModel test factories (Task 7+)

extension HarnessMessage {
    static func fixture(
        seq: Int, type: MessageType, text: String = "",
        payload: [String: JSONValue] = [:]
    ) -> HarnessMessage {
        HarnessMessage(seq: seq, type: type, text: text, payload: payload)
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
