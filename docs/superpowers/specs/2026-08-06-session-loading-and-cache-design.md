# Session Loading and Cache Design

Date: 2026-08-06
Status: Approved direction; implementation pending

## Summary

Drover will repair historical session visibility and make long conversations feel immediate by combining four changes:

1. migrate legacy harness events onto the sequenced message protocol;
2. stop unbounded summarizer retries that compete with interactive traffic;
3. load a bounded transcript tail first and ingest REST history as one observable batch;
4. keep an append-only iOS message cache so repeat opens render before the network round trip.

The command-plane DuckDB remains the durable source of truth. The iOS cache is a rebuildable serving layer, not a second authority. Redis is not introduced for transcript loading.

## Problem Statement

Production evidence identified two independent problems.

First, 31 legacy sessions contain 3,162 `harness_events` rows whose `seq` column is null. `HarnessRegistry.list_events_after` excludes null sequences, so the history endpoint returns an empty message list for sessions that still have valid transcript rows.

Second, current sessions can contain 1,300–3,300 events and 4–10 MB of JSON. Opening a session requests the entire history in one response. The client decodes and preprocesses every message, emits every backlog item individually onto the main actor, and makes SwiftUI reconcile the growing transcript. The existing `messagesVersion` caches remove repeated derived-state work caused by scrolling and typing, but a transcript mutation still invalidates those caches. The cold-load burst therefore remains visible.

The server also disables HTTP caching for every `/harness` response, and a failing summarization job has retried thousands of times. Neither is the primary blank-session cause, but both work against smooth interactive behavior.

## Goals

- Every session with recorded transcript events loads those events.
- A repeat open shows cached content without waiting for the network.
- A cold open shows the newest useful transcript tail without downloading the entire history.
- Catch-up and live WebSocket delivery remain gap-free and duplicate-free.
- Typing in the composer does not invalidate the transcript view hierarchy.
- The cache can be deleted at any time without losing durable data.
- Interactive command-plane traffic is insulated from runaway background retries.

Target measurements on the production fleet:

- cached first content: p95 below 100 ms;
- cold first content: p95 below 500 ms on the private network;
- composer keystroke-to-frame: p95 below 16 ms;
- no main-thread stall above 50 ms during backlog hydration;
- no blank transcript when durable events exist.

## Non-Goals

- Replacing DuckDB as the command-plane source of truth.
- Caching terminal byte streams or full-screen TUI state.
- Rendering SwiftUI presentation objects on the server.
- Synchronizing cache state between iOS devices.
- Adding Redis solely for transcript reads.
- Redesigning session cards, navigation, or transcript visual styling.

## Architecture

### Durable server state

`harness_events` remains append-only and authoritative. A session message is identified by `(session_id, seq)` and also retains its globally unique `event_id`. Sequence numbers are strictly increasing within one session.

The server must construct the wire message from the canonical event row instead of trusting historical `payload_json` to contain protocol metadata. Before returning or streaming an event, it overlays at least `event_id` and `seq` from the row onto a copy of the decoded payload. This makes migrated legacy rows compatible without rewriting their original payload bodies.

### Tail and page API

The existing endpoint remains compatible:

```text
GET /harness/sessions/{session_id}/messages?after_seq=N
```

It continues to return ascending messages newer than `N`, but accepts an optional positive `limit`. The first limited response captures the session-wide `max_seq`. If another page is required, the client repeats that value as `through_seq`; the server then filters `seq <= through_seq`. This fixed upper bound prevents messages arriving during catch-up from making pagination unbounded. After reaching `through_seq`, the client opens the WebSocket with `after_seq=through_seq`; the stream's own initial catch-up delivers anything committed between the final REST page and WebSocket subscription.

A new backward page mode serves cold loads and upward scrolling:

```text
GET /harness/sessions/{session_id}/messages?before_seq=N&limit=200
```

If `before_seq` is omitted, the server returns the newest page. The query selects rows in descending sequence order with a limit, then reverses the result for an ascending wire response.

The response shape becomes:

```json
{
  "messages": [],
  "page_min_seq": 1201,
  "page_max_seq": 1400,
  "max_seq": 1400,
  "has_older": true,
  "has_newer": false
}
```

`max_seq` retains its existing meaning: the session-wide maximum at the response snapshot. New clients use `page_min_seq` and `page_max_seq` for page boundaries. Invalid combinations such as both `after_seq` and `before_seq`, a `through_seq` below `after_seq`, non-positive limits, or limits above the server cap return `400`. The default and maximum limits are configuration constants, initially 200 and 500.

