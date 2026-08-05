# Session Cards And Context Gauge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build CloudGuard-style session cards with useful preview metadata and correct misleading Codex context usage.

**Architecture:** Extend the harness snapshot contract at the server boundary, decode the new metadata in NexusKit, and keep session-card rendering presentational in the iOS app. Context gauge parsing remains centralized in `ContextGauge`.

**Tech Stack:** Python HTTP metrics surface, DuckDB-backed harness registry, Swift 6 NexusKit, SwiftUI iOS app, xcodegen project generation.

## Global Constraints

- Do not print tokens, API keys, or raw transcripts during verification.
- Preserve older-server compatibility by making new Swift fields optional.
- Keep finished sessions separated from active sessions.
- Hide unsupported context gauges rather than displaying cumulative usage as live context.

---

### Task 1: Harness Snapshot Metadata

**Files:**
- Modify: `src/drover/server/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: snapshot session dictionaries with `started_at`, `updated_at`, `ended_at`, `last_activity`, and `preview` keys.
- Produces: host dictionaries with timezone-safe `last_seen_at`.

- [ ] **Step 1: Write failing tests**

Add tests that create a session/event in a temporary harness registry, call `MetricsCollector.render_harness_json()`, and assert:

```python
assert session["last_activity"].endswith("+00:00") or session["last_activity"].endswith("-07:00")
assert session["preview"] == "Refactor session screen cards"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_metrics.py -q`

Expected: the new assertions fail because snapshot timestamps are naive strings and `preview` is absent.

- [ ] **Step 3: Implement minimal server changes**

Add private helpers in `metrics.py`:

```python
def _wire_datetime(value: Any) -> str | None: ...
def _harness_session_dict(session: Any, events_by_session: Mapping[str, list[Any]]) -> dict[str, Any]: ...
def _session_preview(events: list[Any]) -> str | None: ...
```

Use these helpers from `harness_snapshot()`.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/test_metrics.py -q`

Expected: tests pass.

### Task 2: NexusKit Models And Context Gauge

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/Models.swift`
- Modify: `apps/drover/NexusKit/Sources/NexusKit/ContextGauge.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/ModelsTests.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/ContextGaugeTests.swift`

**Interfaces:**
- Consumes: optional `preview`, `started_at`, `updated_at`, and `ended_at` fields from the server.
- Produces: `SessionSummary.preview`, `startedAt`, `updatedAt`, and `endedAt`.
- Produces: `ContextGauge.init?(messages:harness:)`.

- [ ] **Step 1: Write failing Swift tests**

Add model decoding assertions for `preview` and timezone-safe dates. Add a context test where a Codex status message with cumulative `input_tokens` returns nil when initialized with `harness: "codex"`.

- [ ] **Step 2: Verify red**

Run: `swift test --disable-sandbox --package-path apps/drover/NexusKit --filter 'ModelsTests|ContextGaugeTests'`

Expected: tests fail because fields and initializer are absent.

- [ ] **Step 3: Implement minimal Swift model/parser changes**

Decode the new optional fields and route chat model context gauge construction through `ContextGauge(messages:harness:)`.

- [ ] **Step 4: Verify green**

Run: `swift test --disable-sandbox --package-path apps/drover/NexusKit --filter 'ModelsTests|ContextGaugeTests'`

Expected: tests pass.

### Task 3: SwiftUI Session Cards

**Files:**
- Modify: `apps/drover/Drover/Screens/Sessions/SessionsView.swift`
- Modify: `apps/drover/Drover/Screens/Sessions/SessionRow.swift`

**Interfaces:**
- Consumes: `SessionSummary.preview`, `startedAt`, `lastActivity`, `hostID`, `cwd`, `harness`, `attention`, and `status`.
- Produces: flat active card feed plus collapsed finished section and bottom-left launch button.

- [ ] **Step 1: Implement card presentation**

Replace `List` host sections with `ScrollView` and `LazyVStack`, keep navigation/context-menu behavior, and render `SessionRow` as a glass card.

- [ ] **Step 2: Move launch action**

Remove the top trailing plus toolbar item and add a bottom-left floating glass “New session” button that opens the existing `LaunchView` sheet.

- [ ] **Step 3: Verify formatting and build**

Run:

```bash
git diff --check
cd apps/drover && xcodegen generate
swift test --disable-sandbox --package-path NexusKit
xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build -quiet
```

Expected: all commands pass.

## Self-Review

- Spec coverage: snapshot metadata, timestamp fix, card UI, and Codex gauge handling all have tasks.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: server wire keys match Swift coding keys and UI references.
