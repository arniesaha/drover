import Foundation
import Testing
@testable import DroverKit

@Test func snapshotDecodesLeniently() throws {
    let snap = try HarnessSnapshot.decode(from: snapshotJSON)
    #expect(snap.hosts.count == 1)
    #expect(snap.hosts[0].displayName == "Mac Mini")
    #expect(snap.hosts[0].harnesses.contains("agy"))
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

@Test func sessionMetadataDecodesFromHarnessSnapshot() throws {
    let json = Data("""
    {"hosts": [], "sessions": [
      {"session_id": "s1", "host_id": "h1", "harness": "codex",
       "mode": "structured", "status": "running", "awaiting": "input",
       "cwd": "/Volumes/M2 1/drover",
       "preview": "Refactor session screen cards",
       "started_at": "2026-08-05T10:40:37-07:00",
       "updated_at": "2026-08-05T10:41:00-07:00",
       "last_activity": "2026-08-05T10:55:34-07:00",
       "ended_at": null}],
     "cwd_suggestions": []}
    """.utf8)
    let snap = try HarnessSnapshot.decode(from: json)
    let session = snap.sessions[0]
    #expect(session.preview == "Refactor session screen cards")
    #expect(session.startedAt == ISO8601DateFormatter().date(from: "2026-08-05T17:40:37Z"))
    #expect(session.updatedAt == ISO8601DateFormatter().date(from: "2026-08-05T17:41:00Z"))
    #expect(session.lastActivity == ISO8601DateFormatter().date(from: "2026-08-05T17:55:34Z"))
    #expect(session.endedAt == nil)
}

/// Fleet snapshots may include a recap produced from the most recent
/// structured turn. Both fields are optional so older snapshots still decode.
@Test func sessionSummaryDecodesLiveRecap() throws {
    let json = Data(
        #"{"session_id":"s1","recap":"Improving previews; testing refresh.","recap_source_seq":12}"#.utf8
    )

    let session = try JSONDecoder().decode(SessionSummary.self, from: json)

    #expect(session.recap == "Improving previews; testing refresh.")
    #expect(session.recapSourceSeq == 12)
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

@Test func activityDateFallsBackToUpdatedThenStartedAt() {
    let updated = Date(timeIntervalSince1970: 200)
    let started = Date(timeIntervalSince1970: 100)
    let session = SessionSummary(
        id: "s",
        hostID: "h",
        harness: "codex",
        mode: "structured",
        status: "running",
        awaiting: nil,
        cwd: nil,
        lastActivity: nil,
        startedAt: started,
        updatedAt: updated
    )

    #expect(session.activityDate == updated)
}

@Test(arguments: [
    ("claude-code", "Claude", "brain"),
    ("codex", "Codex", "chevron.left.forwardslash.chevron.right"),
    ("agy", "Antigravity", "sparkles"),
    ("shell", "Shell", "terminal"),
    ("custom-harness", "custom-harness", "terminal"),
])
func harnessPresentationMapsKnownHarnesses(harness: String, name: String, symbolName: String) {
    let presentation = HarnessPresentation(harness)
    #expect(presentation.name == name)
    #expect(presentation.symbolName == symbolName)
}

@Test func codexUsageSummaryFormatsTurnCompletedUsage() {
    let message = HarnessMessage.fixture(
        seq: 1,
        type: .status,
        payload: [
            "usage": .object([
                "input_tokens": .number(18_240),
                "cached_input_tokens": .number(4_992),
                "output_tokens": .number(67),
                "reasoning_output_tokens": .number(59),
            ])
        ]
    )
    let summary = TokenUsageSummary(message: message)
    #expect(summary?.compactText == "in 18.2K | out 67 | cache 5K | reason 59")
    #expect(summary?.contextText == nil)
}

@Test func claudeUsageSummaryReportsTotalsButNotContext() {
    let message = HarnessMessage.fixture(
        seq: 1,
        type: .status,
        payload: [
            "result": .object([
                "usage": .object([
                    "input_tokens": .number(5_985),
                    "cache_read_input_tokens": .number(49_153),
                    "output_tokens": .number(64),
                ]),
                "modelUsage": .object([
                    "claude-fable-5[1m]": .object([
                        "inputTokens": .number(5_985),
                        "outputTokens": .number(64),
                        "cacheReadInputTokens": .number(49_153),
                        "contextWindow": .number(1_000_000),
                    ])
                ])
            ])
        ]
    )
    let summary = TokenUsageSummary(message: message)
    #expect(summary?.compactText == "in 6K | out 64 | cache 49.2K")
    // Context now comes from ContextGauge over the whole message list --
    // a single result payload cannot describe live context (see
    // ContextGaugeTests.ignoresResultUsageWhichIsAlsoCumulative).
    #expect(summary?.contextText == nil)
}

@Test func geminiUsageSummaryAggregatesStatsTokens() {
    let message = HarnessMessage.fixture(
        seq: 1,
        type: .assistantOutput,
        payload: [
            "stats": .object([
                "models": .object([
                    "gemini-3.1-flash-lite": .object([
                        "tokens": .object([
                            "input": .number(837),
                            "candidates": .number(35),
                            "cached": .number(0),
                            "thoughts": .number(436),
                        ])
                    ]),
                    "gemini-3.5-flash": .object([
                        "tokens": .object([
                            "input": .number(10_215),
                            "candidates": .number(2),
                            "cached": .number(0),
                            "thoughts": .number(128),
                        ])
                    ]),
                ])
            ])
        ]
    )
    let summary = TokenUsageSummary(message: message)
    #expect(summary?.compactText == "in 11.1K | out 37 | reason 564")
}

/// The server sends cwd suggestions as objects ({path, source, host_id}) —
/// regression guard for the silent [] that decoding them as [String]
/// produced (the launch sheet's suggestion menu was always empty).
@Test func cwdSuggestionsDecodeTheServerObjectShape() throws {
    let snap = try HarnessSnapshot.decode(from: snapshotJSON)
    #expect(snap.cwdSuggestions.count == 3)
    #expect(snap.cwdSuggestions[0].path == "/Users/arnabmac/jenny/nexus")
    #expect(snap.cwdSuggestions[0].source == "recent session")
    #expect(snap.cwdSuggestions[0].hostID == "mac-mini")
    #expect(snap.cwdSuggestions[1].hostID == nil)   // favorites are host-agnostic
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

@Test func messagePageDecodesPaginationMetadata() throws {
    let data = Data("""
    {"messages": [
      {"event_id": "event-4", "seq": 4, "type": "assistant_output",
       "role": "assistant", "text": "four", "payload": {}},
      {"event_id": "event-5", "seq": 5, "type": "assistant_output",
       "role": "assistant", "text": "five", "payload": {}}
    ], "page_min_seq": 4, "page_max_seq": 5, "max_seq": 9,
       "has_older": true, "has_newer": true}
    """.utf8)

    let page = try MessagePage.decode(from: data)

    #expect(page.messages.map(\.seq) == [4, 5])
    #expect(page.pageMinSeq == 4)
    #expect(page.pageMaxSeq == 5)
    #expect(page.maxSeq == 9)
    #expect(page.hasOlder)
    #expect(page.hasNewer)
    #expect(page.decodeIssues.isEmpty)
}

@Test func messagePageDecodesLegacyBatchDefaults() throws {
    let page = try MessagePage.decode(from: messagesJSON)

    #expect(page.messages.map(\.seq) == [1, 2, 3])
    #expect(page.pageMinSeq == nil)
    #expect(page.pageMaxSeq == nil)
    #expect(page.maxSeq == 3)
    #expect(page.hasOlder == false)
    #expect(page.hasNewer == false)
    #expect(page.decodeIssues.count == 1)
    #expect(page.decodeIssues[0].index == 3)
    #expect(page.decodeIssues[0].seq == nil)
}

@Test func messagePageReportsMalformedElementWithoutDiscardingNeighbors() throws {
    let data = Data("""
    {"messages": [
      {"event_id": "event-1", "seq": 1, "type": "user_input",
       "role": "user", "text": "one", "payload": {}},
      {"seq": 2, "type": "assistant_output", "role": "assistant",
       "text": "must not appear in diagnostics", "payload": {}},
      {"event_id": "event-3", "seq": 3, "type": "assistant_output",
       "role": "assistant", "text": "three", "payload": {}}
    ], "page_min_seq": 1, "page_max_seq": 3, "max_seq": 3,
       "has_older": false, "has_newer": false}
    """.utf8)

    let page = try MessagePage.decode(from: data)

    #expect(page.messages.map(\.seq) == [1, 3])
    #expect(page.decodeIssues.count == 1)
    #expect(page.decodeIssues[0].index == 1)
    #expect(page.decodeIssues[0].seq == 2)
    #expect(page.decodeIssues[0].detail.contains("must not appear") == false)
}

// The chat transcript renders `displayText`, parsed from markdown exactly
// once per message (at decode/init) — parsing per render pass (what
// `Text(LocalizedStringKey)` does) saturates the main thread during long
// streams and contributes to the LazyVStack blanking.

@Test func displayTextParsesInlineMarkdown() {
    let message = HarnessMessage(seq: 1, type: .assistantOutput,
                                 text: "hello **bold** world")
    #expect(String(message.displayText.characters) == "hello bold world")
    let hasBold = message.displayText.runs.contains { run in
        run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true
    }
    #expect(hasBold)
}

@Test func displayTextPreservesNewlinesAndPlainText() {
    let message = HarnessMessage(seq: 1, type: .assistantOutput,
                                 text: "line one\nline two")
    #expect(String(message.displayText.characters) == "line one\nline two")
}

@Test func displayTextSurvivesDecoding() throws {
    let batch = try MessageBatch.decode(from: messagesJSON)
    #expect(String(batch.messages[0].displayText.characters) == "hi")
}

@Test func jsonValueRoundTrip() throws {
    let data = Data(#"{"a": 1, "b": [true, null, "x"], "c": {"d": 2.5}}"#.utf8)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: data)
    #expect(value["a"]?.stringValue == nil)
    #expect(value["c"]?.objectValue?["d"] == .number(2.5))
}

@Test func harnessAuthStatusDecodes() throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"codex","state":"unauthenticated",
     "label":null,"detail":"Not logged in"}
    """.utf8)
    let status = try JSONDecoder().decode(HarnessAuthStatus.self, from: data)
    #expect(status.hostID == "mac-mini")
    #expect(status.harness == "codex")
    #expect(status.state == .unauthenticated)
    #expect(status.detail == "Not logged in")
}

@Test func harnessAuthFlowDecodesUnknownStateLeniently() throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"agy","flow_id":"auth-flow-1",
     "state":"provider_weird","login_url":"https://example.test",
     "user_code":"ABCD-EFGH","message":"Open browser"}
    """.utf8)
    let flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: data)
    #expect(flow.state == .unknown)
    #expect(flow.loginURL?.absoluteString == "https://example.test")
    #expect(flow.userCode == "ABCD-EFGH")
}

