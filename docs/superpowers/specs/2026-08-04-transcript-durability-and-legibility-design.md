# Drover: transcript model, snapshot performance, and session legibility

Date: 2026-08-04
Status: approved design, not yet planned

Covers four sub-projects deferred from the 2026-08-04 audit
(`docs/handoffs/2026-08-04-audit.md`), scoped for the push to public
availability. The `daemon.py` decomposition is deliberately **out of scope** —
see "Deferred" at the end.

Every claim of fact below was verified against the live
`~/.drover/harnessd.duckdb` and `~/.drover/drover.duckdb`, or by running the
code. Measurements are recorded so a later reader can tell evidence from
assumption.

---

## SP1 — Transcript & durability model

### Problem

Drover stores session conversation in two places depending on mode:

- `mode='pty'` → `harness_transcript_chunks`, written by the terminal mirror
- `mode='structured'` → `harness_events` only, **zero** chunks

Live counts: 18 PTY sessions (16 with chunks), 38 structured sessions (0 with
chunks). Anything reading chunks directly therefore returns nothing for every
chat-mode session. That is how `_build_handoff_prompt` shipped "Recent
transcript: not available in central Drover yet." to 100% of structured
handoffs (fixed in `093b349` by adding `transcript_text()`; this spec removes
the underlying asymmetry).

Separately, both write paths end in a bare swallow — `_TerminalMirror._flush`
uses `except Exception: pass`, and `structured/manager.py::emit` prints to
stderr and continues. A DuckDB write conflict under contention loses the event
**permanently and invisibly**, with no retry and no record that anything was
lost.

### Evidence that the two tables are redundant

For the session with the most chunks
(`harness-49db21a6-c562-43e9-8e1e-5b71b993185e`):

| source | rows | chars |
|---|---|---|
| `harness_transcript_chunks` | 159 | 1,910 |
| `terminal.output` events (`payload.text`) | 159 | 1,910 |

Byte-identical content, 1:1. Events are strictly richer: they also carry
`content_preview`, the ANSI-cleaned form. The chunk column is named
`content_redacted`, but no redaction actually happens — the stored sample is
raw ANSI (`'\x1b[?1034hsh-3.2$ '`), identical to the event text.

### Design

`harness_events` becomes the single source of truth for transcripts.

**Removed:**

- `harness_transcript_chunks` DDL and its entry in `HARNESS_TABLES`
  (`harness/schema.py`)
- `HarnessRegistry.append_transcript_chunk` / `get_transcript_chunk` /
  `list_transcript_chunks` (`harness/registry.py`)
- `HarnessTranscriptChunk` model (`harness/models.py`)
- `daemon._safe_append_transcript_chunk` — already dead, never called
- the `"chunk"` kind in the terminal mirror's queue and its `_flush` branch

**Changed:**

- `HarnessRegistry.transcript_text()` collapses to its events-only branch; the
  chunk-preference path goes away.
- `metrics.harness_session_snapshot` drops the `transcript_chunks` field from
  its response. `events` is already in the same payload.
- `web/static/harness_terminal.html:780` currently builds terminal scrollback
  from `transcript_chunks`. It moves to `terminal.output` events — the same
  file already handles those at line 498.

**Migration:** `DROP TABLE IF EXISTS harness_transcript_chunks` in
`bootstrap_harness_tables`. No data-preservation step: the content is verified
duplicate, not assumed to be. Existing rows are discarded with the table.

**Ordering constraint:** the two consumers (the HTML scrollback and the wire
field) must move *before* the table is dropped, or the web terminal loses its
scrollback.

### Durability

`_flush` and `manager.emit` get identical treatment:

1. Retry the write up to 3 times with short backoff. DuckDB
   `TransactionException` under concurrent writers is transient; a retry
   usually succeeds.
2. On final failure, increment a process-global `dropped_events` counter
   (a single integer for the daemon, not per-session — the point is an
   operator signal, and a per-session map would grow unbounded), and write a
   best-effort gap marker: `event_type="transcript.gap"`, normalizing to
   `normalized_type='status'`, with the lost-event count in the payload. If
   the marker write itself fails, the counter still records the loss.
3. Surface `dropped_events` through `doctor`.

