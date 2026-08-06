# Session History Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore all legacy transcripts and stop poisoned summarization jobs from consuming unbounded production resources.

**Architecture:** DuckDB bootstrap deterministically sequences all-null legacy sessions, while wire serialization overlays canonical row metadata onto historical payloads. Summarization jobs gain source-versioned retry budgets so identical work dead-letters after five attempts and only genuinely newer source material opens another retry generation.

**Tech Stack:** Python 3.11+, DuckDB, pytest, Click, existing Drover job ledger and Redis Streams adapters.

## Global Constraints

- DuckDB remains the durable command-plane source of truth.
- Sequence values are unique and strictly increasing within one session.
- Mixed null/non-null legacy sessions are never migrated automatically.
- Original `payload_json` bodies are not rewritten.
- Retry ceiling is exactly 5 attempts per source version.
- No message text, bearer token, or attachment body is logged.
- Back up both production DuckDB files before running the migration.

---

## File Structure

- `src/drover/server/harness/schema.py`: schema bootstrap and idempotent legacy sequence migration.
- `src/drover/server/harness/models.py`: canonical `HarnessEvent.wire_payload()` representation.
- `src/drover/server/web/app.py`: use canonical wire payload for REST and WebSocket delivery; suppress expected client-disconnect tracebacks.
- `src/drover/server/summarizer/jobs.py`: focused source-version and retry-state transitions for `summarize_jobs`.
- `src/drover/server/summarizer/worker.py`: enforce retry budgets at claim and failure boundaries.
- `src/drover/server/watcher.py`: enqueue one summarize generation per changed source version.
- `src/drover/server/metrics.py`: retry/dead-letter metrics.
- `src/drover/server/__main__.py`: read-only harness audit command and explicit migration command.
- `tests/test_harness_registry.py`, `tests/test_metrics.py`: migration and wire behavior.
- `tests/test_summarizer_jobs.py`, `tests/test_summarizer_worker.py`, `tests/test_watcher.py`: bounded retry generations.
- `tests/test_server_cli.py`: audit/migration CLI behavior.

### Task 1: Deterministic legacy sequence migration

**Files:**
- Modify: `src/drover/server/harness/schema.py`
- Test: `tests/test_harness_registry.py`

**Interfaces:**
- Produces: `migrate_legacy_harness_event_sequences(con: duckdb.DuckDBPyConnection) -> LegacySequenceMigrationReport`
- Produces: `LegacySequenceMigrationReport(migrated_sessions: int, migrated_events: int, mixed_sessions: tuple[str, ...])`
- Consumed by: `bootstrap_harness_tables` and Task 4's CLI.

- [ ] **Step 1: Write failing migration tests**

Add tests that create `harness_events` rows directly after bootstrap, including tied timestamps:

```python
def test_bootstrap_sequences_all_null_legacy_events_deterministically(tmp_path):
    registry, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute("UPDATE harness_events SET seq=NULL")
        con.executemany(
            "INSERT INTO harness_events(event_id,session_id,event_type,payload_json,created_at,seq) VALUES (?,?,?,?,?,NULL)",
            [
                ("event-b", "legacy", "assistant_output", '{}', "2026-06-01 10:00:00"),
                ("event-a", "legacy", "user_input", '{}', "2026-06-01 10:00:00"),
            ],
        )
        report = migrate_legacy_harness_event_sequences(con)
        rows = con.execute(
            "SELECT event_id,seq FROM harness_events WHERE session_id='legacy' ORDER BY seq"
        ).fetchall()
    assert rows == [("event-a", 1), ("event-b", 2)]
    assert report.migrated_sessions == 1
    assert report.migrated_events == 2

def test_migration_refuses_mixed_sequence_session_without_mutation(tmp_path):
    # Insert seq=1 and seq=NULL in the same session.
    # Assert report.mixed_sessions == ("mixed",) and the null row remains null.

def test_migration_is_idempotent(tmp_path):
    # Run twice and assert the second report migrates zero rows.
```

- [ ] **Step 2: Run the migration tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_harness_registry.py -k 'legacy_events or mixed_sequence or migration_is_idempotent'`

Expected: FAIL because `migrate_legacy_harness_event_sequences` and its report do not exist.

- [ ] **Step 3: Implement the report and migration transaction**

Add:

```python
@dataclass(frozen=True)
class LegacySequenceMigrationReport:
    migrated_sessions: int
    migrated_events: int
    mixed_sessions: tuple[str, ...]

