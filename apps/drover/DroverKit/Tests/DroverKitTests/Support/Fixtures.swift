import Foundation
import Testing
@testable import DroverKit

// Shared fixtures/factories reused across DroverKit test files.
// Real wire shapes captured from the deployed backend.

/// Root suite for test suites that exercise network mocks.
/// Suites nested here historically used a process-global handler; network
/// mocks are now scoped per-test via `MockNetwork` and routed by session tokens.
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

/// The v1 matrices current hosts publish for the built-in harnesses, keyed
/// by name. Test data only: `HarnessCapabilitiesTests` pins every entry to the
/// server-generated golden `harness-capabilities-v1.json`, so these cannot
/// drift from what a real host sends.
let builtinV1Matrices: [String: String] = [
    "shell": #"{"schema_version": 1, "harness_id": "shell", "launch_modes": ["pty"], "approvals": false, "interrupt": false, "native_resume": false, "model_catalog": false, "usage": false, "worktree": false, "interactive_auth": false, "turn_preferences": false, "attachments": []}"#,
    "claude-code": #"{"schema_version": 1, "harness_id": "claude-code", "launch_modes": ["structured"], "approvals": true, "interrupt": true, "native_resume": true, "model_catalog": true, "usage": false, "worktree": false, "interactive_auth": true, "turn_preferences": false, "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"]}"#,
    "codex": #"{"schema_version": 1, "harness_id": "codex", "launch_modes": ["structured"], "approvals": false, "interrupt": true, "native_resume": true, "model_catalog": true, "usage": false, "worktree": true, "interactive_auth": true, "turn_preferences": true, "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"]}"#,
    "agy": #"{"schema_version": 1, "harness_id": "agy", "launch_modes": ["structured"], "approvals": false, "interrupt": true, "native_resume": true, "model_catalog": true, "usage": false, "worktree": true, "interactive_auth": true, "turn_preferences": true, "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"]}"#,
    "deepseek-harness": #"{"schema_version": 1, "harness_id": "deepseek-harness", "launch_modes": ["structured"], "approvals": false, "interrupt": true, "native_resume": true, "model_catalog": true, "usage": false, "worktree": true, "interactive_auth": false, "turn_preferences": true, "attachments": ["image/gif", "image/jpeg", "image/png", "image/webp"]}"#,
]

/// One envelope row for a built-in harness, as a current host publishes it.
func v1Row(_ name: String, enabled: Bool = true) -> String {
    #"{"name": "\#(name)", "enabled": \#(enabled), "capabilities": \#(builtinV1Matrices[name]!)}"#
}

/// The same built-in rows decoded, for tests that build `HostSummary` directly.
func v1Offer(_ name: String, enabled: Bool = true) -> HarnessOffer {
    let data = Data(v1Row(name, enabled: enabled).utf8)
    let row = try! JSONDecoder().decode(JSONValue.self, from: data)
    return HarnessOffer(row: row)!
}