The principle: a transcript that says "3 events lost here" is honest. One that
silently omits them lies, and every downstream consumer — handoff prompts
above all — inherits the lie.

Dropping the duplicate PTY chunk write halves that path's write volume, which
reduces the contention producing these failures in the first place.

### Testing

- `transcript_text()` returns the event-replayed transcript for both modes;
  existing chunk-preference test is removed with the feature.
- Bootstrap on a database that already has `harness_transcript_chunks` drops
  it and leaves the other harness tables intact.
- A forced registry write failure increments `dropped_events` and produces a
  gap marker rather than silently returning.
- A write that fails once then succeeds is retried and recorded — no counter
  increment, no marker.

---

## SP2 — Fleet snapshot performance

### Problem

`MetricsRenderer.harness_snapshot()` copies the entire DuckDB file to a temp
directory on **every request**, uncached:

```python
with tempfile.TemporaryDirectory(prefix="drover-harness-") as tmp:
    snapshot = Path(tmp) / source.name
    shutil.copy2(source, snapshot)
```

The hub's database (`~/.drover/drover.duckdb`) is **483 MB**. Measured copy
time: **0.78 s**. The iOS app polls every 5 s (`SessionStore.startPolling`,
default `seconds: 5`). That is a **16% disk duty cycle per connected client**,
scaling linearly with clients and growing without bound as the context store
grows.

`render_json` already has a TTL cache (`_cached_json` / `_cached_until`).
`render_harness_json` has none.

### Design

**Drop the copy.** Query the live database via
`HarnessRegistry(self.duckdb_path)` — the pattern already proven elsewhere in
this same file (`_build_handoff_prompt`,
`_sync_terminated_harness_session`). `harness_snapshot` needs two indexed
reads (`list_hosts`, `list_sessions`); they run under the existing
process-wide connect lock in microseconds.

The copy was a defensive point-in-time read, not a correctness requirement.
`open_duckdb_connection`'s own docstring records that read-only opens were
abandoned precisely because diagnostics run inside the server process beside
live writers — the live-read path is the supported one.

**Add a TTL cache** (2 s default, configurable) around the rendered harness
JSON, mirroring the existing `_cached_json` mechanism, so N connected clients
produce one render per interval rather than N.

Same change applies to `harness_session_snapshot`, and to `_quality_snapshot`
and `_observatory_snapshot` — identical pattern, lower request frequency, so
lower priority but the same defect.

### Testing

- `harness_snapshot` creates no temporary file and no copy (assert via a
  `shutil.copy2` spy that it is never called).
- Two calls inside the TTL window perform one underlying registry read; a call
  after expiry performs a second.
- Snapshot content is unchanged from the pre-change payload for a fixture
  database — this is a performance change, not a behavioral one.

### Expected result

780 ms → sub-millisecond per snapshot; per-client disk duty cycle from ~16% to
approximately zero.

---

## SP3 — Session legibility

### Problem

Two distinct complaints, both visible in the reported screenshots.

**Rows are indistinguishable.** `SessionRow` titles each row with the cwd's
last path component, so three sessions in this repo all render as "drover".
`SessionSummary` carries no branch, no preview, no title.

Note a correction to the audit: **`branch` is already on the wire.** The
snapshot serializes `session.__dict__`, and `HarnessSession` has a `branch`
field. Only the client fails to decode it. A preview field is genuinely new.

**Raw CLI stderr renders as a chat message.** The full
`error: unexpected argument '--sandbox' found / tip: ... / Usage: codex exec
resume ... / For more information, try '--help'` block appears verbatim as a
message bubble. Related smaller defects in the same view: `turn exited`
appears twice with no explanation, and the token line
(`in 10.4M | out 26.1K | cache 10.1M | reason 5K`) is unlabelled jargon.

### Design — rows

**Server.** Add `last_preview` to each session object in the harness snapshot:
the newest `harness_events.content_preview` for that session where
`event_type` is one of `assistant_output`, `user_input`, `tool_action`.

This must be **one grouped query** across all sessions, not a query per
session. A per-session read would reintroduce exactly the per-request cost SP2
removes.

**Client.** `SessionSummary` decodes `branch` and `last_preview`. `SessionRow`
renders `repo · branch` on the title line and `last_preview` as a truncated
third line.