@Test func harnessAuthFlowDecodesExpiryAndMalformedURL() throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1",
     "state":"expired","login_url":"not a url",
     "expires_at":"2026-07-21T12:34:56Z"}
    """.utf8)
    let flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: data)
    #expect(flow.state == .expired)
    #expect(flow.loginURL == nil)
    #expect(flow.expiresAt == ISO8601DateFormatter().date(from: "2026-07-21T12:34:56Z"))
    #expect(flow.isTerminal)
}

@Test(arguments: ["not a url", "https:relative", "https:/missing-host"])
func harnessAuthFlowDropsNonAbsoluteLoginURL(rawURL: String) throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1",
     "state":"waiting_for_user","login_url":"\(rawURL)"}
    """.utf8)
    let flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: data)
    #expect(flow.loginURL == nil)
}

@Test(arguments: [
    ("2026-07-30 10:12:03.123456+00:00", true),
    ("2026-07-30 10:12:03.123456", true),
    ("2026-07-30 10:12:03+00:00", true),
    ("2026-07-30 10:12:03", true),
    ("2026-07-30T10:12:03Z", true),          // existing ISO path must keep working
    ("2026-07-30T10:12:03.123Z", true),      // existing fractional ISO path
    ("not a date", false),
    ("", false),
])
func wireDateParsesServerAndISOFormats(raw: String, parses: Bool) {
    #expect((WireDate.parse(raw) != nil) == parses)
}

