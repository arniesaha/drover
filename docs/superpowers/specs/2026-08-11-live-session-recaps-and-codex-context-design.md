# Live Session Recaps and Codex Context Design

## Goal

Replace structured-chat previews that repeat the user's initial request with a live, one-sentence recap of the session's goal and current progress. Use the same recap as the primary title inside the chat, while preserving harness identity and an accurate context-usage indicator in compact metadata.

Terminal sessions are outside this change and retain their current last-output preview behavior.

## User Experience

### Fleet inbox

For a structured chat, the card's primary text is a single recap sentence such as:

> Improving chat previews and headers; tracing the live summary pipeline.

The recap may occupy up to two lines. The existing project kicker, timestamp, harness, host, state, and action remain in their current positions.

Before the first generated recap is available, the card uses the cleaned initial user request. While a newer recap is being generated, the previous successful recap remains visible. A generation failure never replaces a good recap with an error or empty state.

### Chat header

The selected layout is “recap plus compact metadata”:

- The live recap is the primary inline navigation title and is limited to one line there.
- A quiet second line contains the harness identity and context gauge.
- When context usage is unavailable, the harness identity remains visible by itself.
- The title truncates to the available navigation width and does not increase the navigation bar beyond the compact two-line layout.

The inbox card and chat header must consume the same server-provided recap so they cannot disagree about the session.

## Live Recap Architecture

### Trigger and coalescing

A structured `status` event whose payload contains `turn_complete: true` marks that session's recap source as stale and enqueues recap generation. Jobs are keyed by session and source sequence. If more turns complete before a pending job runs, they coalesce into the newest source sequence rather than producing obsolete intermediate recaps.

Only structured conversation sessions enqueue live recaps. PTY and terminal events do not.

### Input and output

The recap prompt receives the latest 30 content-bearing conversation events in chronological order. It treats transcript content as untrusted data and requests exactly one plain-text sentence that states:

1. the user's overall goal; and
2. the session's current progress toward that goal.

The worker rejects empty output, strips surrounding whitespace and accidental formatting, and limits stored output to 160 Unicode characters at a word boundary. It does not expose model prose, errors, or job state directly to clients.

### Storage

Use a `live_session_recaps` table for one current recap record per session with:

- `session_id`;
- recap text;
- source sequence;
- generation timestamp; and
- generator model.

The source sequence makes stale worker results detectable. A worker may publish its result only when it still corresponds to the newest requested source sequence. The last successful text remains readable while a replacement is pending or fails.

Use a durable `live_recap_jobs` row keyed by session for desired source sequence, status, attempts, retry timing, and last error. When Redis job streams are enabled, they deliver work using the repository's existing durable-DB-plus-stream pattern; DuckDB remains the source of truth and the worker can recover pending work without Redis.

This is a dedicated live-recap projection. It does not repurpose closed-session `session_summaries` or the larger, lazy `active_session_briefs`, whose lifecycle and handoff-oriented schema are different.

### Serving and refresh

The fleet snapshot adds an optional `recap` field to each structured session. Its existing `preview` remains available as the initial-request fallback and for backward compatibility.

Fleet polling obtains the stored recap and source sequence from the snapshot. When an open chat receives `turn_complete`, it starts a bounded one-second metadata poll and stops when the snapshot's recap source sequence reaches that turn's sequence or after 30 seconds. It keeps the previous recap throughout the poll. This avoids inserting server-generated records into the host-owned message sequence while still updating the title without requiring navigation. Older clients ignore the additive snapshot fields.

## Codex Context Usage

### Confirmed failure

Codex emits usage in `turn.completed` status events, but the current iOS `ContextGauge` examines assistant-output usage. Codex also lacks Claude's `result.modelUsage.contextWindow`, so the gauge returns `nil` and disappears.

The observed Codex completion payloads contain cumulative counters. `input_tokens` already includes cached input; `cached_input_tokens` is a subset and must not be added to it.

### Context-window source

The Codex structured driver resolves the selected model in Codex's local model catalog and adds the effective context window to each completion payload. The effective window is the catalog's configured context window multiplied by its effective percentage, matching the limit Codex itself uses.

Drover must not hard-code the public API maximum. For example, the [official GPT-5.6 Sol model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-sol) advertises a 1,050,000-token maximum, while the inspected Codex runtime configures a 272,000-token window at 95%, or 258,400 effective tokens.

If model metadata cannot be resolved, the event still carries usage and the UI may show an absolute used-token count without a denominator or percentage.

### Current-turn calculation

For Codex, the UI scans completion events newest-first:

1. Read the newest completion's cumulative `input_tokens`.
2. Subtract the preceding completion's cumulative `input_tokens`.
3. If there is no preceding completion, use the newest value.
4. If the counter decreased or reset, use the newest value rather than a negative delta.
5. Do not add `cached_input_tokens`.

This delta represents the most recent request's prompt pressure and falls after compaction. The effective context-window value from the newest completion supplies the denominator.

Claude and Antigravity retain their existing provider-specific calculations.

The compact header text is:

> Codex · ctx 93.6K / 258.4K · 36%

When only the used count is known:

> Codex · ctx 93.6K

## Failure Handling

- A recap backend outage leaves the previous recap visible and the job retryable.
- An out-of-order recap result cannot overwrite a newer recap request.
- Empty or malformed recap output is treated as a failed generation.
- Missing Codex model metadata removes only the denominator and percentage.
- Missing or malformed Codex usage removes only the context gauge; the recap and harness identity remain visible.
- Snapshot decoding remains backward compatible when `recap` is absent.

## Testing

### Server

- A structured `turn_complete` status event enqueues a recap for its source sequence.
- Repeated completions coalesce to the newest sequence.
- Stale worker output cannot replace a newer requested recap.
- Failed or malformed generation retains the last successful recap.
- Fleet snapshots expose recap text while preserving the existing preview fallback.
- A successful generation advances the snapshot's recap and source sequence atomically.
- Terminal sessions do not enqueue live recaps.
- The Codex driver resolves effective context windows from model metadata and degrades cleanly when metadata is absent.

### DroverKit

- Session models decode snapshots with and without `recap`.
- Structured cards prefer `recap`, then fall back to the existing preview.
- Chat state updates its title when a newer recap arrives.
- Chat metadata polling stops when the recap reaches the completed turn, after 30 seconds, or when the view stops.
- Codex context usage is the delta between consecutive cumulative input totals.
- Codex cached input is not double-counted.
- First-sample and reset cases use the latest cumulative input value.
- Missing Codex window metadata produces an absolute count without a percentage.
- Existing Claude and Antigravity context behavior remains unchanged.

### App UI

- The inbox recap renders within two lines at supported Dynamic Type sizes.
- The selected two-line header keeps recap, harness identity, and context readable on narrow iPhones.
- Missing recap and missing context each degrade independently without leaving an empty header.

## Out of Scope

- Changing terminal-session previews.
- Replacing closed-session summaries or active handoff briefs.
- Displaying recap generation errors in the user interface.
- Adding chat image-rendering support; that will be tracked separately.
