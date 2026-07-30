# iOS Fleet-First Sessions + Connection Resilience (M3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the iOS sessions screen into a fleet-first view — sessions grouped by host with live status dots, relay badges, and last-seen — with three explicit connection-resilience layers (hub-unreachable banner, offline-host graying, shared reconnecting pill) and no infinite spinners.

**Architecture:** All logic lands in NexusKit (tested with Swift Testing); the app target gets thin SwiftUI views over it. `HostSummary` learns the wire fields the hub already sends (`connection_kind`, `last_seen_at`, three-way `status`); `SessionStore` gains a pure static grouping function and a `hasLoadedOnce` flag; `SessionsView` is restructured into host sections with an unreachable banner and retriable load states.

**Tech Stack:** Swift 6.0, iOS 18, SwiftUI `@Observable`, Swift Testing (NOT XCTest), XcodeGen, SwiftTerm 1.13.0 (unchanged).

**Spec:** `docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md` — "UX track (iOS)" → *Fleet legibility* + *Connection resilience* sections (= milestone M3).

**Scope notes (deliberate deviations, flag in PR):**
- *Compact `TokenUsageSummary` in session rows* is **descoped to a follow-up**: the hub's session rows carry no usage fields (usage exists only inside per-message payloads at `src/drover/server/harness/structured/claude.py:245`), so this needs a server-side rollup first. File a tracking issue in Task 8.
- Resilience layer 3 (stream/terminal auto-reconnect + scrollback) already shipped 2026-07-13 (`ac19768`); this plan only extracts the duplicated pill into a shared component and gives the chat pill an accessibility id.

## Global Constraints

