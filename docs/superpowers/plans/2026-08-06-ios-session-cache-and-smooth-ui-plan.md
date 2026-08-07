# iOS Session Cache and Smooth UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repeat session opens effectively immediate and keep loading, scrolling, and typing smooth for multi-thousand-message transcripts.

**Architecture:** A rebuildable SQLite cache stores canonical wire messages by server/session/sequence and hydrates a bounded tail before network catch-up. A session loader coordinates cache, forward catch-up, WebSocket, and older-page requests; incremental transcript folding, demand-driven presentation parsing, and an isolated composer remove remaining main-thread invalidation.

**Tech Stack:** Swift 6, SwiftUI, Observation, Foundation, OSLog signposts, system SQLite3, Swift Testing, iOS 18+.

## Global Constraints

- Plans 1 and 2 must be deployed first.
- DuckDB remains authoritative; the iOS cache is always deletable and rebuildable.
- Cache identity is a stable hash of the canonical server base URL; tokens are never cached.
- Cache primary key is `(server_key, session_id, seq)` and `event_id` is unique per server.
- Cache ceiling is 150 MB; evict least-recently-opened completed sessions first.
- Newest and older page size is 200; server maximum is 500.
- Cached first content target is p95 below 100 ms.
- Composer keystroke-to-frame target is p95 below 16 ms.
- No external Swift package dependency is added.

---

## File Structure

- `apps/drover/DroverKit/Package.swift`: link the system `sqlite3` library.
- `apps/drover/DroverKit/Sources/DroverKit/MessageCache.swift`: cache protocol and value types.
- `apps/drover/DroverKit/Sources/DroverKit/SQLiteMessageCache.swift`: SQLite schema, transactions, range reads, divergence reset, and eviction.
- `apps/drover/DroverKit/Sources/DroverKit/SessionLoader.swift`: cache-first open, REST reconciliation, WebSocket handoff, and older-page single flight.
- `apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift`: accept loader-provided starting sequence and live-only mode.
- `apps/drover/DroverKit/Sources/DroverKit/TranscriptAccumulator.swift`: incremental folding state.
- `apps/drover/DroverKit/Sources/DroverKit/MessagePresentationCache.swift`: bounded off-main display parsing.
- `apps/drover/DroverKit/Sources/DroverKit/ComposerModel.swift`: isolated draft/attachment state.
- `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`: coordinate loader, accumulator, and immutable send drafts.
- `apps/drover/Drover/Screens/Chat/ChatView.swift`, `Composer.swift`, `MessageBubble.swift`, `StepRunCard.swift`: UI integration, scroll anchoring, and presentation cache.
- Corresponding focused test files under `apps/drover/DroverKit/Tests/DroverKitTests/`.

### Task 1: SQLite message cache

**Files:**
- Modify: `apps/drover/DroverKit/Package.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/MessageCache.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/SQLiteMessageCache.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/MessageCacheTests.swift`

**Interfaces:**
- Produces `CachedSessionPage(messages, minSeq, maxSeq, hasOlder)`.
- Produces `serverKey(for baseURL: URL) -> String`, a SHA-256 of the lowercased scheme/host, explicit-or-default port, and normalized path.
- Produces async protocol methods `newest`, `older`, `upsert`, `replaceSession`, `markCompleted`, and `evictIfNeeded`.
- Stores canonical encoded `HarnessMessage` wire JSON, not presentation values.

- [ ] **Step 1: Write failing cache contract tests**

Use a temporary database and test:

```swift
let cache = try SQLiteMessageCache(url: temporaryURL, byteLimit: 150 * 1024 * 1024)
try await cache.upsert(serverKey: "server", sessionID: "s", messages: messages(1...250))
let newest = try await cache.newest(serverKey: "server", sessionID: "s", limit: 200)
#expect(newest.messages.map(\.seq) == Array(51...250))
let older = try await cache.older(serverKey: "server", sessionID: "s", beforeSeq: 51, limit: 200)
#expect(older.messages.map(\.seq) == Array(1...50))
```