The server enables gzip for sufficiently large JSON responses when the request advertises it. Transcript responses remain private and authenticated. They may use a private ETag based on `(session_id, request cursor, limit, max_seq)`, but HTTP caching is only a bandwidth optimization; the application cache is responsible for instant rendering.

### iOS message cache

DroverKit adds a small cache abstraction with one implementation backed by a SQLite file in Application Support. Using SQLite directly keeps the dependency surface small and supports indexed range reads and transactional batch inserts.

Logical tables:

```text
cache_servers(server_key, canonical_base_url, schema_version)
cache_sessions(server_key, session_id, min_seq, max_seq, updated_at)
cache_messages(server_key, session_id, seq, event_id, wire_json)
```

The primary key is `(server_key, session_id, seq)` and `event_id` is uniquely indexed within the same server. `server_key` is a stable hash of the canonical base URL; bearer tokens are never persisted in the cache. If a server is rebuilt at the same URL, sequence regression detection invalidates affected session rows.

Only canonical wire JSON is stored. `AttributedString`, SwiftUI views, and other presentation values are not persisted. Schema changes use a cache schema version; an incompatible version deletes and rebuilds the cache rather than migrating presentation data.

The cache has a configurable size ceiling, initially 150 MB. Eviction removes least-recently-opened completed sessions first and never blocks initial rendering. The currently open session is not evicted.

### Open flow

When a chat screen starts:

1. Read the newest cached page off the main actor.
2. Publish that page to `ChatModel` in one batch and render it immediately.
3. Ask the server for its newest page or for messages after the cached maximum.
4. Transactionally upsert returned messages into the cache.
5. Publish each REST page to the model as one batch, preserving sequence order and deduplicating by sequence.
6. Continue forward catch-up through the first response's fixed `max_seq` until `has_newer` is false.
7. Open the WebSocket with `after_seq=max_seq` so its initial catch-up closes the REST-to-subscription race.

For a cold cache, step 3 requests the newest page. Older messages are fetched only when the user approaches the top. Prepending a page preserves the visible scroll anchor so content does not jump.

If cached `max_seq` exceeds the server's reported `max_seq`, the session cache is considered divergent and is replaced from a cold newest-page load. A missing or corrupt cache falls back to the same cold flow without surfacing an error to the user.

### Batched model ingestion

`MessageStream` distinguishes REST pages from live messages. A REST page is emitted as a batch. `ChatModel` performs one ordered deduplicating merge, one approval-state rebuild for the added range, and one transcript-version change per page.

Live WebSocket messages remain incremental, but transcript folding becomes stateful. The model retains the folded items plus the pending tool-step and run state needed to incorporate one new message without rescanning all prior messages. Full rebuilding remains available for tests, cache replacement, and defensive recovery.

### Presentation work and composer isolation

Wire decoding must not eagerly parse markdown and code blocks for every historical message. `HarnessMessage` retains raw text and protocol data. A bounded presentation cache, keyed by `event_id`, creates display blocks off the main actor when a row is about to become visible. The newest page may be prewarmed in one background task.

Composer draft text and attachments move into a dedicated observable composer model owned by the composer subtree. Keystrokes no longer mutate the transcript's observable model. Sending hands an immutable draft to `ChatModel`; success clears the composer model and failure preserves it.

## Legacy Sequence Migration

Bootstrap performs an idempotent migration before the message API begins serving traffic:

1. Find sessions containing null `seq` rows.
2. Reject automatic migration if a session mixes null sequences with existing sequences; log a structured diagnostic for manual repair because assigning around an existing sequence range could reorder history.
3. For an all-null legacy session, assign `row_number()` partitioned by `session_id`, ordered by `created_at, event_id`, starting at one.
4. Verify every migrated session has non-null, unique, contiguous sequences.
5. Commit the migration transaction.

The migration runs independently in the central and harness-daemon databases. It does not rewrite `payload_json`; the canonical wire overlay supplies `seq` and `event_id` at read time. Tests use tied timestamps to prove `event_id` provides deterministic ordering.

Before production deployment, the existing DuckDB files are backed up. A read-only audit reports affected sessions and counts before restart, and a post-restart audit confirms that the formerly blank session IDs return their expected message counts.