let snapshotJSON = Data("""
{"hosts": [{"host_id": "mac-mini", "status": "online",
  "capabilities": {"display_name": "Mac Mini", "harnesses": [
    \(v1Row("shell")),
    \(v1Row("claude-code")),
    \(v1Row("agy"))]}}],
 "sessions": [
  {"session_id": "harness-1", "host_id": "mac-mini", "harness": "agy",
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
     \(v1Row("shell")),
     \(v1Row("claude-code")),
     \(v1Row("agy"))]}},
  {"host_id": "nas", "status": "online",
   "capabilities": {"display_name": "NAS", "harnesses": [
     \(v1Row("shell")),
     \(v1Row("codex"))]}},
  {"host_id": "studio", "status": "online",
   "capabilities": {"display_name": "Studio", "harnesses": [
     \(v1Row("claude-code")),
     \(v1Row("agy"))]}}],
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
            harnesses: harnesses,
            harnessOffers: harnesses.map { v1Offer($0) }
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
                      "harnesses": [\(v1Row("claude-code"))]}},
    {"host_id": "nas", "status": "stale", "connection_kind": "direct",
     "last_seen_at": "2026-07-30 09:00:00+00:00", "stale_after_seconds": 45,
     "capabilities": {"display_name": "NAS",
                      "harnesses": [\(v1Row("claude-code"))]}},
    {"host_id": "work-laptop", "status": "offline", "connection_kind": "relay",
     "last_seen_at": "2026-07-30 08:30:00+00:00",
     "capabilities": {"display_name": "Work Laptop",
                      "harnesses": [\(v1Row("claude-code"))]}}
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

let testRecoveryBindingID = UUID(uuidString: "00000000-0000-4000-8000-000000000101")!

private actor TestChatRecoveryStore: ChatRecoveryPersisting {
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        snapshots[key] = snapshot
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }

    func purge(bindingID: UUID) async throws {
        snapshots = snapshots.filter { $0.key.credentialBindingID != bindingID }
    }

    func sweep(keeping bindingIDs: Set<UUID>) async throws {
        snapshots = snapshots.filter { bindingIDs.contains($0.key.credentialBindingID) }
    }

    func eraseAllAfterCredentialDeletion() async throws {
        snapshots.removeAll()
    }
}

/// Shared `DroverClient` factory wired to an inert session by default, or to
/// a `MockNetwork` when provided. Tests making network requests should pass
/// their test's `MockNetwork` or use `mock.client()`.
func client(_ mock: MockNetwork? = nil) -> DroverClient {
    if let mock {
        return mock.client()
    }
    let cfg = URLSessionConfiguration.ephemeral
    cfg.protocolClasses = [MockURLProtocol.self]
    return DroverClient(config: ServerConfig(urlString: "http://test.local:7080")!,
                        token: "test-token",
                        credentialBindingID: testRecoveryBindingID,
                        session: URLSession(configuration: cfg),
                        retryGate: HubRetryGate())
}

@MainActor
func recoveryChatModel(
    client: DroverClient,
    sessionID: String,
    harness: String? = nil,
    store: HarnessModelCatalogStore = HarnessModelCatalogStore(),
    initialMessages: [HarnessMessage] = [],
    recap: String? = nil,
    recapSourceSeq: Int? = nil,
    recapPollInterval: Duration = .seconds(1),
    recapPollAttempts: Int = 30,
    deliveryConfirmationTimeout: Duration = .seconds(20),
    streamFactory: ((DroverClient, String) -> MessageStream)? = nil
) -> ChatModel {
    let recoveryWriteGate = ChatRecoveryWriteGate()
    let model = ChatModel(
        client: client,
        sessionID: sessionID,
        harness: harness,
        store: store,
        initialMessages: initialMessages,
        recap: recap,
        recapSourceSeq: recapSourceSeq,
        recapPollInterval: recapPollInterval,
        recapPollAttempts: recapPollAttempts,
        deliveryConfirmationTimeout: deliveryConfirmationTimeout,
        recoveryStore: TestChatRecoveryStore(),
        recoveryWriteGate: recoveryWriteGate,
        recoveryGeneration: recoveryWriteGate.generation,
        streamFactory: streamFactory
    )
    model.controls = .everythingAdvertised
    return model
}

// MARK: - HarnessMessage/ChatModel test factories (Task 7+)

extension HarnessMessage {
    static func fixture(
        seq: Int, type: MessageType, text: String = "",
        turnID: String? = nil,
        payload: [String: JSONValue] = [:]
    ) -> HarnessMessage {
        HarnessMessage(
            seq: seq,
            type: type,
            text: text,
            turnID: turnID,
            payload: payload
        )
    }
}

extension ChatModel {
    /// A `ChatModel` with no live stream — `ingest(_:)` drives it directly
    /// in tests, no network needed unless the test also calls a network action
    /// (`sendTurn`/`approve`/etc.). Pass `client: mock.client()` when network
    /// interactions are tested.
    /// Its host advertises every session control, so tests of approval and
    /// turn mechanics are not also tests of capability gating.
    static func fixture(client: DroverClient = client(), messages: [HarnessMessage] = []) -> ChatModel {
        let model = recoveryChatModel(
            client: client,
            sessionID: "fixture-session",
            initialMessages: messages
        )
        model.controls = .everythingAdvertised
        return model
    }
}

extension HarnessControls {
    /// A host advertising every session control, for tests about something
    /// else. Capability gating itself is covered in `HarnessCapabilitiesTests`.
    static let everythingAdvertised = HarnessControls(offer: HarnessOffer(
        name: "fixture",
        capabilities: HarnessCapabilities(
            launchModes: [.structured], approvals: true, interrupt: true,
            nativeResume: true, modelCatalog: true, turnPreferences: true,
            attachments: ["image/*"]
        )
    ))
}
