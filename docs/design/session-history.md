# Session History

Status: accepted, 2026-10-02. Work item: "session history" (backend, web, iOS).

## Problem

Clients only see the newest ~20 finished sessions per host. harnessd's
`GET /sessions` (#451) returns every live session plus the newest 20 finished
ones, with an `archived_cursor` for older ones, but neither the hub nor any
client pages through it. History is also scoped to one host, while the user
thinks in terms of "my sessions" across all hosts.

PostgreSQL, the control plane, already holds every session (279 on the live
hub) with its events, previews, summaries (`session_memory`, #489), token
totals (`session_usage`) and harness↔native identity (#485). History is
therefore a read over PostgreSQL. It never fans out to hosts and never touches
DuckDB.

## Decisions

- **One hub endpoint, PostgreSQL only.** `GET /sessions/history` pages the
  control plane. A hub running the DuckDB control store answers `501`, not
  `503`. The condition is permanent for that configuration, and a `503` would
  make the iOS client back off the whole hub.
- **Keyset, never offset.** The order is `(activity_at DESC, session_id DESC)`,
  where `activity_at = COALESCE(last_activity, updated_at)`. `updated_at` is
  `NOT NULL`, so every row has a sort key. The cursor is the last row's key,
  and the next page reads strictly below it.
- **Fixed, small rows.** No transcripts and no events in the list. Opening a
  row uses the existing `GET /harness/sessions/{id}/messages`, paged by `seq`.
- **Full-text search over a trigger-maintained document.** The title (latest
  user prompt), repo/branch/harness and summary live in three tables. One
  `tsvector` per session in `session_search`, kept current by triggers in the
  writer's transaction, makes them searchable through one GIN index.
- **Separate from the live list.** `/harness` and `/harness/sessions` are
  unchanged. History is its own view and its own admission lane.

## Endpoint Contract

`GET /sessions/history`

| Parameter | Form | Meaning |
|---|---|---|
| `host` | repeated or comma-separated, ≤20 | `host_id` in the set (retired hosts included) |
| `harness` | repeated or comma-separated, ≤20 | harness in the set |
| `repo` | `owner/name`, repeated, ≤20 | exact repository |
| `state` | `running` / `awaiting` / `finished` / `failed`, repeated | derived state, see below |
| `since`, `until` | ISO 8601 date or timestamp (naive = UTC) | `since ≤ activity_at < until` |
| `q` | ≤200 chars | full-text search over title, summary and repo |
| `limit` | 1..50, default 30 | page size; values above 50 are clamped |
| `cursor` | opaque | `next_cursor` from the previous page |

Unknown parameters are rejected with `400`, which is how `offset` is refused.
A cursor carries a fingerprint of the filters it was issued for. Replaying it
under different filters is a `400` ("start again without it"), never a silently
wrong page.

States: `finished` = `completed`/`terminated`; `failed` = `errored`/`failed`;
`awaiting` = live and (`status = 'awaiting'` or `awaiting IS NOT NULL`);
`running` = every other live status. Unrecognised statuses count as live,
matching `ARCHIVED_SESSION_STATUSES`.

Search: `q` is split into word runs (at most 8). Every term must match, and
each term matches as a prefix (`websock` finds `websocket`), so the search box
narrows as the user types. The query is assembled only from word characters,
so no `tsquery` operator can be injected. A `q` with no word characters
applies no filter.

Response (`200`, `Cache-Control: no-store`):

```json
{
  "items": [
    {
      "id": "h-7f3a…",
      "title": "Refactor the pager window",
      "harness": "claude",
      "model": "claude-opus-5-5",
      "host": {"id": "mac-mini", "name": "Studio Mac", "retired": false},
      "repo": "arniesaha/drover",
      "branch": "feat/session-history",
      "state": "finished",
      "status": "completed",
      "started_at": "2026-10-01T09:12:03.120000+00:00",
      "ended_at": "2026-10-01T10:02:44.000000+00:00",
      "last_activity": "2026-10-01T10:02:44.000000+00:00",
      "summary": "Rewrote the history pager to keep a 300-row window…",
      "has_transcript": true,
      "tokens": {"input": 120400, "output": 9100, "cache_read": 880000, "cache_write": 4100}
    }
  ],
  "next_cursor": "eyJ2IjoxLCJ0Ijoi…",
  "has_more": true,
  "page_size": 30,
  "truncated": false,
  "as_of": "2026-10-02T09:40:00.000000+00:00"
}
```

- `title` is the first line of the latest user prompt (≤120 chars, with auth
  material redacted). Fallbacks, in order: `owner/name · branch`, the cwd's
  basename, then `"<harness> session"`.
- `summary` is a plain-text snippet of at most 300 chars. It comes from the
  final summary, or the live recap while a session runs. The harness-keyed
  memory row wins over a native-keyed one.
- `host.retired` is true for a retired host, and also for a `host_id` with no
  host row left. Clients label both as retired.
- `tokens` is `null` when `session_usage` has no row.
- `truncated` is true when the byte cap shed trailing rows. `next_cursor` then
  points at the last row actually sent, so nothing is skipped.

Errors: `400 {"error": …}` for invalid parameters; `401` without
credentials; `501` on a DuckDB control store; `503` with `Retry-After: 1`
when the history admission lane (`DROVER_HISTORY_HTTP_CONCURRENCY`, default 4)
or the PostgreSQL pool is saturated.

`GET /sessions/history/facets` returns bounded filter-chip values: hosts (≤100,
retired ones labelled), harnesses (≤20), the 50 most recently active repos,
and the four states.

### Opening a row

Clients open a row with the existing transcript endpoint, newest page first,
then older pages by sequence:

```
GET /harness/sessions/{id}/messages?before_seq=<max+1>&limit=200   # newest
GET /harness/sessions/{id}/messages?before_seq=<page_min_seq>&limit=200
```

History clients never request more than 200 events per page. The endpoint's
own maximum of 500 is unchanged for existing callers.

## Limits

| Limit | Value | Enforced by |
|---|---|---|
| Page size, default | 30 rows | `session_history.DEFAULT_PAGE_SIZE` |
| Page size, hard maximum | 50 rows (clamped) | `MAX_PAGE_SIZE` |
| Rows read per request | `limit + 1` (≤51) | SQL `LIMIT`, so memory per request is constant |
| Response bytes | ≤ 64 KiB, newline included | `response_caps.fit_page` sheds trailing rows |
| Title | ≤ 120 chars | `TITLE_CHARS` |
| Summary snippet | ≤ 300 chars | `SUMMARY_CHARS` |
| Filter values per parameter | ≤ 20 | `MAX_FILTER_VALUES` |
| Search query | ≤ 200 chars, ≤ 8 terms | `MAX_QUERY_CHARS`, `MAX_QUERY_TERMS` |
| Concurrent history requests | 4, then `503` + `Retry-After` | `history_slots` |
| Pool wait | 2 s, then `503` + `Retry-After` | `control_plane_connection(timeout=2.0)` |
| Web DOM rows | viewport + 8 overscan each side (~40 at 800 px) | `history_view.js` virtual window |
| Web rows in memory | every fetched row (fixed shape); snapshot ≤ 1,500 | `SNAPSHOT_MAX_ITEMS` |
| Web transcript drawer | ≤ 1,000 messages, 200 per page | `TRANSCRIPT_MAX_MESSAGES` |
| iOS rows in memory | ≤ 300 full rows (10 pages) | `HistoryModel` window |
| Transcript page size | ≤ 200 events | clients pass `limit=200` |
| Latency target | p95 < 150 ms per page at 10K sessions | `tests/test_session_history_perf.py` |

### Measured

On a synthetic 10K-session hub (5 hosts, one retired; previews, summaries on
75%, usage, events on 50%), measured by `tests/test_session_history_perf.py` on
a disposable local PostgreSQL 17 cluster (Apple silicon, 2026-10-02). Each
figure is end to end through `fetch_history_page`: SQL, rendering and the cap.

| Scenario | p95 |
|---|---|
| First page (30) | 4.6 ms |
| First page (50) | 7.4 ms |
| Host filter / retired host | 4.7 ms / 4.8 ms |
| Harness / repo / state filters | 4.8 ms / 5.8 ms / 5.3 ms |
| Date range | 4.8 ms |
| `q` common term / prefix / rare term | 6.8 ms / 7.0 ms / 0.9 ms |
| `q` + host + state | 5.5 ms |
| 50 consecutive deep keyset pages | 4.8 ms (flat with depth) |
| Facets | 3.0 ms |
| **All 164 requests** | **p50 4.6 ms, p95 6.8 ms, max 7.7 ms** |

Seeding 10K sessions, with every trigger firing, took 1.2 s.

## Storage (migration 9)

- `harness_sessions_history`: `((COALESCE(last_activity, updated_at)), session_id)`.
  The planner scans it backward for the default order, and the row comparison
  `(activity_at, session_id) < (?, ?)` is an index condition.
- `harness_sessions_history_host`: the same, prefixed by `host_id`, for the
  most common filter.
- `harness_sessions_native`: partial index on `native_session_id`, used to
  resolve memory rows keyed by a native id.
- `session_search (session_id PK, document tsvector)` with a GIN index. The
  weights are: A = latest user prompt (first 1,000 chars), B = owner, name,
  name split on `-`, branch, harness, C = summary or recap (first 8,000 chars).
  The text-search configuration is `simple`, so identifiers are not stemmed.
- Triggers refresh one session's document when:
  - `harness_sessions` inserts, or updates repo, branch, harness or native id.
    `last_activity` is excluded because it moves on every event.
  - a row in `harness_session_previews` changes.
  - a row in `session_memory` changes, whether keyed by the harness id or the
    native id.
  - a session is deleted, which removes its document.
  The refresh function fails soft: it emits a `WARNING`, and the session or
  event write still commits. A stale document can be rebuilt, but a lost
  write cannot.
- The migration backfills documents for every existing session. Expected
  versions are derived from `_MIGRATIONS` in the tests, not hard-coded.

## Consistency While Paging

New sessions arrive above the cursor, so a later page can never repeat or skip
a row because of them. This is tested by inserting sessions between page
fetches. Sorting by activity has one inherent edge case. A session that is not
yet paged and gets new activity moves above the cursor, so it is missed in this
pass and appears at the top on refresh. A session already shown that moves up
is not shown twice. Clients also de-duplicate by `id`. Finished sessions, which
are nearly all of history, do not move.

## Web

`/ui/history` is a vanilla page, like the rest of the console:

- Infinite scroll by cursor, with an `IntersectionObserver` sentinel near the
  end.
- Filter chips for host, harness, repo, state and a date range, filled from
  `/sessions/history/facets`. A search box debounced at 250 ms.
- Rows grouped under day headers, in the viewer's local time zone.
- Fixed-height row virtualization. Only the visible rows plus overscan are in
  the DOM.
- Empty, error (honouring `Retry-After`) and loading states.
- A row opens a transcript drawer over the list.
  - The drawer pages `/harness/sessions/{id}/messages` with `limit=200`, newest
    first, with "Load earlier" by `before_seq`. It shows at most 1,000
    messages; past that, it links to the live console.
  - The list stays mounted underneath. Close, Escape or Back returns to the
    exact scroll position with nothing refetched.
  - `?session=` deep-links the drawer.
  - The existing console page (`/ui/harness/sessions/{id}`) loads every event
    and proxies to the host, so History does not use it for reading.
    "Open in console" (or a modified click) still goes there.
- Filters live in the URL query. On Back from the console, or on reload, the
  loaded rows, cursor and scroll offset are restored from `sessionStorage`
  without refetching. A list over 1,500 rows keeps only its offset and
  re-pages to it.

The pure logic is `history_view.js`: query building, day grouping, the
virtual window, debounce, merge/dedupe, snapshots and transcript paging. Node
tests run it under pytest (`tests/test_web_history.py`). The same file
boots the page's real script against a stub DOM with canned hub responses,
covering paging, debounce, empty/busy/unavailable states, the drawer and Back
restoration.

## iOS

The History screen opens from Home.

- DroverKit provides:
  - `DroverClient.historyPage(filter:cursor:limit:)` and `historyFacets()`.
    History has its own `Retry-After` lane, so a busy history read never
    stalls the fleet poll.
  - The `HistoryItem`/`HistoryPage`/`HistoryFacets` models.
  - `HistoryPager`, a pure page/cursor state machine.
- `HistoryModel` (app, `@MainActor @Observable`) keeps at most 300 full rows,
  which is 10 pages of 30.
  - A lazy `List` asks for the next page when a row within 5 of the end
    appears.
  - When the window exceeds 300 rows, pages furthest from the visible row
    are evicted down to a skeleton (id and day only), so list geometry and day
    sections stay stable.
  - When a skeleton row appears, its page is refetched from its saved cursor.
- Filters are a sheet. Search uses `.searchable`, debounced at 300 ms. Rows
  are grouped into day sections. Pull to refresh restarts from the top.
- Opening a row shows a read-only transcript paged with
  `messagePage(.older(beforeSeq:limit: 200))`.

## Later

- **Semantic search.** `history_where` has a marked hook. A `mode=semantic`
  would add a candidate set from `session_embeddings` (pgvector) under a
  similarity threshold, while keeping the keyset order. Ranking by similarity
  would need its own cursor.
- **Response caps.** `response_caps.py` is a local stand-in for
  `drover.server.mcp.contract` on the unmerged `mcp/caps-and-freshness` branch.
  It counts bytes the same way, but sheds whole rows so cursors stay valid.
  Fold the two together when that branch lands.