## Summarizer Retry Containment

Interactive transcript loading must not compete with a permanently malformed summarization job.

- Legacy `summarize_jobs` and pipeline jobs receive a finite attempt ceiling.
- Schema-validation failures and malformed model output retry with bounded exponential backoff and jitter.
- After the ceiling, the job moves to a terminal dead-letter state with the last error retained.
- New source material may create a new versioned job; it does not reset the attempts of the poisoned job in place.
- Metrics expose retry count, dead-letter count, oldest retry age, and job kind.

This is deployed separately from transcript pagination so its behavior and operational impact are independently verifiable.

## Error Handling

- REST decoding or transport failure keeps already rendered cache content visible and shows a reconnect indicator.
- Authentication failure remains terminal until configuration changes.
- A malformed individual message is reported and skipped without discarding the remainder of a page; its sequence is recorded as a gap so catch-up does not silently advance past it.
- A sequence gap stops forward catch-up before WebSocket attachment and retries from the last contiguous sequence.
- Cache write failure does not block rendering or live streaming.
- Pagination requests are single-flight per session and cancelled when the screen disappears.
- Server-side client disconnects are logged as concise access outcomes, not full socket tracebacks.

## Instrumentation

Add signposts and metrics around:

- cache lookup and decoded row count;
- REST time to first byte, bytes transferred, gzip ratio, and page count;
- JSON decode and presentation-prewarm duration;
- main-actor batch merge duration;
- time from navigation to first transcript frame;
- time until live WebSocket attachment;
- cache hit, miss, divergence, corruption, and eviction;
- sequence gaps and malformed messages;
- summarizer retry and dead-letter transitions.

No message text, bearer token, or attachment body is logged.

## Testing

### Server

- Migration backfills all-null legacy sessions deterministically.
- Mixed null/non-null sessions fail safely without mutation.
- Migration is idempotent across repeated bootstrap.
- Wire serialization overlays canonical `event_id` and `seq`.
- Forward and backward pagination have stable boundaries with no duplicates or gaps.
- Concurrent appends between pages are covered by the fixed `through_seq` catch-up contract.
- Gzip and ETag behavior preserve authenticated, private responses.
- Retry ceilings transition poisoned summarizer jobs to dead-letter exactly once.

### DroverKit

- Cached history renders before a delayed network response.
- Cold open requests only the newest page.
- Batch ingestion causes one transcript-version change per REST page.
- Cache plus REST plus WebSocket delivery remains strictly ordered and deduplicated.
- Divergent and corrupt caches rebuild safely.
- Prepending older pages preserves the visible scroll anchor.
- Composer typing does not invalidate transcript-derived state.
- Individual malformed events do not blank an otherwise valid page.

### Performance

Use sanitized fixtures shaped like the production histories: 3,316 events/4 MB and 2,024 events/10 MB. Measure cold open, warm open, page prepend, live append, typing, memory high-water mark, and cache eviction on a physical iPhone release build.

## Rollout

1. Add audits, legacy migration, canonical wire serialization, and retry containment. Back up both DuckDB files, restart services, and verify formerly blank sessions.
2. Add server pagination and gzip while preserving the old endpoint contract.
3. Add client batch ingestion and instrumentation. Deploy and compare cold-load signposts against the current build.
4. Add the iOS persistent cache and tail-first open flow behind a local feature flag. Validate cache rebuild and offline-visible behavior.
5. Add upward pagination, incremental folding, lazy presentation parsing, and composer isolation.
6. Remove the feature flag after production measurements meet the target thresholds for several large sessions.

Each stage is independently deployable and reversible. The durable database remains compatible throughout; cache rollback consists of deleting the rebuildable iOS cache and returning to the compatible full-history endpoint.

## Acceptance Criteria

- Every one of the 31 identified legacy sessions returns its stored transcript after migration.
- Existing and new sessions have unique contiguous per-session sequences.
- Opening a cached session renders without waiting for REST.
- Opening an uncached large session initially transfers no more than the configured newest-page limit.
- REST history is applied with one observable transcript mutation per page.
- WebSocket attachment begins only after contiguous REST catch-up.
- Typing does not mutate or re-evaluate transcript state.
- The 3,316-event and 10 MB fixtures meet the stated latency and stall targets on a physical device.
- Poisoned summarizer jobs stop retrying after the configured ceiling.
- Full server, registry, DroverKit, and device smoke suites pass.