def migrate_legacy_harness_event_sequences(con):
    mixed = tuple(row[0] for row in con.execute("""
        SELECT session_id FROM harness_events GROUP BY session_id
        HAVING count(*) FILTER (WHERE seq IS NULL) > 0
           AND count(*) FILTER (WHERE seq IS NOT NULL) > 0
        ORDER BY session_id
    """).fetchall())
    eligible = [row[0] for row in con.execute("""
        SELECT session_id FROM harness_events GROUP BY session_id
        HAVING count(*) > 0 AND count(seq) = 0
        ORDER BY session_id
    """).fetchall()]
    migrated_events = 0
    con.execute("BEGIN TRANSACTION")
    try:
        for session_id in eligible:
            event_count = con.execute(
                "SELECT count(*) FROM harness_events WHERE session_id = ?",
                [session_id],
            ).fetchone()[0]
            con.execute("""
                UPDATE harness_events AS target SET seq = ranked.new_seq
                FROM (
                    SELECT event_id, row_number() OVER (
                        ORDER BY created_at, event_id
                    )::INTEGER AS new_seq
                    FROM harness_events WHERE session_id = ?
                ) AS ranked
                WHERE target.event_id = ranked.event_id
            """, [session_id])
            migrated_events += int(event_count)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return LegacySequenceMigrationReport(len(eligible), migrated_events, mixed)
```

Count eligible rows before each update, as above, rather than depending on DuckDB cursor row-count behavior. Call the helper after `_ensure_harness_columns` adds `seq`.

- [ ] **Step 4: Run focused and registry tests**

Run: `uv run --extra dev python -m pytest -q tests/test_harness_registry.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/schema.py tests/test_harness_registry.py
git commit -m "fix(harness): sequence legacy transcript events"
```

### Task 2: Canonical wire serialization

**Files:**
- Modify: `src/drover/server/harness/models.py`
- Modify: `src/drover/server/web/app.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_harness_websocket.py`

**Interfaces:**
- Consumes: migrated `HarnessEvent.seq` from Task 1.
- Produces: `HarnessEvent.wire_payload() -> dict[str, Any]`.
- Guarantees: returned object always carries row-authoritative `event_id`, `seq`, and `session_id`.

- [ ] **Step 1: Write failing REST and WebSocket tests**

Create an event whose `payload_json` omits `event_id` and `seq`, then assert both routes return canonical values:

```python
def test_messages_endpoint_overlays_canonical_event_metadata(tmp_path):
    # Seed row event_id="legacy-e1", session_id="legacy", seq=1, payload_json='{"text":"hello"}'.
    # GET /harness/sessions/legacy/messages and assert message metadata comes from columns.
    assert body["messages"] == [{
        "text": "hello", "event_id": "legacy-e1", "session_id": "legacy", "seq": 1
    }]
```

Add the equivalent assertion to the WebSocket initial catch-up test.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_metrics.py tests/test_harness_websocket.py -k canonical_event_metadata`

Expected: FAIL because routes return the raw payload.

- [ ] **Step 3: Implement one canonical serializer and use it everywhere**

Add to `HarnessEvent`:

```python
def wire_payload(self) -> dict[str, Any]:
    payload = dict(self.payload)
    payload["event_id"] = self.event_id
    payload["session_id"] = self.session_id
    payload["seq"] = self.seq
    return payload
```

Reject `seq is None` at this boundary with a clear `ValueError`; null rows must be caught by the audit/migration rather than emitted as sequence zero. Replace both `event.payload` route usages in `app.py` with `event.wire_payload()`.

- [ ] **Step 4: Treat client disconnects as expected access outcomes**

Wrap only the final socket write:

```python
try:
    self.wfile.write(payload)
except (BrokenPipeError, ConnectionResetError):
    log.info("client disconnected while sending %s bytes for %s", len(payload), self.path)
```

Do not catch serialization, database, or authentication errors.

- [ ] **Step 5: Run route tests and commit**

Run: `uv run --extra dev python -m pytest -q tests/test_metrics.py tests/test_harness_websocket.py`

Expected: PASS.

```bash
git add src/drover/server/harness/models.py src/drover/server/web/app.py tests/test_metrics.py tests/test_harness_websocket.py
git commit -m "fix(api): serialize canonical harness message metadata"
```

### Task 3: Source-versioned summarizer retry ceiling

**Files:**
- Create: `src/drover/server/summarizer/jobs.py`
- Create: `tests/test_summarizer_jobs.py`
- Modify: `src/drover/schema.py`
- Modify: `src/drover/server/watcher.py`
- Modify: `src/drover/server/summarizer/worker.py`
- Test: `tests/test_watcher.py`
- Test: `tests/test_summarizer_worker.py`

