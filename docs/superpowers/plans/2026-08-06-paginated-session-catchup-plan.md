# Paginated Session Catch-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all-at-once transcript downloads and per-message backlog mutations with bounded, compressed pages applied atomically before gap-free WebSocket attachment.

**Architecture:** The registry exposes forward and backward sequence pages with a fixed forward snapshot bound. The HTTP API adds validated cursors and gzip while preserving old-client behavior; DroverKit decodes page metadata, emits REST pages as batches, and applies one observable transcript mutation per page.

**Tech Stack:** Python 3.11+, DuckDB, stdlib HTTP/gzip, pytest, Swift 6, Foundation URLSession, Swift Testing.

## Global Constraints

- Plan 1 must be deployed first; all served events have non-null canonical sequences.
- Existing unpaginated clients continue receiving complete history when `limit` is omitted.
- New page size defaults to 200 and is capped at 500.
- Forward pagination uses one fixed `through_seq` snapshot.
- WebSocket attachment starts only after contiguous REST catch-up.
- REST pages cause one `messagesVersion` increment each.
- Responses remain authenticated and private.
- No external Swift package dependency is added.

---

## File Structure

- `src/drover/server/harness/registry.py`: sequence-page queries and `HarnessEventPage`.
- `src/drover/server/web/app.py`: cursor validation, compatibility response, gzip, and transport timing.
- `tests/test_harness_registry.py`, `tests/test_metrics.py`: page/query/HTTP contract.
- `apps/drover/DroverKit/Sources/DroverKit/Models.swift`: page metadata and lenient message diagnostics.
- `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`: page request API and HTTP measurements.
- `apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift`: fixed-bound catch-up and batch events.
- `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`: ordered batch merge.
- `apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift`, `StreamTests.swift`, `ChatModelDerivedStateTests.swift`: client behavior.
- `apps/drover/DroverKit/Tests/DroverKitTests/SessionLoadPerformanceTests.swift`: production-shaped batch fixture.

### Task 1: Registry sequence pages

**Files:**
- Modify: `src/drover/server/harness/registry.py`
- Modify: `src/drover/server/harness/models.py`
- Test: `tests/test_harness_registry.py`

**Interfaces:**
- Produces `HarnessEventPage(events, page_min_seq, page_max_seq, max_seq, has_older, has_newer)`.
- Produces `list_event_page(session_id: str, *, after_seq: int | None = None, before_seq: int | None = None, through_seq: int | None = None, limit: int | None = None) -> HarnessEventPage`.

- [ ] **Step 1: Write failing forward/backward page tests**

Seed sequences 1–7 and assert:

```python
page = registry.list_event_page("s", after_seq=0, through_seq=5, limit=2)
assert [e.seq for e in page.events] == [1, 2]
assert (page.page_min_seq, page.page_max_seq, page.max_seq) == (1, 2, 5)
assert page.has_older is False
assert page.has_newer is True

tail = registry.list_event_page("s", limit=3)
assert [e.seq for e in tail.events] == [5, 6, 7]
assert tail.has_older is True

older = registry.list_event_page("s", before_seq=5, limit=2)
assert [e.seq for e in older.events] == [3, 4]
```

Also test empty sessions, `before_seq=1`, and a concurrent sequence 8 arriving after `through_seq=7`; forward pages must stop at 7.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_harness_registry.py -k event_page`

Expected: FAIL because the page API does not exist.

- [ ] **Step 3: Implement page queries**

Use an ascending query for forward pages:

```sql
SELECT * FROM harness_events
WHERE session_id=? AND seq>? AND seq<=?
ORDER BY seq ASC LIMIT ?
```

Use a nested descending query for backward pages:

```sql
SELECT * FROM (
  SELECT * FROM harness_events
  WHERE session_id=? AND seq<?
  ORDER BY seq DESC LIMIT ?
) page ORDER BY seq ASC
```

Fetch `limit + 1` rows to derive `has_newer`/`has_older` without a second count query. `max_seq` is the supplied `through_seq` or the session maximum captured at the start of the call.

- [ ] **Step 4: Run registry tests and commit**

Run: `uv run --extra dev python -m pytest -q tests/test_harness_registry.py`

Expected: PASS.

```bash
git add src/drover/server/harness/models.py src/drover/server/harness/registry.py tests/test_harness_registry.py
git commit -m "feat(harness): page session events by sequence"
```

### Task 2: Compatible paginated and compressed HTTP API

**Files:**
- Modify: `src/drover/server/web/app.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes `HarnessRegistry.list_event_page` from Task 1.
- Produces query parameters `after_seq`, `before_seq`, `through_seq`, `limit`.
- Produces response keys `messages`, `page_min_seq`, `page_max_seq`, `max_seq`, `has_older`, `has_newer`.

- [ ] **Step 1: Write failing HTTP contract tests**

Add tests for newest-tail, older, limited-forward, fixed-through, and invalid combinations. Preserve this old-client assertion:

```python
with _authed_get(f"{base}/harness/sessions/s/messages?after_seq=0") as response:
    body = json.loads(response.read())
assert [m["seq"] for m in body["messages"]] == list(range(1, 8))
assert body["max_seq"] == 7
```