- App root: `/Volumes/M2 1/drover/apps/drover` — **paths contain a space; always quote.**
- Swift tools 6.0; platforms iOS 18 / macOS 14 (`NexusKit/Package.swift`).
- Tests are **Swift Testing** (`import Testing`, `@Test`, `#expect`), never XCTest.
- **MockNetworkTests rule:** any test that installs `MockURLProtocol.handler` MUST be nested as `extension MockNetworkTests { @Suite(.serialized) struct XTests { … } }` (rationale doc-comment: `NexusKitTests/ClientTests.swift:5-8`). Pure model/derivation tests stay OUTSIDE that suite (file-scope or plain struct).
- Tests touching a `@MainActor @Observable` model need `@Test @MainActor`.
- Decoding style is lenient: `try?` + fallback per field; only `id` is required. Follow it.
- `Drover.xcodeproj` is generated: after ADDING any file under `Drover/`, run `xcodegen generate` (from `apps/drover/`). NexusKit source/test files need no regen.
- Fast test loop (package only): `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test`
- Full app test loop: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`
- Commit style: conventional commits (`feat(ios): …`, `fix(ios): …`, `refactor(ios): …`, `test(ios): …`, `docs: …`).
- Server wire facts (do not re-derive): host `status` ∈ `"online" | "stale" | "offline"` — relay hosts are socket-truth online/offline (never stale, flip in ~60–80s, `src/drover/server/relay_manager.py:50,68`); direct hosts are online/stale (never offline; stale after 45s, `src/drover/server/metrics.py:33`). Datetimes are Python `str(datetime)`: `"YYYY-MM-DD HH:MM:SS[.ffffff][+00:00]"` — **space separator, fraction and offset both optional; naive means UTC**. Every session row carries `host_id`. Sessions "waiting on you" = `awaiting == "input"` or `"approval"`.

---

### Task 1: `WireDate` — parse the hub's `str(datetime)` format

The hub serializes all datetimes via `json.dumps(..., default=str)` (`src/drover/server/metrics.py:459-469`), producing `"2026-07-30 10:12:03.123456+00:00"` — space separator, sometimes no fraction, sometimes no offset. `WireDate.parse` (`NexusKit/Sources/NexusKit/Models.swift:21-27`) only tries ISO-8601 with `T`, so **`SessionSummary.lastActivity` is silently nil against the real server today** (the lenient decoder swallows it). This task fixes that and unblocks `last_seen_at` in Task 2. The web UI does the same normalization at `src/drover/server/web/static/harness.html:463-469`.

**Files:**
- Modify: `NexusKit/Sources/NexusKit/Models.swift` (the `WireDate` enum, currently lines 21–27)
- Test: `NexusKit/Tests/NexusKitTests/ModelsTests.swift` (file-scope tests — no network, stays outside `MockNetworkTests`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `WireDate.parse(_ value: String) -> Date?` now also accepts server-format strings. (Internal to NexusKit; tests reach it via `@testable import`.) If the existing `parse` signature takes an optional or differs slightly, keep the existing signature and add the fallback inside it — every call site stays untouched.

- [ ] **Step 1: Write the failing tests** — append to `ModelsTests.swift` at file scope:

```swift
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter wireDate`
Expected: FAIL — the four space-separator cases parse to nil.

- [ ] **Step 3: Implement the fallback** — extend `WireDate` in `Models.swift`. Keep the existing ISO formatters and try them first; add:

```swift
    /// The hub serializes datetimes with Python's `str(datetime)`:
    /// "2026-07-30 10:12:03.123456+00:00" — space separator, fraction and
    /// offset both optional. Naive timestamps are UTC (same assumption as
    /// the web UI's normalizer in static/harness.html).
    /// DateFormatter parsing has been thread-safe since iOS 7; held the same
    /// way as the ISO formatters above.
    nonisolated(unsafe) private static let serverFormatters: [DateFormatter] = [
        "yyyy-MM-dd HH:mm:ss.SSSSSSxxxxx",
        "yyyy-MM-dd HH:mm:ss.SSSSSS",
        "yyyy-MM-dd HH:mm:ssxxxxx",
        "yyyy-MM-dd HH:mm:ss",
    ].map { pattern in
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = pattern
        return formatter
    }

    private static func parseServerFormat(_ value: String) -> Date? {
        for formatter in serverFormatters {
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }
```

and change `parse` so its final fallback is `parseServerFormat(value)` after the existing ISO attempts.

- [ ] **Step 4: Run the tests**

Run: `swift test --filter wireDate` — Expected: PASS.
Then the whole package: `swift test` — Expected: all pass (no call-site changes).

- [ ] **Step 5: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit
git commit -m "fix(ios): parse the hub's str(datetime) wire format in WireDate

The hub serializes datetimes with a space separator and optional
fraction/offset; the ISO-only parser left last_activity silently nil
against the real server."
```

---

### Task 2: `HostSummary` fleet fields — `connection_kind`, `last_seen_at`, presence

The wire already carries every `HarnessHost` field (`src/drover/server/harness/models.py:20-32`); the app just doesn't decode them. Add the fields, a memberwise init (there is none today — `Models.swift:204`), `Equatable`/`Hashable`, and derived presence.

**Files:**
- Modify: `NexusKit/Sources/NexusKit/Models.swift` (replace `HostSummary`, lines 204–241; add `HostPresence`)
- Modify: `NexusKit/Tests/NexusKitTests/Support/Fixtures.swift` (add `HostSummary.fixture`, `fleetSnapshotJSON`, extend `SessionSummary.fixture` with `hostID`)
- Modify: `Drover/Screens/Launch/LaunchView.swift:31-37` (use new `title` instead of the inline ternary)
- Test: `NexusKit/Tests/NexusKitTests/ModelsTests.swift`

**Interfaces:**
- Consumes: `WireDate.parse(_:) -> Date?` (Task 1).
- Produces (used by Tasks 3–6):
  - `HostSummary` gains `connectionKind: String`, `lastSeenAt: Date?`, conformances `Equatable, Hashable`, and `public init(id: String, displayName: String, status: String, connectionKind: String = "direct", lastSeenAt: Date? = nil, harnesses: [String] = [])`
  - `public enum HostPresence: String, Sendable { case online, stale, offline }`
  - `HostSummary.presence: HostPresence`, `HostSummary.isRelay: Bool`, `HostSummary.title: String`
  - `HostSummary.fixture(id:displayName:status:connectionKind:lastSeenAt:harnesses:)` and `let fleetSnapshotJSON: Data` in test support
  - `SessionSummary.fixture` gains `hostID: String = "fixture-host"` parameter

- [ ] **Step 1: Write the failing tests** — append to `ModelsTests.swift` at file scope:

```swift
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `swift test --filter hostSummary` (and `--filter hostPresence`)
Expected: COMPILE FAILURE — no memberwise init, no `fixture`, no `connectionKind`/`presence`/`title`/`HostPresence`.

- [ ] **Step 3: Implement** — replace `HostSummary` in `Models.swift` with:

```swift
public struct HostSummary: Sendable, Identifiable, Decodable, Equatable, Hashable {
    public var id: String
    public var displayName: String
    public var status: String
    public var connectionKind: String
    public var lastSeenAt: Date?
    public var harnesses: [String]

    public init(
        id: String,
        displayName: String,
        status: String,
        connectionKind: String = "direct",
        lastSeenAt: Date? = nil,
        harnesses: [String] = []
    ) {
        self.id = id
        self.displayName = displayName
        self.status = status
        self.connectionKind = connectionKind
        self.lastSeenAt = lastSeenAt
        self.harnesses = harnesses
    }

    private enum CodingKeys: String, CodingKey {
        case id = "host_id"
        case status
        case connectionKind = "connection_kind"
        case lastSeenAt = "last_seen_at"
        case capabilities
    }

    private enum CapabilitiesKeys: String, CodingKey {
        case displayName = "display_name"
        case harnesses
    }

    private struct HarnessEntry: Decodable {
        let name: String
        let enabled: Bool
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        status = (try? container.decode(String.self, forKey: .status)) ?? ""
        connectionKind = (try? container.decode(String.self, forKey: .connectionKind)) ?? "direct"
        if let raw = try? container.decode(String.self, forKey: .lastSeenAt) {
            lastSeenAt = WireDate.parse(raw)
        } else {
            lastSeenAt = nil
        }
        if let caps = try? container.nestedContainer(keyedBy: CapabilitiesKeys.self, forKey: .capabilities) {
            displayName = (try? caps.decode(String.self, forKey: .displayName)) ?? ""
            let entries = (try? caps.decode([HarnessEntry].self, forKey: .harnesses)) ?? []
            harnesses = entries.filter(\.enabled).map(\.name)
        } else {
            displayName = ""
            harnesses = []
        }
    }
}

/// Three-way host presence. Relay hosts are socket-truth online/offline
/// (never stale); direct hosts are heartbeat-based online/stale (never
/// offline). Unknown/empty statuses render as offline.
public enum HostPresence: String, Sendable {
    case online, stale, offline
}

extension HostSummary {
    public var presence: HostPresence {
        switch status {
        case "online": return .online
        case "stale": return .stale
        default: return .offline
        }
    }

    public var isRelay: Bool { connectionKind == "relay" }

    public var title: String { displayName.isEmpty ? id : displayName }
}
```

Preserve the existing doc comments on the type if any; keep the decoder's lenient style exactly as shown.

- [ ] **Step 4: Add fixtures** — in `Support/Fixtures.swift`:

Add next to the existing `SessionSummary.fixture` (and add a `hostID: String = "fixture-host"` parameter to that one, passing it through to `hostID:` — all existing call sites keep working via the default):

```swift
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
```

- [ ] **Step 5: Simplify `LaunchView`** — at `Drover/Screens/Launch/LaunchView.swift:31-37`, replace the picker label's `host.displayName.isEmpty ? host.id : host.displayName` with `host.title`.

- [ ] **Step 6: Run the tests**

Run: `swift test` in `NexusKit/` — Expected: all pass, including the pre-existing `LaunchModelTests`/`ModelsTests` untouched assertions.
Then verify the app still builds (LaunchView touched, no new files, no regen needed):
`cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
Expected: BUILD SUCCEEDED.

- [ ] **Step 7: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover
git commit -m "feat(ios): decode host connection_kind, last_seen_at, three-way presence"
```

---

### Task 3: Fleet grouping in `SessionStore` + `hasLoadedOnce`

Pure grouping logic as a `nonisolated static` function (testable without the network suite), an instance property over the cached snapshot, and a `hasLoadedOnce` flag so the UI can tell "never loaded" from "empty" (fixes the no-loading-state gap, `SessionStore.swift` has no such flag today).

**Files:**
- Modify: `NexusKit/Sources/NexusKit/SessionStore.swift`
- Test (pure): Create `NexusKit/Tests/NexusKitTests/FleetGroupingTests.swift`
- Test (network): `NexusKit/Tests/NexusKitTests/StoreTests.swift` (inside `MockNetworkTests`)

**Interfaces:**
- Consumes: `HostSummary` + `HostPresence` + fixtures (Task 2), existing `SessionSummary.attention`, existing private `byLastActivityDescending`.
- Produces (used by Tasks 4–6):
  - `public struct HostGroup: Sendable, Equatable, Identifiable { public let host: HostSummary; public let sessions: [SessionSummary]; public var id: String { host.id } ; public init(host:sessions:) }`
  - `SessionStore.hostGroups: [HostGroup]` (instance, computed)
  - `public nonisolated static func hostGroups(hosts: [HostSummary], sessions: [SessionSummary]) -> [HostGroup]`
  - `SessionStore.hasLoadedOnce: Bool` (public private(set), false until first successful refresh, never reset)

- [ ] **Step 1: Write the failing pure tests** — create `FleetGroupingTests.swift`:

```swift
import Foundation
import Testing
@testable import NexusKit

// Pure derivation tests — no MockURLProtocol, so deliberately OUTSIDE
// the MockNetworkTests serialized suite (see ClientTests' doc comment).
struct FleetGroupingTests {
    @Test func groupsActiveSessionsUnderTheirHost() {
        let hosts = [HostSummary.fixture(id: "mac-mini"), HostSummary.fixture(id: "nas")]
        let sessions = [
            SessionSummary.fixture(id: "s1", status: "running", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "s2", status: "running", awaiting: nil, hostID: "nas"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups.map(\.id) == ["mac-mini", "nas"])
        #expect(groups[0].sessions.map(\.id) == ["s1"])
        #expect(groups[1].sessions.map(\.id) == ["s2"])
    }

    @Test func waitingSessionsSortToTopOfTheirGroup() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let sessions = [
            SessionSummary.fixture(id: "working", status: "running", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "input", status: "running", awaiting: "input", hostID: "mac-mini"),
            SessionSummary.fixture(id: "approval", status: "running", awaiting: "approval", hostID: "mac-mini"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups[0].sessions.map(\.id) == ["approval", "input", "working"])
    }

    @Test func offlineAndStaleHostsSortAfterOnline() {
        let hosts = [
            HostSummary.fixture(id: "laptop", status: "offline"),
            HostSummary.fixture(id: "nas", status: "stale"),
            HostSummary.fixture(id: "mac", status: "online"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: [])
        #expect(groups.map(\.id) == ["mac", "nas", "laptop"])
    }

    @Test func hostWithNoActiveSessionsStillAppears() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: [])
        #expect(groups.map(\.id) == ["mac-mini"])
        #expect(groups[0].sessions.isEmpty)
    }

    @Test func finishedSessionsAreExcludedFromGroups() {
        let hosts = [HostSummary.fixture(id: "mac-mini")]
        let sessions = [
            SessionSummary.fixture(id: "done", status: "completed", awaiting: nil, hostID: "mac-mini"),
            SessionSummary.fixture(id: "err", status: "errored", awaiting: nil, hostID: "mac-mini"),
        ]
        let groups = SessionStore.hostGroups(hosts: hosts, sessions: sessions)
        #expect(groups[0].sessions.isEmpty)
    }

    @Test func sessionOnUnknownHostGetsSynthesizedOfflineGroup() {
        let sessions = [
            SessionSummary.fixture(id: "orphan", status: "running", awaiting: nil, hostID: "ghost-host"),
        ]
        let groups = SessionStore.hostGroups(hosts: [], sessions: sessions)
        #expect(groups.map(\.id) == ["ghost-host"])
        #expect(groups[0].host.presence == .offline)
        #expect(groups[0].sessions.map(\.id) == ["orphan"])
    }
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `swift test --filter FleetGroupingTests`
Expected: COMPILE FAILURE — `HostGroup` and `SessionStore.hostGroups` don't exist.

- [ ] **Step 3: Implement grouping** — in `SessionStore.swift`, add above the class:

```swift
/// One host's slice of the fleet: the host row plus its *active* sessions
/// (needs-approval / needs-input / working), waiting-first.
public struct HostGroup: Sendable, Equatable, Identifiable {
    public let host: HostSummary
    public let sessions: [SessionSummary]

    public var id: String { host.id }

    public init(host: HostSummary, sessions: [SessionSummary]) {
        self.host = host
        self.sessions = sessions
    }
}
```

and inside `SessionStore`:

```swift
    /// Sessions grouped by host, fleet-first: online hosts before stale
    /// before offline, waiting sessions at the top of their group. Hosts
    /// with no active sessions still appear (the one-glance fleet view);
    /// sessions whose host the hub no longer lists get a synthesized
    /// offline group rather than vanishing.
    public var hostGroups: [HostGroup] {
        Self.hostGroups(hosts: snapshot?.hosts ?? [], sessions: snapshot?.sessions ?? [])
    }

    public nonisolated static func hostGroups(
        hosts: [HostSummary],
        sessions: [SessionSummary]
    ) -> [HostGroup] {
        let active = sessions.filter {
            switch $0.attention {
            case .needsApproval, .needsInput, .working: return true
            case .done, .errored: return false
            }
        }
        var byHost = Dictionary(grouping: active, by: \.hostID)
        var groups = hosts.map { host in
            HostGroup(
                host: host,
                sessions: (byHost.removeValue(forKey: host.id) ?? []).sorted(by: groupOrdering)
            )
        }
        for (hostID, orphans) in byHost {
            groups.append(HostGroup(
                host: HostSummary(id: hostID, displayName: hostID, status: "offline"),
                sessions: orphans.sorted(by: groupOrdering)
            ))
        }
        return groups.sorted(by: hostOrdering)
    }

    private nonisolated static func attentionRank(_ session: SessionSummary) -> Int {
        switch session.attention {
        case .needsApproval: return 0
        case .needsInput: return 1
        default: return 2
        }
    }

    private nonisolated static func groupOrdering(_ a: SessionSummary, _ b: SessionSummary) -> Bool {
        let (ra, rb) = (attentionRank(a), attentionRank(b))
        if ra != rb { return ra < rb }
        return byLastActivityDescending(a, b)
    }

    private nonisolated static func presenceRank(_ host: HostSummary) -> Int {
        switch host.presence {
        case .online: return 0
        case .stale: return 1
        case .offline: return 2
        }
    }

    private nonisolated static func hostOrdering(_ a: HostGroup, _ b: HostGroup) -> Bool {
        let (ra, rb) = (presenceRank(a.host), presenceRank(b.host))
        if ra != rb { return ra < rb }
        return a.host.title.localizedCaseInsensitiveCompare(b.host.title) == .orderedAscending
    }
```

(`byLastActivityDescending` already exists as a private static on `SessionStore` — if it isn't `nonisolated`, mark it so; it touches no state.)

- [ ] **Step 4: Run the pure tests**

Run: `swift test --filter FleetGroupingTests` — Expected: PASS.

- [ ] **Step 5: Write the failing `hasLoadedOnce` tests** — append inside the existing `extension MockNetworkTests { @Suite(.serialized) struct StoreTests { … } }` in `StoreTests.swift`:

```swift
    @Test @MainActor func hasLoadedOnceFlipsOnFirstSuccessfulRefresh() async throws {
        MockURLProtocol.handler = { _ in (200, snapshotJSON) }
        let store = SessionStore(client: client())
        #expect(store.hasLoadedOnce == false)
        await store.refresh()
        #expect(store.hasLoadedOnce)
    }

    @Test @MainActor func hasLoadedOnceSurvivesLaterFailure() async throws {
        MockURLProtocol.handler = { _ in (200, snapshotJSON) }
        let store = SessionStore(client: client())
        await store.refresh()
        MockURLProtocol.handler = { _ in (500, Data()) }
        await store.refresh()
        #expect(store.hasLoadedOnce)
        #expect(store.isReachable == false)
        #expect(store.snapshot != nil)   // last-known state kept, list never blanks
    }

    @Test @MainActor func fleetSnapshotProducesHostGroups() async throws {
        MockURLProtocol.handler = { _ in (200, fleetSnapshotJSON) }
        let store = SessionStore(client: client())
        await store.refresh()
        #expect(store.hostGroups.map(\.id) == ["mac-mini", "nas", "ghost-host", "work-laptop"])
        #expect(store.hostGroups[0].sessions.map(\.id) == ["mac-input", "mac-running"])
    }
```

(Expected order: mac-mini is the only online host → first; nas is stale → second; ghost-host and work-laptop are both offline (ghost-host's group is synthesized), so they sort alphabetically by title — "ghost-host" before "Work Laptop" under `localizedCaseInsensitiveCompare`.)

- [ ] **Step 6: Run to verify the new store tests fail**

Run: `swift test --filter StoreTests`
Expected: COMPILE FAILURE — `hasLoadedOnce` doesn't exist.

- [ ] **Step 7: Implement `hasLoadedOnce`** — in `SessionStore`:

```swift
    /// True once any refresh has succeeded. Lets the UI distinguish
    /// "never loaded" (spinner / retriable error) from "loaded but empty".
    /// Never reset: after first success the list renders last-known state.
    public private(set) var hasLoadedOnce = false
```

and in the success path of `refresh()` (where `snapshot`/`isReachable`/`lastError` are already updated), add `hasLoadedOnce = true`.

- [ ] **Step 8: Run the full package suite**

Run: `swift test` — Expected: all pass.

- [ ] **Step 9: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit
git commit -m "feat(ios): host-grouped fleet view model with waiting-first ordering"
```

---

### Task 4: Fleet-first `SessionsView` with host section headers

Restructure the list from attention buckets ("Needs you"/"Working") into host sections; keep the global "Finished" disclosure. The existing `row(for:)`, context menus, launch sheet, `AttentionWatcher` wiring, and navigation destinations are untouched.

**Files:**
- Create: `Drover/Screens/Sessions/HostSectionHeader.swift`
- Modify: `Drover/Screens/Sessions/SessionsView.swift` (body, lines 25–48; delete the `bucket(_:empty:)` helper at line 101)

**Interfaces:**
- Consumes: `store.hostGroups`, `HostSummary.presence/.isRelay/.title/.lastSeenAt` (Tasks 2–3), existing `row(for:)` and `store.finished`.
- Produces: `struct HostSectionHeader: View { let host: HostSummary }` with accessibility id `host-header-<host_id>`; used only here.

- [ ] **Step 1: Create `HostSectionHeader.swift`**

```swift
import NexusKit
import SwiftUI

/// Fleet section header: presence dot, host name, relay badge, last-seen.
/// Green = live, amber = stale (missed heartbeats, may recover),
/// gray = offline (relay socket down, or host unknown to the hub).
struct HostSectionHeader: View {
    let host: HostSummary

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(dotColor)
                .frame(width: 8, height: 8)
            Text(host.title)
            if host.isRelay {
                Text("relay")
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(.secondary.opacity(0.2), in: Capsule())
            }
            Spacer()
            if host.presence != .online {
                Text(lastSeenText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textCase(nil)
            }
        }
        .accessibilityIdentifier("host-header-\(host.id)")
    }

    private var dotColor: Color {
        switch host.presence {
        case .online: return .green
        case .stale: return .orange
        case .offline: return .gray
        }
    }

    private var lastSeenText: String {
        guard let lastSeenAt = host.lastSeenAt else { return "offline" }
        let relative = lastSeenAt.formatted(.relative(presentation: .named))
        return "last seen \(relative)"
    }
}
```

- [ ] **Step 2: Restructure the `List` body in `SessionsView.swift`** — replace the "Needs you"/"Working" sections (keep the `lastError` section for now; Task 6 replaces it) with:

```swift
            ForEach(store.hostGroups) { group in
                Section {
                    Group {
                        if group.sessions.isEmpty {
                            Text("No active sessions")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(group.sessions) { session in
                                row(for: session)
                            }
                        }
                    }
                    .opacity(group.host.presence == .online ? 1 : 0.55)
                } header: {
                    HostSectionHeader(host: group.host)
                }
            }
```

Keep the `Finished (\(store.finished.count))` `DisclosureGroup` exactly as it is today. Delete the now-unused `bucket(_:empty:)` helper. Do not touch `.onChange(of: store.needsYou)` — the notification path still keys off `needsYou`.

- [ ] **Step 3: Regenerate the project and build**

```bash
cd "/Volumes/M2 1/drover/apps/drover"
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
```
Expected: BUILD SUCCEEDED.

- [ ] **Step 4: Run the full unit suite through Xcode** (catches any app-target fallout)

```bash
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover
git commit -m "feat(ios): fleet-first sessions screen grouped by host with presence headers"
```

---

### Task 5: `StatusChip` on session rows

The spec's row status chip: running / waiting on you / needs approval / exited / error. The host capsule in the row is now redundant (host is the section header) — replace it with the chip.

**Files:**
- Create: `Drover/Screens/Sessions/StatusChip.swift`
- Modify: `Drover/Screens/Sessions/SessionRow.swift` (replace the host-id capsule at lines 30–34 with `StatusChip(attention: session.attention)`)

**Interfaces:**
- Consumes: `SessionSummary.attention` (existing `AttentionState`).
- Produces: `struct StatusChip: View { let attention: AttentionState }`; used only in `SessionRow`.

- [ ] **Step 1: Create `StatusChip.swift`**

```swift
import NexusKit
import SwiftUI

/// Compact per-session status chip. Colors mirror SessionRow's
/// attention tinting so the two never disagree.
struct StatusChip: View {
    let attention: AttentionState

    var body: some View {
        Text(label)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }

    private var label: String {
        switch attention {
        case .needsApproval: return "Needs approval"
        case .needsInput: return "Waiting on you"
        case .working: return "Running"
        case .done: return "Exited"
        case .errored: return "Error"
        }
    }

    private var color: Color {
        switch attention {
        case .needsApproval: return .orange
        case .needsInput: return .blue
        case .working: return .green
        case .done: return .gray
        case .errored: return .red
        }
    }
}
```

- [ ] **Step 2: Swap it into `SessionRow.swift`** — replace the host-id capsule (`Text(session.hostID)` block, lines 30–34) with:

```swift
            StatusChip(attention: session.attention)
```

Keep the harness capsule and the relative `lastActivity` text as they are.

- [ ] **Step 3: Regenerate, build, test**

```bash
cd "/Volumes/M2 1/drover/apps/drover"
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```
Expected: BUILD SUCCEEDED, all tests pass.

- [ ] **Step 4: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover
git commit -m "feat(ios): per-session status chip replaces host capsule in rows"
```

---

### Task 6: Resilience layer 1 — unreachable banner, retriable load states, bounded requests

Three UI states driven by `SessionStore` flags: (a) never loaded + no error → spinner; (b) never loaded + error → full-screen retriable error (no infinite spinner — the refresh fails fast and lands here); (c) loaded but unreachable → persistent banner + dimmed last-known list. Plus a 15s HTTP timeout so a black-holed request can't hold a state indefinitely.

**Files:**
- Create: `Drover/Screens/Sessions/UnreachableBanner.swift`
- Modify: `Drover/Screens/Sessions/SessionsView.swift` (delete the `lastError` red-label section; add overlay + safeAreaInset + dimming)
- Modify: `NexusKit/Sources/NexusKit/NexusClient.swift` (private `request(path:method:body:)`, line ~179)

**Interfaces:**
- Consumes: `store.hasLoadedOnce`, `store.isReachable`, `store.lastError`, `store.refresh()` (Task 3 + existing).
- Produces: `struct UnreachableBanner: View { let message: String; let retry: () -> Void }`, accessibility id `hub-unreachable-banner`.

- [ ] **Step 1: Create `UnreachableBanner.swift`**

```swift
import SwiftUI

/// Persistent layer-1 banner: the hub is unreachable but the list keeps
/// rendering last-known state beneath it. Auto-retry continues via the
/// store's poll loop; the button is the manual path.
struct UnreachableBanner: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Label(message, systemImage: "wifi.exclamationmark")
                .font(.footnote)
                .lineLimit(2)
            Spacer()
            Button("Retry", action: retry)
                .font(.footnote.weight(.semibold))
                .buttonStyle(.bordered)
                .controlSize(.mini)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.orange.opacity(0.15))
        .accessibilityIdentifier("hub-unreachable-banner")
    }
}
```

- [ ] **Step 2: Wire the three states into `SessionsView.swift`**

Delete the `if let lastError = store.lastError { Section { Label(...) } }` block from the `List`. Then, alongside the existing modifiers on the `List` (order relative to `.navigationTitle` doesn't matter), add:

```swift
        .opacity(store.hasLoadedOnce && !store.isReachable ? 0.5 : 1)
        .safeAreaInset(edge: .top, spacing: 0) {
            if store.hasLoadedOnce && !store.isReachable {
                UnreachableBanner(message: store.lastError ?? "Server unreachable") {
                    Task { await store.refresh() }
                }
            }
        }
        .overlay {
            if !store.hasLoadedOnce {
                if let error = store.lastError {
                    ContentUnavailableView {
                        Label("Can't reach the Drover server", systemImage: "wifi.exclamationmark")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Retry") {
                            Task { await store.refresh() }
                        }
                        .buttonStyle(.borderedProminent)
                    }
                } else {
                    ProgressView("Connecting…")
                }
            }
        }
```

- [ ] **Step 3: Bound HTTP requests** — in `NexusClient.swift`'s private `request(path:method:body:)` helper, after the `URLRequest` is constructed, add:

```swift
        request.timeoutInterval = 15
```

(No unit test: `MockURLProtocol` answers synchronously so a timeout isn't observable there; the 5s poll cadence plus this cap is the no-infinite-spinner guarantee. Do NOT touch `healthz()` or the websocket request builders — the streams own their reconnect timing.)

- [ ] **Step 4: Regenerate, build, run full suite**

```bash
cd "/Volumes/M2 1/drover/apps/drover"
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```
Expected: BUILD SUCCEEDED, all tests pass (StoreTests' error-path test asserts `lastError` content and still holds — the string is now rendered by the banner instead of the list section).

- [ ] **Step 5: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover
git commit -m "feat(ios): hub-unreachable banner, retriable load states, 15s request cap"
```

---

### Task 7: Shared `ReconnectingPill` (layer-3 cleanup)

Chat and terminal each hand-roll an identical pill (`ChatView.swift:181-191`, `TerminalView.swift:147-158`); the chat one has no accessibility id. Extract one component; behavior gating stays where it is.

**Files:**
- Create: `Drover/Screens/Shared/ReconnectingPill.swift` (new `Shared/` directory is fine — XcodeGen globs `[Drover]`)
- Modify: `Drover/Screens/Chat/ChatView.swift` (replace the private `reconnectingPill` computed var and its call site at line ~32)
- Modify: `Drover/Screens/Terminal/TerminalView.swift` (same, call site at line ~55; keep the existing `terminal-reconnecting` id)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `struct ReconnectingPill: View { let accessibilityID: String }`. Chat uses `"chat-reconnecting"`, terminal keeps `"terminal-reconnecting"` (referenced by UI-test expectations; do not rename).

- [ ] **Step 1: Create `ReconnectingPill.swift`**

```swift
import SwiftUI

/// Thin layer-3 "reconnecting…" pill shared by chat and terminal.
/// Visibility gating (hasConnectedOnce / isConnected) stays with each
/// screen; this is presentation only.
struct ReconnectingPill: View {
    let accessibilityID: String

    var body: some View {
        HStack(spacing: 6) {
            ProgressView()
                .scaleEffect(0.7)
            Text("Reconnecting…")
                .font(.caption)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.secondary.opacity(0.15), in: Capsule())
        .accessibilityIdentifier(accessibilityID)
    }
}
```

(If the two existing pills' padding/background differ slightly from the above, match the terminal one — it is the one under UI test.)

- [ ] **Step 2: Replace both private implementations**

- `ChatView.swift`: delete the private `reconnectingPill` (lines ~181–191); at its call site (`if model.hasConnectedOnce && !model.isConnected { … }`, line ~32) use `ReconnectingPill(accessibilityID: "chat-reconnecting")`.
- `TerminalView.swift`: delete the private `reconnectingPill` (lines ~147–158); at its call site (line ~55) use `ReconnectingPill(accessibilityID: "terminal-reconnecting")`.

- [ ] **Step 3: Regenerate, build, run full suite + UI-test build**

```bash
cd "/Volumes/M2 1/drover/apps/drover"
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
xcodebuild -project Drover.xcodeproj -scheme DroverUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build-for-testing
```
Expected: unit tests pass; UI-test target builds (running the E2E suite needs a fresh sim install — that's Task 8's checklist, not here).

- [ ] **Step 4: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover
git commit -m "refactor(ios): extract shared ReconnectingPill; chat pill gains a11y id"
```

---

### Task 8: Verification sweep, docs, tracking issues

**Files:**
- Modify: `apps/drover/README.md` (the "74 tests in 1 suite" baseline line)
- No code changes expected; fixes only if the sweep finds breakage.

**Interfaces:** none.

- [ ] **Step 1: Full clean test run**

```bash
cd "/Volumes/M2 1/drover/apps/drover"
xcodegen generate
xcodebuild -project Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```
Expected: all green. Note the new total test count from the output.

- [ ] **Step 2: Update the README baseline** — in `apps/drover/README.md`, replace the stated expected test count ("74 tests in 1 suite") with the actual new total from Step 1.

- [ ] **Step 3: Sim smoke check (visual, against the live hub)** — per the established runbook (fresh install):

```bash
xcrun simctl uninstall booted com.arnab.drover 2>/dev/null || true
# build & run via Xcode or xcodebuild install, then verify by eye:
```
Checklist: hosts appear as sections ordered online→stale→offline; work-laptop shows the `relay` badge; stale/offline groups are dimmed with "last seen …"; a waiting session sorts to the top of its group with the "Waiting on you" chip; killing the hub (`launchctl` stop on the Mac Mini or airplane-moding the sim's network) shows the banner while the list stays rendered dimmed, and Retry + auto-poll recover it.

- [ ] **Step 4: File the follow-up tracking issues**

```bash
cd "/Volumes/M2 1/drover"
gh issue create --title "Session-row token usage needs a server-side rollup" \
  --body "M3 descope: spec wants compact TokenUsageSummary (tokens + context fill) on fleet session rows, but session rows carry no usage fields — usage lives only in per-message payloads (structured/claude.py:245, codex.py:260). Add a hub-side rollup (e.g. last-usage columns updated on event ingest, additive migration like connection_kind) then decode + render in SessionRow."
gh issue create --title "LaunchModel hides stale hosts from the host picker" \
  --body "LaunchModel.availableHosts filters status == \"online\" (LaunchModel.swift:44), so a direct host that missed 45s of heartbeats silently disappears from the launch sheet. Decide: show stale hosts (selectable with a warning) or keep hiding but surface why."
```

- [ ] **Step 5: Commit docs**

```bash
git add apps/drover/README.md
git commit -m "docs(ios): update unit-test baseline count for fleet UX suite"
```

---

## Self-review (spec ↔ plan)

- **Grouped by host, header with name + status dot + last-seen + relay badge** → Tasks 2–4. ✔
- **Session row: harness icon+name / cwd / status chip** → existing row + Task 5. ✔ **Token usage in row** → descoped, tracked (Task 8 Step 4) — server has no field.
- **Waiting sessions sort to top of their host group** → Task 3 `groupOrdering`. ✔
- **Layer 1 (hub unreachable): persistent banner, auto+manual retry, list dimmed never blanked** → Task 6 (auto-retry = existing 5s poll; keep-last-known already in `SessionStore.refresh`). ✔
- **Layer 2 (host offline): group visible, grayed, last-seen, sessions never vanish** → Tasks 3–4 (incl. synthesized group for orphaned sessions). ✔
- **Layer 3 (stream drop): reconnect + pill + scrollback** → shipped 2026-07-13; Task 7 extracts the shared pill. ✔
- **No infinite spinners: loading states time out into retriable errors** → Task 6 (spinner only pre-first-load; failure lands in `ContentUnavailableView` with Retry; 15s HTTP cap). ✔
- **Type consistency:** `HostPresence` / `presence` / `isRelay` / `title` (Task 2) are the only new `HostSummary` API, used verbatim in Tasks 3, 4. `HostGroup` / `hostGroups(hosts:sessions:)` / `hasLoadedOnce` (Task 3) used verbatim in Tasks 4, 6. `SessionSummary.fixture(hostID:)` added in Task 2, used in Task 3 tests. ✔