Also test canonical server-key normalization, overlapping upserts, `event_id` collision, transaction rollback, corrupt row isolation, schema-version rebuild, sequence-regression replacement, 150 MB LRU eviction, completed-session preference, and protection of the open session.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter MessageCacheTests`

Expected: FAIL because cache types do not exist.

- [ ] **Step 3: Define cache-neutral types**

In `MessageCache.swift`:

```swift
public struct CachedSessionPage: Sendable {
    public let messages: [HarnessMessage]
    public let minSeq: Int?
    public let maxSeq: Int?
    public let hasOlder: Bool
}

public protocol MessageCaching: Sendable {
    func newest(serverKey: String, sessionID: String, limit: Int) async throws -> CachedSessionPage
    func older(serverKey: String, sessionID: String, beforeSeq: Int, limit: Int) async throws -> CachedSessionPage
    func upsert(serverKey: String, sessionID: String, messages: [HarnessMessage]) async throws
    func replaceSession(serverKey: String, sessionID: String, messages: [HarnessMessage]) async throws
    func markCompleted(serverKey: String, sessionID: String) async throws
    func evictIfNeeded(excludingSessionID: String?) async throws
}
```

Implement `serverKey(for:)` with CryptoKit's SHA-256 over the canonical URL string; do not include user info, query, fragment, or bearer token.

- [ ] **Step 4: Implement SQLite actor and schema**

Link `.linkedLibrary("sqlite3")`. Wrap the connection in an actor and use prepared statements for:

```sql
CREATE TABLE cache_meta(schema_version INTEGER NOT NULL);
CREATE TABLE cache_sessions(
  server_key TEXT NOT NULL, session_id TEXT NOT NULL,
  min_seq INTEGER, max_seq INTEGER, last_opened REAL NOT NULL,
  is_completed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(server_key, session_id)
);
CREATE TABLE cache_messages(
  server_key TEXT NOT NULL, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
  event_id TEXT NOT NULL, wire_json BLOB NOT NULL,
  PRIMARY KEY(server_key, session_id, seq),
  UNIQUE(server_key, event_id)
);
CREATE INDEX cache_messages_desc ON cache_messages(server_key,session_id,seq DESC);
```

Use `JSONEncoder`/`JSONDecoder` on a cache DTO whose keys match the wire contract; add `Encodable` only to the DTO, not UI presentation types. `markCompleted` flips `is_completed=1`; eviction orders completed sessions before running sessions and then by `last_opened`, while always excluding the current session ID.

- [ ] **Step 5: Run cache tests and commit**

Run: `swift test --filter MessageCacheTests`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Package.swift apps/drover/DroverKit/Sources/DroverKit/MessageCache.swift apps/drover/DroverKit/Sources/DroverKit/SQLiteMessageCache.swift apps/drover/DroverKit/Tests/DroverKitTests/MessageCacheTests.swift
git commit -m "feat(ios): add persistent session message cache"
```

### Task 2: Cache-first session loader

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/SessionLoader.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/SessionLoaderTests.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift`

**Interfaces:**
- Produces `SessionLoadEvent.cached([HarnessMessage])`, `.history([HarnessMessage])`, `.older([HarnessMessage])`, `.live(HarnessMessage)`, `.connection(Bool)`, `.unauthorized`.
- Produces `SessionLoader.events() -> AsyncStream<SessionLoadEvent>`.
- Produces `SessionLoader.loadOlder(beforeSeq: Int) async` with one request in flight.

- [ ] **Step 1: Write failing loader ordering tests**

Use fake cache/client/socket implementations. Assert cached messages are emitted before a delayed network response, cache `maxSeq=80` causes forward request `after_seq=80`, server `max_seq=100` catches up in one or more pages, then WebSocket starts at 100. Assert cache write failure still emits network history and live events.

Add divergence: cached max 100/server max 80 causes `replaceSession` with the newest server page. Add cancellation and older-page single-flight tests.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter SessionLoaderTests`

Expected: FAIL because `SessionLoader` does not exist.