Add gzip coverage using `Accept-Encoding: gzip`; assert `Content-Encoding: gzip`, decompress, and compare JSON with the identity response. Assert bodies below 1 KiB remain uncompressed.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_metrics.py -k 'messages_endpoint and (page or gzip or limit or through)'`

Expected: FAIL because pagination metadata and gzip are absent.

- [ ] **Step 3: Implement validation and compatibility mode**

Add pure helpers:

```python
_MESSAGE_PAGE_DEFAULT = 200
_MESSAGE_PAGE_MAX = 500

@dataclass(frozen=True)
class MessagePageQuery:
    after_seq: int | None
    before_seq: int | None
    through_seq: int | None
    limit: int | None

def _parse_optional_nonnegative_int(
    params: dict[str, list[str]], name: str
) -> int | None:
    values = params.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must appear once")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value

def _parse_message_page_query(params: dict[str, list[str]]) -> MessagePageQuery:
    query = MessagePageQuery(
        after_seq=_parse_optional_nonnegative_int(params, "after_seq"),
        before_seq=_parse_optional_nonnegative_int(params, "before_seq"),
        through_seq=_parse_optional_nonnegative_int(params, "through_seq"),
        limit=_parse_optional_nonnegative_int(params, "limit"),
    )
    if query.after_seq is not None and query.before_seq is not None:
        raise ValueError("after_seq and before_seq are mutually exclusive")
    if query.through_seq is not None and query.after_seq is None:
        raise ValueError("through_seq requires after_seq")
    if query.limit == 0 or (query.limit is not None and query.limit > _MESSAGE_PAGE_MAX):
        raise ValueError(f"limit must be between 1 and {_MESSAGE_PAGE_MAX}")
    return query
```

When `limit`, `before_seq`, and `through_seq` are all absent, retain the current complete `after_seq` behavior. Otherwise enforce the page cap and serialize Task 1's metadata.

- [ ] **Step 4: Add selective gzip and response metrics**

Extend `_send` with `allow_gzip: bool = False`. Compress only when allowed, body length is at least 1,024 bytes, and `Accept-Encoding` contains `gzip`. Set `Vary: Accept-Encoding`, `Content-Encoding: gzip`, and the compressed `Content-Length`. Record structured log fields for route class, status, uncompressed bytes, transferred bytes, and elapsed milliseconds without logging the session ID or content.

- [ ] **Step 5: Run HTTP tests and commit**

Run: `uv run --extra dev python -m pytest -q tests/test_metrics.py`

Expected: PASS.

```bash
git add src/drover/server/web/app.py tests/test_metrics.py
git commit -m "feat(api): paginate and compress session history"
```

### Task 3: Swift page models and client requests

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/Models.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ModelsTests.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift`

**Interfaces:**
- Produces `MessagePage` with `messages`, `pageMinSeq`, `pageMaxSeq`, `maxSeq`, `hasOlder`, `hasNewer`, and `decodeIssues`.
- Produces `MessagePageRequest` enum cases `.newest(limit:)`, `.older(beforeSeq:limit:)`, `.newer(afterSeq:throughSeq:limit:)`.
- Produces `DroverClient.messagePage(sessionID:request:) async throws -> MessagePage`.

- [ ] **Step 1: Write failing decode and request-shape tests**

Assert the new response decodes, legacy `{messages,max_seq}` still decodes with false page flags, and each enum case produces the exact query string. Include a malformed element between valid sequences and assert one `MessageDecodeIssue` is returned rather than blanking the page.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter 'ModelsTests|ClientTests'`

Expected: FAIL because the page types and client method are absent.

- [ ] **Step 3: Implement page types and diagnostic lenient decoding**

Define:

```swift
public struct MessageDecodeIssue: Sendable, Equatable {
    public let index: Int
    public let seq: Int?
    public let detail: String
}

public struct MessagePage: Sendable {
    public let messages: [HarnessMessage]
    public let pageMinSeq: Int?
    public let pageMaxSeq: Int?
    public let maxSeq: Int
    public let hasOlder: Bool
    public let hasNewer: Bool
    public let decodeIssues: [MessageDecodeIssue]
}
```

Retain the raw decoding error and, when recoverable, the raw integer `seq` inside `LenientElement` so `MessagePage.decode` can surface indexed diagnostics. Do not include raw JSON or message text in the diagnostic.

- [ ] **Step 4: Implement typed URL construction**

Build `URLComponents.queryItems` rather than concatenating user-controlled strings. Keep `messages(sessionID:afterSeq:)` as a compatibility wrapper around an unlimited legacy request until Task 4 switches the stream.

- [ ] **Step 5: Run DroverKit model/client tests and commit**

Run: `swift test --filter 'ModelsTests|ClientTests'`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/Models.swift apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift apps/drover/DroverKit/Tests/DroverKitTests/ModelsTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift
git commit -m "feat(ios): decode paginated session history"
```