**Interfaces:**
- Produces: `SUMMARY_MAX_ATTEMPTS = 5`.
- Produces: `source_version_for_session(con, session_id: str) -> str` based on canonical event count and maximum dedup key/timestamp.
- Produces: `enqueue_summary_generation(con, session_id: str, source_version: str) -> bool`; true means publish one stream entry.
- Produces: `finish_summary_failure(con: duckdb.DuckDBPyConnection, session_id: str, source_version: str, error: str, *, now: datetime, jitter: Callable[[float, float], float]) -> Literal["retry_wait", "dead_lettered"]`.

- [ ] **Step 1: Write failing state-machine tests**

Cover these exact transitions:

```python
def test_same_source_version_does_not_reset_attempt_budget(tmp_path):
    con = duckdb.connect(str(tmp_path / "jobs.duckdb"))
    bootstrap_schema(con)
    assert enqueue_summary_generation(con, "s1", "v1") is True
    con.execute(
        "UPDATE summarize_jobs SET status='dead_lettered', attempts=5 "
        "WHERE session_id='s1'"
    )
    assert enqueue_summary_generation(con, "s1", "v1") is False

def test_new_source_version_opens_fresh_generation(tmp_path):
    con = duckdb.connect(str(tmp_path / "jobs.duckdb"))
    bootstrap_schema(con)
    assert enqueue_summary_generation(con, "s1", "v1") is True
    con.execute(
        "UPDATE summarize_jobs SET status='dead_lettered', attempts=5 "
        "WHERE session_id='s1'"
    )
    assert enqueue_summary_generation(con, "s1", "v2") is True
    assert con.execute(
        "SELECT source_version,status,attempts FROM summarize_jobs "
        "WHERE session_id='s1'"
    ).fetchone() == ("v2", "pending", 0)

def test_fifth_failure_dead_letters_serving_and_pipeline_job(tmp_path):
    worker, serving, ledger = _worker_with_job(tmp_path, attempts=4, source_version="v1")
    worker._finish_failure("s1", "v1", RuntimeError("backend failed"))
    assert serving.execute(
        "SELECT status,attempts FROM summarize_jobs WHERE session_id='s1'"
    ).fetchone() == ("dead_lettered", 5)
    assert ledger.get_job("s1").state == "dead_lettered"
```

Also assert watcher publishes to its stream only when the enqueue helper returns true.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_summarizer_jobs.py tests/test_watcher.py tests/test_summarizer_worker.py -k 'source_version or dead_letter or publish'`

Expected: FAIL because the source-version state machine does not exist.

- [ ] **Step 3: Extend the serving schema**

Add nullable-safe bootstrap columns to `summarize_jobs`:

```sql
source_version   VARCHAR,
max_attempts     INTEGER DEFAULT 5,
next_run_at      TIMESTAMP,
dead_lettered_at TIMESTAMP
```

The new helper updates an existing row only when `source_version IS DISTINCT FROM ?`; that update sets `status='pending'`, `attempts=0`, `last_error=NULL`, `next_run_at=NULL`, and `dead_lettered_at=NULL`.

- [ ] **Step 4: Enforce bounded failure and synchronize the durable ledger**

In the worker, reject claims whose status is `dead_lettered`; acknowledge a stale stream entry without running the backend. Compare the delivery's `source_version` with the serving row and acknowledge mismatches as obsolete, so an old Redis entry cannot spend a newer generation's retry budget. For the no-Redis path, `_claim_duckdb_job` first promotes `retry_wait` rows whose `next_run_at <= now()` back to `pending`. On failure:

```python
terminal = attempts >= max_attempts
status = "dead_lettered" if terminal else "retry_wait"
base_seconds = min(60 * (2 ** max(0, attempts - 1)), 3600)
next_run_at = None if terminal else now + seconds(base_seconds + jitter(0, base_seconds * 0.2))
```

Inject the clock and jitter function for deterministic tests. For terminal failure, call `ledger_shadow.fail_and_dead_letter(ledger_job_id, error_message=str(exc), error_category="summarizer")`; that wrapper invokes `Ledger.fail_job` followed by `Ledger.dead_letter_job`. For retryable failure, call `ledger_shadow.retry(ledger_job_id, error_message=str(exc), next_run_at=next_run_at)`. A newly read stream entry whose serving row is `retry_wait` before `next_run_at` remains unacknowledged for later reclaim; it is not processed early.

- [ ] **Step 5: Publish only changed generations**

In `watcher.py`, compute a source version after ingestion and replace unconditional stream publication with:

```python
created = enqueue_summary_generation(con, str(sid), source_version)
if created and self._summarize_job_stream is not None:
    self._summarize_job_stream.add({"session_id": str(sid), "source_version": source_version})