- [ ] **Step 3: Implement cache-first orchestration**

Define an actor that owns one load task and one older-page task. The open algorithm is:

```swift
let cached = try? await cache.newest(serverKey: serverKey, sessionID: sessionID, limit: 200)
if let cached, !cached.messages.isEmpty { continuation.yield(.cached(cached.messages)) }
let first = try await client.messagePage(
    sessionID: sessionID,
    request: cached?.maxSeq.map { .newer(afterSeq: $0, throughSeq: nil, limit: 200) }
        ?? .newest(limit: 200)
)
```

Continue fixed-bound forward pages, upsert before publishing each network page, then start live streaming after the fixed maximum. Cache errors are logged as non-sensitive counters and never terminate the stream.

- [ ] **Step 4: Split MessageStream into catch-up and live seams**

Retain the Plan 2 combined initializer for compatibility tests. Add an internal live-only method taking `afterSeq`; `SessionLoader` uses it after cache/REST reconciliation so history is not fetched twice.

- [ ] **Step 5: Run loader/stream tests and commit**

Run: `swift test --filter 'SessionLoaderTests|StreamTests'`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/SessionLoader.swift apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift apps/drover/DroverKit/Tests/DroverKitTests/SessionLoaderTests.swift apps/drover/DroverKit/Tests/DroverKitTests/StreamTests.swift
git commit -m "feat(ios): hydrate chats from cache before catch-up"
```

### Task 3: ChatModel loader integration and older-page anchoring

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift`
- Test: `apps/drover/DroverUITests/E2EValidationUITests.swift`

**Interfaces:**
- Consumes `SessionLoadEvent` from Task 2.
- Produces `canLoadOlder`, `isLoadingOlder`, and `loadOlder() async`.
- Produces prepend merge that preserves the first previously visible row ID.

- [ ] **Step 1: Write failing model prepend tests**

Assert cached/history replacement, forward append, older prepend, overlap dedupe, one version increment per page, and stable `latestRowID`. Verify repeated `loadOlder()` calls while one is active make one loader request.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter ChatModelDerivedStateTests`

Expected: FAIL because loader events and older state are absent.

- [ ] **Step 3: Integrate loader events**

Replace the direct `MessageStream` pump with `SessionLoader`. Cached initial tail replaces empty state; forward pages append; older pages prepend. Every branch merges once and bumps `messagesVersion` once.

- [ ] **Step 4: Add scroll-triggered older loading with anchor preservation**

In `ChatView`, detect proximity within 200 points of the top. Before loading, capture the first visible transcript row ID; after prepend/layout, call `proxy.scrollTo(anchorID, anchor: .top)` without animation. Do not change `isPinnedToBottom` during older-page insertion.

- [ ] **Step 5: Add one UI smoke test and commit**

Seed more than one page, open at the bottom, scroll to top, wait for the older-page accessibility marker, and assert the preexisting anchor row remains visible.

Run: `swift test --package-path apps/drover/DroverKit` and build the UI-test target.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift apps/drover/DroverUITests/E2EValidationUITests.swift
git commit -m "feat(ios): page older transcript rows without jumping"
```

### Task 4: Incremental transcript accumulator

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/TranscriptAccumulator.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/Transcript.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/TranscriptAccumulatorTests.swift`

**Interfaces:**
- Produces `TranscriptAccumulator.replace(with:)`, `append(_:)`, `append(contentsOf:)`, and read-only `items`/`latestRowID`.
- Maintains pending tool-step indexes, thinking/status run state, and latest rendered row without rescanning prior messages.

- [ ] **Step 1: Write differential tests against the reference fold**

For every prefix of existing transcript fixtures, append one message to an accumulator and assert:

```swift
#expect(accumulator.items == TranscriptItem.group(prefix))
#expect(accumulator.latestRowID == TranscriptItem.latestRowID(of: prefix))
```

Include tool results across intervening messages, status/thinking token runs, unmatched tools, replacement, and page batches.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter TranscriptAccumulatorTests`