### Task 4: Fixed-bound REST catch-up and batch stream events

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/StreamTests.swift`

**Interfaces:**
- Changes `StreamEvent.message(HarnessMessage)` to retain live delivery.
- Adds `StreamEvent.history([HarnessMessage], decodeIssues: [MessageDecodeIssue])`.
- Produces contiguous fixed-bound catch-up with page size 200.

- [ ] **Step 1: Write failing multi-page and race tests**

Mock pages `[1,2] max=5`, `[3,4] through=5`, `[5]`, then a WebSocket frame 6. Assert emitted events are three history batches followed by message 6, the WebSocket request uses `after_seq=5`, and no duplicate is delivered.

Add a page with sequences `[1,3]`; assert the stream emits a connection failure and retries from zero without attaching WebSocket. Add a malformed issue at sequence 2 and assert the same gap behavior.

- [ ] **Step 2: Run tests and confirm failure**

Run: `swift test --filter StreamTests`

Expected: FAIL because history batching and page iteration do not exist.

- [ ] **Step 3: Implement `catchUp()` as a private actor method**

Use this contract:

```swift
private func catchUp(
    continuation: AsyncStream<StreamEvent>.Continuation
) async throws -> Int
```

The first request captures `maxSeq`; later requests use the same value as `throughSeq`. Validate that every message equals `lastSeq + 1` before yielding the page. Update `lastSeq` only after validating the complete page. Return the fixed maximum for WebSocket attachment.

- [ ] **Step 4: Keep live delivery incremental**

After catch-up, call `streamRequest(sessionID:afterSeq: fixedMaxSeq)`. Continue using the existing `deliver` guard for live frames. Reconnect starts forward catch-up from the current contiguous `lastSeq` and captures a new bound.

- [ ] **Step 5: Run stream tests and commit**

Run: `swift test --filter StreamTests`

Expected: PASS.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/MessageStream.swift apps/drover/DroverKit/Tests/DroverKitTests/StreamTests.swift
git commit -m "perf(ios): batch paginated session catch-up"
```

### Task 5: Atomic `ChatModel` history merge

**Files:**
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/SessionLoadPerformanceTests.swift`

**Interfaces:**
- Consumes `StreamEvent.history` from Task 4.
- Produces `mergeHistory(_ incoming: [HarnessMessage])` with one version bump.
- Produces internal counters `historyPagesMerged` and `lastHistoryMergeDuration` for signpost tests; production instrumentation uses `OSSignposter` when available.

- [ ] **Step 1: Write failing atomic-merge tests**

Assert a 200-message history event increments `messagesVersion` once, sorts and deduplicates overlapping sequences, rebuilds pending approval correctly, and does not dispatch queued turns from historical completion statuses.

- [ ] **Step 2: Add a production-shaped performance fixture**

Generate 3,316 messages with representative status, thinking, tool, and prose payload sizes. The test measures the merge itself and asserts one version mutation; use `measure`/clock reporting rather than a brittle CI wall-clock failure. Keep the physical-device thresholds for the deployment gate.

- [ ] **Step 3: Run tests and confirm failure**

Run: `swift test --filter 'ChatModelDerivedStateTests|SessionLoadPerformanceTests'`

Expected: FAIL because `history` is not handled atomically.

- [ ] **Step 4: Implement ordered deduplicating batch merge**

For append-only forward pages, compare against the current last sequence and append only greater sequences. For a replacement/overlap path, merge by `seq` in a dictionary and sort once. Call `rebuildApprovals()` once, increment `messagesVersion` once, and never call `dispatchQueuedTurnIfComplete` for backlog messages.

- [ ] **Step 5: Run the full DroverKit suite and commit**

Run: `swift test`

Expected: all tests pass.

```bash
git add apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift apps/drover/DroverKit/Tests/DroverKitTests/ChatModelDerivedStateTests.swift apps/drover/DroverKit/Tests/DroverKitTests/SessionLoadPerformanceTests.swift
git commit -m "perf(ios): apply history pages atomically"
```

### Task 6: Integration and production measurement

**Files:**
- No source changes unless instrumentation reveals a defect, which must get its own failing test and commit.

- [ ] **Step 1: Run complete automated verification**

```bash
uv run --extra dev python -m pytest -q
swift test --package-path apps/drover/DroverKit
uv run --extra dev python scripts/check_public_release.py
```

Expected: all tests pass and public audit has zero findings.

- [ ] **Step 2: Benchmark server pages**

Against copies of the 3,316-event/4 MB and 2,024-event/10 MB sessions, record identity versus gzip transferred bytes, time to first byte, total time, and page query duration for newest, older, and forward catch-up.

- [ ] **Step 3: Deploy server before client**

Restart server/harness services on the reviewed commit. Verify the old installed app can still open a session through the unpaginated compatibility route.

- [ ] **Step 4: Deploy a release-built iOS client**

Use `scripts/deploy-ios.sh`, confirm the installed build contains the reviewed commit identifier in deployment notes, and exercise cold opens of the two production-shaped sessions.

- [ ] **Step 5: Confirm rollout gates**

Assert cold first content is below 500 ms on the private network, each REST page produces one model mutation, transferred bytes reflect gzip, WebSocket follows the fixed bound without gaps, and server logs no full socket traceback when navigating away mid-response.
