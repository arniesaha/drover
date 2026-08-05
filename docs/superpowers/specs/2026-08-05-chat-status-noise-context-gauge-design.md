# Chat status noise and the context gauge

**Date:** 2026-08-05
**Status:** approved, ready for planning
**Scope:** iOS only (`apps/drover`). No daemon, hub, or wire change — every
payload this needs already reaches the phone.

## Problem

Two defects, found together while reviewing a live chat transcript.

### 1. Half the transcript is status noise

Measured on a real claude-code session
(`harness-571701ec-001c-4b99-96e3-106d4c946e2c`, 741 messages):

| count | status text |
|---|---|
| 286 | `thinking_tokens` — 39% of *all* messages on its own |
| 18 | `task_started` |
| 17 | `task_notification` |
| 5 | `init` |
| 5 | `tool_progress` |
| 5 | `background_tasks_changed` |
| 4 | `turn complete` |
| 3 | `hook_started` |
| 3 | `hook_response` |
| 3 | `task_updated` |
| 2 | `rate_limit_event` |
| 1 | `vcs_state_changed` |
| **352** | **all status — 48% of the 741-message transcript** |

For contrast, the whole transcript is 5 `user_input`, 118 `assistant_output`,
133 `tool_action`, 133 `tool_result` — and 352 status.

Claude's driver maps every unrecognised top-level event kind to a `status`
message whose text is the raw kind name (`claude.py:134`), and `system`
subtypes the same way (`claude.py:94`). `MessageBubble` renders each as a
centered caption (`MessageBubble.swift:110`), so they stack up as a wall of
`hook_started` / `thinking_tokens` lines between the user's question and the
first real output.

The events are not junk. Each carries structured data the UI discards:

- `thinking_tokens` → `estimated_tokens` (running total), `estimated_tokens_delta`
- `tool_progress` → `elapsed_time_seconds`, `parent_tool_use_id`
- `task_started` / `task_notification` → `task_id`, `description`, `summary`, `status`
- `hook_started` / `hook_response` → `hook_id`, `hook_name`, `outcome`, `exit_code`

### 2. The context readout is off by ~58x

The app displayed `ctx 9.1M / 1M (914%)` against a 1M window. From the same
session's real `result` payload:

```
modelUsage["claude-opus-5[1m]"]:
  inputTokens=211  cacheReadInputTokens=9,024,921  cacheCreationInputTokens=120,147
  contextWindow=1,000,000                          sum = 9,145,279   <- displayed
```

`TokenUsageSummary.parseModelUsage` (`TokenUsageSummary.swift:132`) sums
`inputTokens + cacheReadInputTokens + cacheCreationInputTokens`. Every turn
re-reads the cached prefix, so that field is a **session-lifetime counter**:
10 turns × ~900K ≈ 9M. It is an odometer being read as a fuel gauge.

The obvious alternative, `result.usage`, is also wrong — it is cumulative too,
over the internal turns of a single request. All four result payloads:

| seq | num_turns | `result.usage` sum | `modelUsage` sum |
|---|---|---|---|
| 27 | 4 | 94,511 | 94,511 |
| 597 | 100 | **7,468,690** | 7,563,201 |
| 615 | 4 | 516,760 | 8,079,961 |
| 654 | 10 | 1,065,318 | 9,145,279 |

Neither aggregate is a gauge. The only true one is the **latest assistant
message's** `usage` — one API call's prompt:

```
seq 733: in=2  cacheRead=154,527  cacheCreation=2,446   -> 156,975
seq 741: in=2  cacheRead=156,973  cacheCreation=1,173   -> 158,148   <- correct
```

It also drops correctly on compaction (1,065,318 → 158,148), which is the
behaviour a gauge must have and a cumulative counter never can.

## Design

### Unit 1 — `Transcript.fold` (NexusKit, pure, unit-tested)

`TranscriptItem` gains one case and one associated value:

```swift
case statusRun([HarnessMessage])                          // new
case thinkingRun([HarnessMessage], estimatedTokens: Int?) // gains the count
```

- Consecutive `.status` messages collapse into one `.statusRun`. A
  non-status message breaks the run, so each fold sits where its events
  occurred — hooks that fired before the first tool call stay before it.
- `thinking_tokens` never enters a status run. It routes to the thinking run
  it belongs to. `estimated_tokens` is a running total, so the fold keeps the
  **max** across the run's span; if no run is open it attaches to the most
  recent one.
- `turn complete` is an ordinary foldable event — its only valuable payload
  (usage) moves to the header in Unit 3.
- `.statusRun`'s `id` is its first message's id, matching `thinkingRun`, so a
  run streaming in updates in place instead of being rebuilt.
- `latestRowID` must handle the new case or auto-scroll breaks on a
  transcript ending in a status run (`Transcript.swift:52` documents this
  trap for the existing cases).

### Unit 2 — `SessionEventsRow` (app, presentational)

- Collapsed: `⚙ 6 session events ›`. A single event shows its own name
  instead — `⚙ init ›` — because a count of one reads worse than the thing
  itself.
- Expanded: one line per event with its useful payload field (`hook_name` +
  `outcome`, `task_type` + `description`, `elapsed_time_seconds`), styled to
  match `ThinkingBlock` so folds read as one family.

### Unit 3 — context gauge

Math — only the numerator changes:

| Value | Source | Change |
|---|---|---|
| Window | `modelUsage[model].contextWindow` | keep |
| Context used | `modelUsage` sums | **replace** |
| → new source | latest assistant message `usage` | `input + cache_read + cache_creation` |

Rule: walk messages backwards, take the first carrying `payload.usage` that
is not a `result` payload. `parseModelUsage` stays, reduced to the window,
which is the one thing it reports reliably.

Placement: toolbar `.principal` (`ChatView.swift:212`) gains a subtitle —
`Claude` over `ctx 158K / 1M · 16%`. Per-message usage footers on assistant
bubbles are unchanged; only those riding on status captions disappear with
the fold.

Degradation:

- No window yet (before the first `result`) → `ctx 158K`, no denominator.
  Do not invent one.
- Gemini's `stats` shape carries no per-call usage → gauge hidden. Same for
  any harness lacking it; absent data means absent gauge, with no
  per-harness special-casing.
- Over 100% is shown, not clamped. 1.06M/1M was real and meant "about to
  compact" — the most useful thing the gauge can say.

## Testing

- **Regression fixture for the bug itself:** the captured payload where the
  current code yields 9,145,279 and the correct code yields 158,148. This
  failure cannot return silently.
- Fold: status runs break on a non-status message; `thinking_tokens` never
  renders as a row; `estimated_tokens` reaches `ThinkingBlock` as a max;
  `latestRowID` correct for a transcript ending in a status run.
- Gauge returns nil for the gemini `stats` shape and when no window is known.
- Single-event run renders its name, not a count.

## Out of scope

Deliberately deferred; both become cheap once the payloads are being read:

- Promoting `tool_progress.elapsed_time_seconds` onto tool cards that
  currently sit on "running…" indefinitely.
- Pairing `task_started` / `task_notification` by `task_id` into task rows.