Expected: FAIL because accumulator is absent.

- [ ] **Step 3: Extract reusable fold state and implement append**

Move the existing private fold state into `TranscriptAccumulator` without changing `TranscriptItem.group` semantics. Keep `group` as a reference implementation that constructs an accumulator, replaces all messages, and returns its items.

- [ ] **Step 4: Use accumulator in ChatModel**

Replace `itemsCache` and `rowIDCache` with one ignored accumulator. Forward pages and live messages append incrementally; cache replacement and older prepends call `replace(with:)`. Artifact and context-gauge caches remain versioned because they have separate semantics.

- [ ] **Step 5: Run transcript/model suites and commit**

Run: `swift test --filter 'TranscriptAccumulatorTests|TranscriptGroupingTests|StepRunGroupingTests|ChatModelDerivedStateTests'`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/TranscriptAccumulator.swift apps/drover/DroverKit/Sources/DroverKit/Transcript.swift apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/DroverKit/Tests/DroverKitTests/TranscriptAccumulatorTests.swift
git commit -m "perf(ios): fold transcript incrementally"
```

### Task 5: Demand-driven presentation parsing

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/MessagePresentationCache.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/Models.swift`
- Modify: `apps/drover/Drover/Screens/Chat/MessageBubble.swift`
- Modify: `apps/drover/Drover/Screens/Chat/StepRunCard.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/MessagePresentationCacheTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ModelsTests.swift`

**Interfaces:**
- `HarnessMessage` retains raw wire fields only.
- Produces `MessagePresentation(displayText: AttributedString, displayBlocks: [DisplayBlock])`.
- Produces actor `MessagePresentationCache.presentation(for:) async -> MessagePresentation` with bounded LRU capacity 400.

- [ ] **Step 1: Write failing laziness and dedupe tests**

Inject a parser spy. Decode 3,316 messages and assert zero parser calls. Request presentations for ten IDs and assert ten calls; request them again and assert no additional calls. Concurrent requests for one ID must share one in-flight parse.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter 'MessagePresentationCacheTests|ModelsTests'`

Expected: FAIL because decoding still parses every message.

- [ ] **Step 3: Remove eager presentation fields from HarnessMessage**

Delete decode-time `displayText` and `displayBlocks`. Move the exact existing `DisplayBlock.parseInlineMarkdown` and `DisplayBlock.segment` calls behind the new cache actor. Update model tests to test the presentation cache rather than eager message fields.

- [ ] **Step 4: Integrate row-local async presentation**

Add a small row model/state that requests cached presentation in `.task(id: message.id)`. Render raw plain text until parsed content arrives, without changing row identity. In `ChatView`, prewarm only the newest 200 message IDs after a page merge.

- [ ] **Step 5: Run display/transcript tests and commit**

Run: `swift test --filter 'MessagePresentationCacheTests|DisplayBlocksTests|DisplayBlocksMarkdownTests|StepRunGroupingTests'`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/MessagePresentationCache.swift apps/drover/DroverKit/Sources/DroverKit/Models.swift apps/drover/Drover/Screens/Chat/MessageBubble.swift apps/drover/Drover/Screens/Chat/StepRunCard.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverKit/Tests/DroverKitTests/MessagePresentationCacheTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ModelsTests.swift
git commit -m "perf(ios): parse transcript presentation on demand"
```

### Task 6: Isolated composer model

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/ComposerModel.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/ComposerModelTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`

**Interfaces:**
- Produces `ComposerDraft(text: String, attachments: [TurnAttachment])`.
- Produces `@MainActor @Observable ComposerModel` owning text, attachments, model, and thinking effort.
- Changes send API to `ChatModel.sendTurn(_ draft: ComposerDraft, model: String?, thinkingEffort: String?) async -> SendOutcome`.

- [ ] **Step 1: Write failing draft lifecycle tests**

Assert snapshotting is immutable, successful send clears only the matching draft revision, transport failure preserves it, a 409 queues it, and typing after send begins is not erased when the earlier request succeeds.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter 'ComposerModelTests|ChatModelTests'`