@Test func wireDateTreatsNaiveTimestampAsUTC() {
    let naive = WireDate.parse("2026-07-30 10:12:03")
    let aware = WireDate.parse("2026-07-30 10:12:03+00:00")
    #expect(naive != nil)
    #expect(naive == aware)
}

@Test func hostSummaryDecodesFleetFields() throws {
    let json = Data("""
    {"host_id": "work-laptop", "status": "offline", "connection_kind": "relay",
     "last_seen_at": "2026-07-30 10:12:03.123456+00:00", "kind": "laptop",
     "capabilities": {"display_name": "Work Laptop",
                      "harnesses": [{"name": "claude-code", "enabled": true},
                                    {"name": "shell", "enabled": false}]}}
    """.utf8)
    let host = try JSONDecoder().decode(HostSummary.self, from: json)
    #expect(host.id == "work-laptop")
    #expect(host.displayName == "Work Laptop")
    #expect(host.connectionKind == "relay")
    #expect(host.isRelay)
    #expect(host.lastSeenAt != nil)
    #expect(host.harnesses == ["claude-code"])
}

@Test func hostSummaryDefaultsWhenFleetFieldsAbsent() throws {
    let json = Data(#"{"host_id": "mac-mini", "status": "online"}"#.utf8)
    let host = try JSONDecoder().decode(HostSummary.self, from: json)
    #expect(host.connectionKind == "direct")
    #expect(host.isRelay == false)
    #expect(host.lastSeenAt == nil)
    #expect(host.title == "mac-mini")   // displayName empty → falls back to id
}

@Test(arguments: [
    ("online", HostPresence.online),
    ("stale", HostPresence.stale),
    ("offline", HostPresence.offline),
    ("", HostPresence.offline),
    ("mystery", HostPresence.offline),
])
func hostPresenceDerivation(status: String, expected: HostPresence) {
    let host = HostSummary.fixture(status: status)
    #expect(host.presence == expected)
}

/// Regression: a single malformed harness entry must not discard every
/// other entry for the host — decoding is per-element lenient (mirrors
/// `LenientElement`'s contract, already relied on elsewhere in this file),
/// not whole-array lenient.
@Test func hostSummaryHarnessesSkipOnlyMalformedEntries() throws {
    let json = Data("""
    {"host_id": "mac-mini", "status": "online",
     "capabilities": {"display_name": "Mac Mini",
                      "harnesses": [{"name": "claude-code", "enabled": true},
                                    {"name": "broken-entry"},
                                    {"name": "shell", "enabled": true}]}}
    """.utf8)
    let host = try JSONDecoder().decode(HostSummary.self, from: json)
    #expect(host.harnesses == ["claude-code", "shell"])
}