```

Use a SHA-256 over stable session facts returned by one query: logical event count, maximum event timestamp, and maximum dedup key. Do not hash message content.

- [ ] **Step 6: Run worker/watcher/ledger tests and commit**

Run: `uv run --extra dev python -m pytest -q tests/test_summarizer_jobs.py tests/test_summarizer_worker.py tests/test_watcher.py tests/test_ledger.py tests/test_ledger_cutover.py tests/test_job_streams.py`

Expected: PASS.

```bash
git add src/drover/schema.py src/drover/server/summarizer/jobs.py src/drover/server/summarizer/worker.py src/drover/server/watcher.py src/drover/server/ledger_shadow.py tests/test_summarizer_jobs.py tests/test_summarizer_worker.py tests/test_watcher.py tests/test_ledger.py tests/test_ledger_cutover.py
git commit -m "fix(summarizer): bound retries by source generation"
```

### Task 4: Audit commands and operational metrics

**Files:**
- Modify: `src/drover/server/__main__.py`
- Modify: `src/drover/server/metrics.py`
- Test: `tests/test_server_cli.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces CLI: `drover-server harness audit-sequences --db PATH --json`.
- Produces CLI: `drover-server harness migrate-sequences --db PATH --apply`; dry-run without `--apply`.
- Produces metrics: `drover_harness_legacy_unsequenced_events`, `drover_harness_mixed_sequence_sessions`, `drover_summarize_jobs{status="<state>"}`, `drover_summarize_max_attempts`, `drover_summarize_oldest_retry_seconds`.

- [ ] **Step 1: Write failing CLI and metrics tests**

Assert dry-run never mutates, `--apply` reports exact session/event counts, mixed sessions make the command exit non-zero, and metric output includes dead-letter/retry counts without session IDs.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run --extra dev python -m pytest -q tests/test_server_cli.py tests/test_metrics.py -k 'sequence or summarize_max_attempts or oldest_retry'`

Expected: FAIL because commands and metrics do not exist.

- [ ] **Step 3: Implement the commands and metrics**

Reuse Task 1's report/query helpers. Commands must open the override database path, print JSON with `database`, `null_event_count`, `all_null_sessions`, `mixed_sessions`, and `applied`, and avoid printing payload data.

- [ ] **Step 4: Run the complete server verification gate**

Run:

```bash
uv run --extra dev python -m pytest -q
uv run --extra dev python scripts/check_public_release.py
```

Expected: all tests pass and `Public release audit: 0 finding(s)`.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/__main__.py src/drover/server/metrics.py tests/test_server_cli.py tests/test_metrics.py
git commit -m "feat(ops): audit transcript sequences and retry health"
```

### Task 5: Back up, deploy, and verify production correctness

**Files:**
- No source changes.

**Interfaces:**
- Consumes both production database paths and the Task 4 commands.
- Produces timestamped backups plus before/after audit JSON retained outside git.

- [ ] **Step 1: Confirm the exact deployment commit and clean tree**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git diff origin/main...HEAD --stat
```

Expected: only reviewed plan commits are ahead; no uncommitted files.

- [ ] **Step 2: Capture dry-run audits**

Before shutdown, resolve the exact central DuckDB path from the deployed launch-agent/config and run the audit command against that path. The harness daemon database is process-locked, so record its audit during the controlled restart window rather than bypassing the lock.

- [ ] **Step 3: Stop services and create explicit backups**

Use `launchctl bootout gui/$(id -u)/com.drover.harnessd` followed by `launchctl bootout gui/$(id -u)/com.drover.server`. Copy each exact database to a timestamped sibling such as `drover.duckdb.pre-seq-20260806THHMMSS.bak`; do not overwrite an existing backup.

- [ ] **Step 4: Apply migration and restart services**

Run `drover-server harness migrate-sequences --db <exact-path> --apply` for central and harness databases, then bootstrap the launch agents with their existing plist files. Verify `/healthz` and `/harness/hosts` using the local token without printing it.

- [ ] **Step 5: Verify the original production symptom**

For each of the 31 audited session IDs, request `/messages?after_seq=0`, assert returned count equals the pre-migration row count, and assert every message has positive `seq` and non-empty `event_id`. Open the largest formerly blank session on the iPhone and confirm visible content.

- [ ] **Step 6: Verify retry containment**

Observe metrics and logs through at least one retry window. Assert the poisoned source version stops at five attempts, enters `dead_lettered`, and produces no repeated backend calls. Record rollback commands and backup paths in the deployment note.