Expected: FAIL because composer state is owned by ChatModel.

- [ ] **Step 3: Implement revisioned ComposerModel**

Track a monotonically increasing revision. `snapshot()` returns `(revision, draft, selectedModel, thinkingEffort)`. `clear(ifRevision:)` clears only if the user has not typed or attached something newer while the send awaited.

- [ ] **Step 4: Move bindings into the composer subtree**

`ChatView` owns `@State private var composerModel`. `Composer` binds directly to it. The transcript subtree receives only transcript-related values; it must not read composer text or attachments. ChatModel accepts immutable send inputs and returns an outcome the composer applies. When the session snapshot transitions to a terminal state, ChatModel calls `cache.markCompleted` so eviction has authoritative completion data.

- [ ] **Step 5: Add observation regression test and commit**

Use `withObservationTracking` around `model.items`; mutate `composerModel.text` through 100 characters and assert transcript tracking is never invalidated and `messagesVersion` remains unchanged.

Run: `swift test --filter 'ComposerModelTests|ChatModelTests|ChatModelDerivedStateTests'`.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/ComposerModel.swift apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/Drover/Screens/Chat/Composer.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/DroverKit/Tests/DroverKitTests/ComposerModelTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift
git commit -m "perf(ios): isolate composer state from transcript"
```

### Task 7: Instrumentation, feature flag, and device acceptance

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/SessionLoadMetrics.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/SessionLoader.swift`
- Modify: `apps/drover/Drover/AppEnvironment.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/SessionLoadMetricsTests.swift`
- Modify: `apps/drover/DroverUITests/E2EValidationUITests.swift`

**Interfaces:**
- Produces signposts `cache_lookup`, `cache_hydrate`, `rest_page`, `decode`, `model_merge`, `first_content`, `live_attached`, and `older_page`.
- Produces local flag `sessionCacheEnabled`, default true only for Debug during dogfood; rollback switches to Plan 2's paginated nonpersistent path.

- [ ] **Step 1: Write failing metrics privacy tests**

Inject a recorder and assert event names/durations/counts are recorded while session ID, message text, server token, attachment data, and raw URL query values never appear.

- [ ] **Step 2: Implement signpost wrapper and feature flag**

Use `OSSignposter` behind a protocol so tests receive deterministic records. `AppEnvironment` constructs either `SQLiteMessageCache` or a `NullMessageCache`; both feed the same `SessionLoader` interface.

- [ ] **Step 3: Run full automated verification**

```bash
swift test --package-path apps/drover/DroverKit
uv run --extra dev python -m pytest -q
uv run --extra dev python scripts/check_public_release.py
```

Expected: all tests pass and public audit has zero findings.

- [ ] **Step 4: Run physical-device release measurements**

Measure the sanitized 3,316-event/4 MB and 2,024-event/10 MB sessions for cold open, warm open, upward page, live append, 100-character typing burst, memory high-water mark, cache corruption rebuild, sequence regression rebuild, and eviction.

- [ ] **Step 5: Apply acceptance thresholds**

Cached first content must be p95 below 100 ms, cold first content below 500 ms, typing below one 16 ms frame at p95, no backlog merge main-thread stall above 50 ms, and no blank transcript when server or cache has messages.

- [ ] **Step 6: Deploy or roll back the flag**

If every threshold passes for both large sessions, enable `sessionCacheEnabled` for the dogfood build and deploy with `scripts/deploy-ios.sh`. If any threshold fails, leave the cache flag disabled, retain Plan 2 pagination/batching, and attach the signpost trace to the next focused optimization task.

- [ ] **Step 7: Commit**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/SessionLoadMetrics.swift apps/drover/DroverKit/Sources/DroverKit/SessionLoader.swift apps/drover/Drover/AppEnvironment.swift apps/drover/DroverKit/Tests/DroverKitTests/SessionLoadMetricsTests.swift apps/drover/DroverUITests/E2EValidationUITests.swift
git commit -m "feat(ios): measure and gate cached session loading"
```