Chosen over branch-only because it does not solve the reported case: all three
sessions are on `main` and would still read identically. The preview is what
actually disambiguates; branch is the cheap context that matters once feature
branches and per-session worktrees are in play, as they already are here.

### Design — errors

Drivers keep the full stderr in the payload and add a one-line `summary` — the
first meaningful line, e.g. `error: unexpected argument '--sandbox' found`.
`MessageBubble.errorCard` renders the summary with a disclosure control for
the full text.

Also in scope: suppress the duplicated bare `turn exited` status line, and
label the token-usage row.

### Testing

- The snapshot includes `last_preview` for a session with events and omits or
  nulls it for one without.
- The preview query issues a fixed number of statements regardless of session
  count (guards the N+1).
- `SessionSummary` decodes `branch`/`last_preview`, tolerating absent keys —
  older hosts will not send them.
- Two sessions in the same repo on the same branch produce different row
  content.
- An error message with multi-line stderr yields a one-line summary and
  retains the full text.

---

## SP5 — Relay E2E determinism

### Problem

`tests/test_relay_e2e.py::test_full_session_lifecycle_over_relay` failed in CI
on `4605ed0` with "hub never mirrored a terminal.output event", and passed
locally (1,058 passed) and on the next CI run. The assertion polls the hub
registry for 10 s for a mirrored event.

This is not purely a slow-runner problem. Because `_flush` swallows write
failures with no retry, an event lost to lock contention **never arrives at
all** — so the test cannot distinguish "not yet" from "never". Under CI load,
contention is likelier.

### Design

SP1 is the substantive fix: retries make the write likely to succeed, and the
`dropped_events` counter makes the failure observable.

The test then changes to:

- raise the ceiling from 10 s to 30 s while continuing to poll on the
  condition, so the common case stays fast and a loaded CI runner is not
  mistaken for a failure
- on failure, report the `dropped_events` counter and the last 20 recorded
  events for the session, so the next flake is diagnosable instead of a
  mystery

### Testing

The test is the test. Success criterion: it distinguishes a slow write from a
dropped one in its failure output.

---

## Sequencing

```
SP1 (foundational) ──> SP3 (preview quality depends on event integrity)
                   └─> SP5 (assertion depends on the counter)
SP2 (independent, can run in parallel)
```

SP1 first. SP2 may proceed alongside it — they touch different functions.
SP3 and SP5 follow SP1.

Each sub-project is independently shippable and gets its own phase in the
implementation plan, with tests green before the next begins. Nothing here
requires a big-bang merge.

---

## Deferred

**`daemon.py` decomposition.** The file is 2,750 lines:
`HarnessRequestHandler` alone is 1,460, with roughly 750 lines of
native-transcript parsing helpers at module level and a 138-line
`_TerminalMirror`. Natural seams exist (native transcript discovery/parsing →
own module; terminal mirror → own module; server bootstrap → own module; the
handler split by route family).

Deliberately excluded from this pre-launch scope: it is the largest single
chunk of work, carries the most regression risk, and its benefit accrues to
contributors rather than users. **Accepted cost:** SP1 and SP3 both add code
to a file that is already too large, making the eventual split slightly
bigger.

**Other audit items** not covered here and still open: legacy `nexus` naming
across 128 tracked files, hardcoded personal paths in
`docs/install-shipper.md` and Swift test fixtures, no iOS job in CI, no
linter/type checker, and a three-line README quickstart.

---

## Risks

**Dropping a table is the one irreversible step in this spec.** The evidence
that chunks are redundant is direct (byte-identical 1:1 comparison), not
inferential. If a lower-risk sequence is preferred, the writers and consumers
can be removed in one release and the `DROP TABLE` deferred to the next; the
only cost is leaving dead code in place for a cycle.

**The `last_preview` query is the N+1 risk.** Implemented per-session it would
reintroduce the cost SP2 removes. This is called out in SP3's tests
deliberately.

**Preview content is user-visible session text.** `content_preview` is already
ANSI-cleaned and length-bounded (2,000 chars) by
`normalize_harness_event`, and the row truncates further. No new redaction
requirement is introduced, but the field now appears in a list view rather
than only a detail view.
