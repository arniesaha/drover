# Transcript Model & Snapshot Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `harness_events` the single source of truth for session transcripts, stop silently losing events under write contention, and remove a 483 MB database copy from the fleet-snapshot request path.

**Architecture:** `harness_transcript_chunks` duplicates data already in `harness_events` (verified byte-identical 1:1), so its two consumers move to events, its writers are deleted, and the table is dropped. Both registry write paths gain bounded retry plus a `dropped_events` counter instead of a bare `except: pass`. `MetricsCollector.harness_snapshot` stops copying the DuckDB file and queries the live database under the existing connect lock, behind a short TTL cache.

**Tech Stack:** Python 3.11+, DuckDB, pytest, plain-JS web view (`harness_terminal.html`).

Implements SP1 and SP2 of `docs/superpowers/specs/2026-08-04-transcript-durability-and-legibility-design.md`. SP3 (session legibility) and SP5 (relay E2E determinism) are a separate plan, written after this one lands.

## Global Constraints

- Python >= 3.11. Run tests with `uv run pytest`, not bare `pytest` (the venv is uv-managed).
- Formatting: `uv run black` — line length 88. CI checks any changed file under `src/**/*.py` or `tests/*.py`; an unformatted file fails the build.
- Never let a registry write failure propagate to a caller. `emit()` runs on a driver's stdout-pump thread and an escaped exception silently kills it, freezing the session. Retry, count, continue — never raise.
- Ordering is load-bearing in Tasks 1-5: both consumers must stop reading `transcript_chunks` before the writers are removed, and writers must go before the table is dropped. Do not reorder.
- `DROP TABLE harness_transcript_chunks` is irreversible. It is justified by a verified byte-identical comparison, not an assumption — see the spec's evidence table.

---

### Task 1: Move web terminal scrollback off `transcript_chunks`

The web view rebuilds terminal scrollback from `data.transcript_chunks`. It must read `terminal.output` events instead — the same payload already carries `events`, and this file already renders them at line 494.

**Files:**
- Modify: `src/drover/server/web/static/harness_terminal.html:780`

**Interfaces:**
- Consumes: the `/harness/sessions/<id>` JSON payload, fields `events[]` (each with `event_type` and `payload_json`) and `transcript_chunks[]`.
- Produces: nothing consumed by later tasks. This task only removes a reader.

- [ ] **Step 1: Replace the scrollback source**

Find this line (currently line 780):

```javascript
      (data.transcript_chunks || []).forEach((chunk) => append(chunk.content_redacted || ""));
```

Replace with:

```javascript
      // Scrollback comes from terminal.output events, not a separate chunk
      // table: the two carried byte-identical text, and the chunk table is
      // gone. payload_json is a string on the wire.
      (data.events || [])
        .filter((event) => event.event_type === "terminal.output")
        .forEach((event) => {
          let text = "";
          try {
            text = (JSON.parse(event.payload_json || "{}") || {}).text || "";
          } catch (err) {
            text = "";
          }
          append(text);
        });
```

- [ ] **Step 2: Verify the payload field name**

Run: `uv run python -c "import sys; sys.path.insert(0,'src'); from drover.server.harness.models import HarnessEvent; print([f for f in HarnessEvent.__dataclass_fields__])"`
Expected: output includes both `event_type` and `payload_json`. If the field is named differently, use the actual name — the JS reads `event.__dict__` as serialized by `metrics.harness_session_snapshot`.

- [ ] **Step 3: Commit**

```bash
git add src/drover/server/web/static/harness_terminal.html
git commit -m "refactor(web): build terminal scrollback from terminal.output events"
```

---

### Task 2: Drop `transcript_chunks` from the session-detail payload

**Files:**
- Modify: `src/drover/server/metrics.py:888,903`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `HarnessRegistry.list_transcript_chunks` (removed in Task 4).
- Produces: the `/harness/sessions/<id>` payload no longer has a `transcript_chunks` key. Task 4 relies on this call site being gone.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_metrics.py`:

```python
def test_session_snapshot_has_no_transcript_chunks_key(tmp_path):
    """Scrollback comes from terminal.output events; the chunk table is gone."""
    collector, registry = _metrics_collector_with_registry(tmp_path)
    session = registry.create_session(
        host_id="h1", harness="shell", command="sh", mode="pty"
    )
    registry.append_event(
        session_id=session.session_id,
        event_type="terminal.output",
        payload={"text": "hello"},
    )

    snapshot = collector.harness_session_snapshot(session.session_id)

    assert "transcript_chunks" not in snapshot
    assert any(e["event_type"] == "terminal.output" for e in snapshot["events"])
```

If `_metrics_collector_with_registry` does not exist in this file, build the collector the way the nearest existing `harness_session_snapshot` test does — search for `harness_session_snapshot` in `tests/test_metrics.py` and copy its setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_session_snapshot_has_no_transcript_chunks_key -v`
Expected: FAIL — `assert "transcript_chunks" not in snapshot`.

- [ ] **Step 3: Remove the field and its query**

In `src/drover/server/metrics.py::harness_session_snapshot`, delete this line (currently 888):

```python
                chunks = registry.list_transcript_chunks(session_id)
```

and this line from the returned dict (currently 903):

```python
                    "transcript_chunks": [chunk.__dict__ for chunk in chunks],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v -k "snapshot"`
Expected: PASS. Two pre-existing tests call `registry.append_transcript_chunk` (lines ~584 and ~1391) — they still pass here because the writer is not removed until Task 3.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "refactor(hub): drop transcript_chunks from session detail payload"
```

---

### Task 3: Stop writing transcript chunks

Removes the terminal mirror's chunk path and the dead `_safe_append_transcript_chunk` (defined, never called).

**Files:**
- Modify: `src/drover/server/harness/daemon.py:2097` (call site), `:2265-2279` (dead method), `:2638-2650` (`record_chunk`), `:2675-2683` (`_flush`)
- Test: `tests/test_harness_websocket.py:171,206,255`

**Interfaces:**
- Consumes: `_TerminalMirror.record_event`, `HarnessRegistry.append_events_if_new` (both unchanged).
- Produces: `_TerminalMirror` no longer has `record_chunk`; its queue carries only `("event", record)` tuples. Task 6 rewrites `_flush` on top of this shape.

- [ ] **Step 1: Update the websocket tests to assert on events**

In `tests/test_harness_websocket.py`, replace the chunk assertion at ~line 171:

```python
        chunks = state.registry.list_transcript_chunks(session_id)
```

with:

```python
        outputs = [
            event
            for event in state.registry.list_events(session_id)
            if event.event_type == "terminal.output"
        ]
```

and update the assertion on the following line(s) to check `outputs` — each `event.payload["text"]` holds what `chunk.content_redacted` used to. Apply the same change at ~line 255 (`real_registry.list_transcript_chunks`).

At ~line 206, remove `"append_transcript_chunk"` from the fault-injection set:

```python
        if name in {"append_event", "append_events_if_new"}:
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_websocket.py -v`
Expected: the two rewritten tests FAIL — the mirror still records chunks and the event assertion may pass, but the fault-injection test now exercises a path that still calls `append_transcript_chunk`. Read the failure before proceeding; if they already pass, the assertions are not actually checking the new path and need tightening.

- [ ] **Step 3: Remove the call site**

In `src/drover/server/harness/daemon.py`, delete (currently 2097-2101):

```python
                    mirror.record_chunk(
                        session_id=session_id,
                        content_redacted=content,
                        byte_count=len(output),
                    )
```

- [ ] **Step 4: Remove `record_chunk` and the flush branch**

Delete `_TerminalMirror.record_chunk` entirely (currently 2638-2650). Then simplify `_flush` (currently 2675-2683) from:

```python
    def _flush(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        # Same swallow-and-continue stance as _safe_append_event: a locked
        # or failing registry must never take the terminal down with it.
        try:
            for kind, record in batch:
                if kind == "chunk":
                    self._registry.append_transcript_chunk(**record)
            events = [record for kind, record in batch if kind == "event"]
            if events:
                self._registry.append_events_if_new(events)
        except Exception:
            pass
```

to:

```python
    def _flush(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        # Same swallow-and-continue stance as _safe_append_event: a locked
        # or failing registry must never take the terminal down with it.
        # (Task 6 replaces the bare swallow with retry + a dropped counter.)
        events = [record for kind, record in batch if kind == "event"]
        if not events:
            return
        try:
            self._registry.append_events_if_new(events)
        except Exception:
            pass
```

- [ ] **Step 5: Remove the dead method**

Delete `_safe_append_transcript_chunk` entirely (currently 2265-2279). Confirm it has no callers first:

Run: `git grep -n "_safe_append_transcript_chunk" -- src/ tests/`
Expected: only the definition itself. If anything else appears, stop and re-read that caller.

- [ ] **Step 6: Update the daemon test's fake registry**

In `tests/test_harness_daemon.py:58`, delete the stub:

```python
    def append_transcript_chunk(self, **kwargs):
```

along with its body.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_websocket.py tests/test_harness_daemon.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/drover/server/harness/daemon.py tests/test_harness_websocket.py tests/test_harness_daemon.py
git commit -m "refactor(harnessd): stop writing transcript chunks; drop dead chunk writer"
```

---

### Task 4: Remove the registry chunk API and the model

**Files:**
- Modify: `src/drover/server/harness/registry.py:20,448-519,521-543`, `src/drover/server/harness/models.py:132-149`, `src/drover/server/harness/__init__.py:7,16`
- Test: `tests/test_harness_registry.py:121-163,411-425`

**Interfaces:**
- Consumes: `harness_events` via `_rows`.
- Produces: `HarnessRegistry.transcript_text(session_id, *, limit: int = 200) -> str` — events-only. `HarnessTranscriptChunk`, `append_transcript_chunk`, `get_transcript_chunk`, and `list_transcript_chunks` no longer exist.

- [ ] **Step 1: Delete the chunk-preference test and rewrite the ordering test**

In `tests/test_harness_registry.py`, delete `test_transcript_text_prefers_recorded_pty_chunks` entirely (line ~411) — it tests a behavior this task removes.

Replace `test_append_events_and_transcript_chunks_in_order` (line ~121) with an events-only version:

```python
def test_append_events_in_order(tmp_path):
    registry, _ = _registry(tmp_path)
    for seq, text in enumerate(["first", "second", "third"], start=1):
        registry.append_event(
            session_id="harness-session-2",
            event_type="terminal.output",
            payload={"text": text},
            seq=seq,
        )

    events = registry.list_events("harness-session-2")
    assert [event.payload["text"] for event in events] == [
        "first",
        "second",
        "third",
    ]
```

In `test_bootstrap_creates_harness_tables` (line ~35), remove `"harness_transcript_chunks"` from the asserted set.

At line ~452, `test_transcript_text_rebuilds_structured_session_from_events` asserts `registry.list_transcript_chunks(session.session_id) == []`. Delete that one assertion line; the rest of the test stands.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_registry.py -v`
Expected: FAIL — `test_bootstrap_creates_harness_tables` still sees the table (dropped in Task 5), and any test still referencing removed helpers errors.

- [ ] **Step 3: Remove the registry methods**

In `src/drover/server/harness/registry.py`, delete `append_transcript_chunk` (448-495), `get_transcript_chunk` (497-504), and `list_transcript_chunks` (506-519). Remove `HarnessTranscriptChunk` from the import at line 20.

- [ ] **Step 4: Simplify `transcript_text`**

Replace the method's body — drop the chunk preference, keep the event replay:

```python
    # Session conversation lives entirely in harness_events, for both PTY and
    # structured sessions. PTY output arrives as terminal.output events; the
    # separate transcript-chunk table it used to duplicate is gone.
    _TRANSCRIPT_EVENT_ROLES = {
        "user_input": "user",
        "assistant_output": "assistant",
        "tool_action": "tool",
        "tool_result": "tool-result",
        "terminal.output": "terminal",
    }

    def transcript_text(self, session_id: str, *, limit: int = 200) -> str:
        """Best-effort readable transcript for a session.

        Returns "" when the session has no content-bearing events.
        """
        with self._connect() as con:
            rows = _rows(
                con,
                """
                SELECT event_type, payload_json
                FROM harness_events
                WHERE session_id = ? AND event_type IN
                      ('user_input', 'assistant_output', 'tool_action',
                       'tool_result', 'terminal.output')
                ORDER BY COALESCE(seq, 0), created_at, event_id
                """,
                [session_id],
            )
        lines: list[str] = []
        for row in rows[-limit:]:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            label = self._TRANSCRIPT_EVENT_ROLES.get(str(row.get("event_type")), "note")
            lines.append(f"[{label}] {text}")
        return "\n".join(lines).strip()
```

Note the added `terminal.output` mapping: PTY transcripts previously came from chunks, so this is what preserves handoff context for PTY sessions.

- [ ] **Step 5: Remove the model and its export**

Delete `HarnessTranscriptChunk` from `src/drover/server/harness/models.py` (132-149). Remove both references in `src/drover/server/harness/__init__.py` (lines 7 and 16).

- [ ] **Step 6: Fix remaining test writers**

Run: `git grep -n "append_transcript_chunk\|list_transcript_chunks\|HarnessTranscriptChunk" -- src/ tests/`

Every remaining hit is a test that writes chunks as setup — `tests/test_metrics.py` (~584, ~1391). Rewrite each to `registry.append_event(session_id=..., event_type="terminal.output", payload={"text": ...})`. Do not delete these tests; they assert other behavior.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_registry.py tests/test_metrics.py -v`
Expected: PASS except `test_bootstrap_creates_harness_tables`, which stays red until Task 5.

- [ ] **Step 8: Commit**

```bash
git add src/drover/server/harness/registry.py src/drover/server/harness/models.py src/drover/server/harness/__init__.py tests/
git commit -m "refactor(registry): remove transcript-chunk API; transcript_text reads events"
```

---

### Task 5: Drop the table

**Files:**
- Modify: `src/drover/server/harness/schema.py:7-12,66-76,79-113`
- Test: `tests/test_harness_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `bootstrap_harness_tables(con)` no longer creates `harness_transcript_chunks` and drops it if present. `HARNESS_TABLES` has three entries.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_harness_registry.py`:

```python
def test_bootstrap_drops_legacy_transcript_chunk_table(tmp_path):
    """The chunk table duplicated terminal.output events; bootstrap removes it."""
    _, duckdb_path = _registry(tmp_path)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS harness_transcript_chunks ("
            "chunk_id VARCHAR PRIMARY KEY, session_id VARCHAR)"
        )

    from drover.server.harness.schema import bootstrap_harness_tables

    with duckdb.connect(str(duckdb_path)) as con:
        bootstrap_harness_tables(con)
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }

    assert "harness_transcript_chunks" not in tables
    assert {"harness_hosts", "harness_sessions", "harness_events"}.issubset(tables)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_registry.py::test_bootstrap_drops_legacy_transcript_chunk_table -v`
Expected: FAIL — the table is still created by bootstrap.

- [ ] **Step 3: Remove the DDL and add the drop**

In `src/drover/server/harness/schema.py`:

Change `HARNESS_TABLES` to:

```python
HARNESS_TABLES = (
    "harness_hosts",
    "harness_sessions",
    "harness_events",
)
```

Delete the `_HARNESS_TRANSCRIPT_CHUNKS_DDL` constant entirely. In `bootstrap_harness_tables`, replace the final `con.execute(_HARNESS_TRANSCRIPT_CHUNKS_DDL)` with:

```python
    # Dropped, not migrated: every row duplicated a terminal.output event
    # byte-for-byte (verified 1:1 on live data), so there is nothing here
    # that harness_events does not already hold.
    con.execute("DROP TABLE IF EXISTS harness_transcript_chunks")
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, 1058+ tests. Any failure here is a chunk reference missed in Tasks 1-4 — fix it rather than adapting the test.

- [ ] **Step 5: Format and commit**

```bash
uv run black src/drover/server/harness/schema.py tests/test_harness_registry.py
git add src/drover/server/harness/schema.py tests/test_harness_registry.py
git commit -m "refactor(schema): drop harness_transcript_chunks; events are the transcript"
```

---

### Task 6: Retry and count dropped events in the terminal mirror

**Files:**
- Modify: `src/drover/server/harness/daemon.py` (`_TerminalMirror`)
- Test: `tests/test_harness_websocket.py`

**Interfaces:**
- Consumes: `HarnessRegistry.append_events_if_new(records) -> int`.
- Produces: module-level `dropped_event_count() -> int` and `reset_dropped_event_count() -> None` in `drover.server.harness.daemon`. Task 7 increments the same counter; Task 8 reads it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_harness_websocket.py`:

```python
def test_mirror_retries_then_counts_dropped_events(monkeypatch, tmp_path):
    """A write failure must be retried, and a permanent one must be counted.

    The old bare `except Exception: pass` lost events silently and forever.
    """
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()

    attempts = {"n": 0}

    class AlwaysFailingRegistry:
        def append_events_if_new(self, records):
            attempts["n"] += 1
            raise RuntimeError("TransactionException: write-write conflict")

        def append_event(self, **kwargs):
            return None

    mirror = daemon_mod._TerminalMirror(AlwaysFailingRegistry())
    mirror.record_event({"event_id": "e1", "session_id": "s1"})
    mirror.stop()

    assert attempts["n"] == 3, "should retry twice after the first failure"
    assert daemon_mod.dropped_event_count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_websocket.py::test_mirror_retries_then_counts_dropped_events -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'reset_dropped_event_count'`.

- [ ] **Step 3: Add the counter**

Near the top of `src/drover/server/harness/daemon.py`, after the imports:

```python
# Registry writes are best-effort by design -- an exception on a driver's
# pump thread would kill it and freeze the session. But "best effort" must
# not mean "silently lost forever": every permanently failed write bumps
# this counter, which the metrics endpoint exports.
_dropped_events_total = 0
_dropped_events_lock = threading.Lock()


def record_dropped_events(count: int = 1) -> None:
    global _dropped_events_total
    with _dropped_events_lock:
        _dropped_events_total += count


def dropped_event_count() -> int:
    with _dropped_events_lock:
        return _dropped_events_total


def reset_dropped_event_count() -> None:
    global _dropped_events_total
    with _dropped_events_lock:
        _dropped_events_total = 0
```

A plain lock-guarded integer, not `itertools.count()` — the value must be
readable without consuming it. `threading` is already imported in this module.

- [ ] **Step 4: Add retry to `_flush`**

Replace `_TerminalMirror._flush` with:

```python
    def _flush(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        events = [record for kind, record in batch if kind == "event"]
        if not events:
            return
        # DuckDB write-write conflicts under concurrent writers are
        # transient, so retry before giving up. A failure must never
        # propagate: this runs on the mirror's worker thread.
        for attempt in range(3):
            try:
                self._registry.append_events_if_new(events)
                return
            except Exception:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        record_dropped_events(len(events))
        try:
            self._registry.append_event(
                session_id=events[0].get("session_id", ""),
                event_type="transcript.gap",
                payload={"dropped": len(events)},
                normalized_type="status",
            )
        except Exception:
            pass
```

Confirm `time` is already imported in this module; if not, add `import time`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_harness_websocket.py::test_mirror_retries_then_counts_dropped_events -v`
Expected: PASS.

- [ ] **Step 6: Add the retry-succeeds test**

```python
def test_mirror_retry_succeeds_without_counting_a_drop(tmp_path):
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    attempts = {"n": 0}

    class FlakyRegistry:
        def append_events_if_new(self, records):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("TransactionException")
            return len(records)

        def append_event(self, **kwargs):
            raise AssertionError("no gap marker expected on eventual success")

    mirror = daemon_mod._TerminalMirror(FlakyRegistry())
    mirror.record_event({"event_id": "e1", "session_id": "s1"})
    mirror.stop()

    assert attempts["n"] == 2
    assert daemon_mod.dropped_event_count() == 0
```

- [ ] **Step 7: Run both tests**

Run: `uv run pytest tests/test_harness_websocket.py -v -k "dropped or retry"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
uv run black src/drover/server/harness/daemon.py tests/test_harness_websocket.py
git add src/drover/server/harness/daemon.py tests/test_harness_websocket.py
git commit -m "fix(harnessd): retry mirror writes and count permanent drops"
```

---

### Task 7: Retry and count dropped events in the structured manager

**Files:**
- Modify: `src/drover/server/harness/structured/manager.py:122-147`
- Test: `tests/test_structured_manager.py` (already exists — 374 lines)

**Interfaces:**
- Consumes: `drover.server.harness.daemon.record_dropped_events` and `dropped_event_count` from Task 6.
- Produces: no new API.

- [ ] **Step 1: Read the existing failure test**

`tests/test_structured_manager.py` already has
`test_emit_survives_registry_write_failure(monkeypatch, tmp_path, capsys)` and a
`_build_manager(monkeypatch, tmp_path, *, session_id="sess-1")` helper that
installs a `_StubDriver`. Read both before writing anything — the new test is a
sibling of the existing one and must use the same helper.

Run: `uv run pytest tests/test_structured_manager.py::test_emit_survives_registry_write_failure -v`
Expected: PASS. This is the behavior being extended, not replaced — emit must
still never raise.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_structured_manager.py`, following the construction used by
`test_emit_survives_registry_write_failure`:

```python
def test_emit_retries_then_counts_a_permanent_drop(monkeypatch, tmp_path):
    """emit() must never raise -- but it must not lose the event silently.

    The old handler wrote one attempt and swallowed the failure, so a
    transient DuckDB write-write conflict discarded the event forever.
    """
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    attempts = {"n": 0}

    def failing_append_event(self, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("TransactionException: write-write conflict")

    monkeypatch.setattr(HarnessRegistry, "append_event", failing_append_event)
    monkeypatch.setattr(
        HarnessRegistry, "update_session_activity", lambda self, *a, **k: None
    )

    manager, session_id, driver = _build_manager(monkeypatch, tmp_path)
    driver.emit(StructuredMessage(type="assistant_output", role="assistant", text="hi"))

    assert attempts["n"] == 3, "one attempt plus two retries"
    assert daemon_mod.dropped_event_count() == 1
```

`_build_manager`'s exact return shape is whatever the existing tests unpack —
match it. If it returns something other than a 3-tuple, adapt this line and
reach the driver's `emit` the way the neighbouring tests do.

- [ ] **Step 2b: Run test to verify it fails**

Run: `uv run pytest tests/test_structured_manager.py::test_emit_retries_then_counts_a_permanent_drop -v`
Expected: FAIL — `attempts["n"] == 1`, counter stays 0.

- [ ] **Step 3: Add retry**

In `manager.py`, replace the single `registry.append_event(...)` / `except Exception` block (122-147) with:

```python
                from drover.server.harness.daemon import record_dropped_events

                recorded = False
                for attempt in range(3):
                    try:
                        registry.append_event(
                            session_id=session_id,
                            event_type=message.type,
                            payload=event_payload,
                            seq=seq,
                            harness=harness,
                            normalized_source="structured",
                        )
                        registry.update_session_activity(
                            session_id, awaiting=awaiting
                        )
                        recorded = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt < 2:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        # Counts only -- no event text, it may contain
                        # sensitive content. on_message still runs below,
                        # so the central copy can still succeed.
                        print(
                            "drover structured manager: registry write failed "
                            f"for session {session_id} seq {seq} "
                            f"({type(exc).__name__}); event not recorded "
                            "locally",
                            file=sys.stderr,
                        )
                if not recorded:
                    record_dropped_events(1)
```

Add `import time` to the module's imports if absent. The local import of `record_dropped_events` avoids a circular import — `daemon` imports from `structured`, not the reverse.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_structured_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run black src/drover/server/harness/structured/manager.py tests/test_structured_manager.py
git add src/drover/server/harness/structured/manager.py tests/test_structured_manager.py
git commit -m "fix(harnessd): retry structured event writes and count permanent drops"
```

---

### Task 8: Export `dropped_events` from the metrics endpoint

**Deviation from the spec, deliberate.** The spec says "surface `dropped_events`
through `doctor`". `doctor.py`'s only harness references are field labels inside
`audit_lakehouse`; it has no harness-health section, so a counter would not fit
there. The Prometheus renderer already has an established
`_append_<area>_metrics(lines, ...)` pattern for exactly this, and a monotonic
operational counter belongs on a metrics endpoint. Intent preserved, mechanism
changed.

**Files:**
- Modify: `src/drover/server/metrics.py` (new `_append_harness_metrics`, called from `_refresh_if_needed`)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `drover.server.harness.daemon.dropped_event_count()` from Task 6.
- Produces: `_append_harness_metrics(lines: list[str]) -> None`. Prometheus output gains `drover_harness_dropped_events_total`.

- [ ] **Step 1: Read the existing pattern**

Run: `sed -n '195,215p' src/drover/server/metrics.py`

That is `_append_redis_metrics` — the shortest example of the HELP/TYPE/value
shape. The new function must match it.

- [ ] **Step 2: Write the failing test**

```python
def test_prometheus_exports_dropped_harness_events(tmp_path):
    """A non-zero counter means transcript content was permanently lost."""
    from drover.server.harness import daemon as daemon_mod

    daemon_mod.reset_dropped_event_count()
    daemon_mod.record_dropped_events(4)

    collector, _ = _metrics_collector_with_registry(tmp_path)
    text = collector.render_prometheus()

    assert "drover_harness_dropped_events_total" in text
    assert "drover_harness_dropped_events_total 4" in text
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_prometheus_exports_dropped_harness_events -v`
Expected: FAIL — the metric name is absent.

- [ ] **Step 4: Add the exporter**

Beside the other `_append_*_metrics` functions:

```python
def _append_harness_metrics(lines: list[str]) -> None:
    # Non-zero means registry writes failed permanently and transcript
    # content was lost. Gaps are marked in-band with transcript.gap events.
    from drover.server.harness.daemon import dropped_event_count

    lines.extend(
        [
            "# HELP drover_harness_dropped_events_total "
            "Harness events permanently lost after write retries.",
            "# TYPE drover_harness_dropped_events_total counter",
            f"drover_harness_dropped_events_total {dropped_event_count()}",
        ]
    )
```

Then call it in `_refresh_if_needed`, beside the existing appenders:

```python
            _append_adoption_metrics(lines, snapshot)
            _append_harness_metrics(lines)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_metrics.py -v -k dropped`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uv run black src/drover/server/metrics.py tests/test_metrics.py
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): export drover_harness_dropped_events_total"
```

---

### Task 9: Stop copying the database on every snapshot

**Files:**
- Modify: `src/drover/server/metrics.py:841-873` (`harness_snapshot`), `:874-905` (`harness_session_snapshot`)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `HarnessRegistry(self.duckdb_path)` against the live database — the pattern already used by `_build_handoff_prompt` and `_sync_terminated_harness_session` in this same file.
- Produces: both methods keep their existing return shapes exactly. Task 10 wraps `harness_snapshot` in a cache.

- [ ] **Step 1: Write the failing test**

```python
def test_harness_snapshot_does_not_copy_the_database(tmp_path, monkeypatch):
    """The hub DB is ~483MB; copying it per poll was a 16% disk duty cycle."""
    collector, registry = _metrics_collector_with_registry(tmp_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")

    copies: list = []
    real_copy = shutil.copy2
    monkeypatch.setattr(
        shutil, "copy2", lambda *a, **k: (copies.append(a), real_copy(*a, **k))[1]
    )

    snapshot = collector.harness_snapshot()

    assert copies == [], "harness_snapshot must query the live DB, not copy it"
    assert len(snapshot["sessions"]) == 1
```

Reuse this file's existing collector helper; if it is named differently, use the actual name.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_harness_snapshot_does_not_copy_the_database -v`
Expected: FAIL — `copies` has one entry.

- [ ] **Step 3: Replace the copy with a live query**

In `harness_snapshot`, replace the `tempfile`/`copy2` block with a direct registry read:

```python
        try:
            # Query the live database rather than copying it: the file is
            # ~483MB and this runs on every fleet poll (measured 0.78s per
            # copy at a 5s poll = a 16% disk duty cycle per client). Two
            # indexed reads under the registry's connect lock cost
            # microseconds. Live reads beside live writers are the supported
            # path -- see open_duckdb_connection's docstring.
            registry = HarnessRegistry(source)
            hosts = registry.list_hosts() if include_hosts else []
            sessions = registry.list_sessions() if include_sessions else []
            return {
                "hosts": [
                    _harness_host_dict(host, self.relay_manager) for host in hosts
                ],
                "sessions": [session.__dict__ for session in sessions],
                "cwd_suggestions": _harness_cwd_suggestions(
                    sessions, self.favorite_cwds
                ),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to render harness snapshot: %s", exc)
            return {"hosts": [], "sessions": [], "error": str(exc)}
```

- [ ] **Step 4: Apply the same change to `harness_session_snapshot`**

Replace its `tempfile`/`copy2` block with `registry = HarnessRegistry(source)`, keeping every other line of that method — including the `native_transcript` proxy call and the return dict — exactly as-is (minus the `transcript_chunks` entry already removed in Task 2).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS. Content must be unchanged — this is a performance change only.

- [ ] **Step 6: Commit**

```bash
uv run black src/drover/server/metrics.py tests/test_metrics.py
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "perf(hub): query live DB for harness snapshots instead of copying 483MB"
```

---

### Task 10: Cache the harness snapshot behind a short TTL

**Files:**
- Modify: `src/drover/server/metrics.py` (`MetricsCollector` fields, `render_harness_json`)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `harness_snapshot()` from Task 9.
- Produces: `MetricsCollector.harness_ttl_seconds: float = 2.0`. `render_harness_json` returns cached JSON within the window.

Note `MetricsCollector.ttl_seconds` already exists and is 60.0 — that is the Prometheus cache. Do not reuse it; 60s is far too stale for a fleet view.

- [ ] **Step 1: Write the failing test**

```python
def test_render_harness_json_caches_within_ttl(tmp_path, monkeypatch):
    collector, registry = _metrics_collector_with_registry(tmp_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")

    calls = {"n": 0}
    real = collector.harness_snapshot

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(collector, "harness_snapshot", counting)

    first = collector.render_harness_json()
    second = collector.render_harness_json()

    assert calls["n"] == 1, "second call inside the TTL must be served from cache"
    assert first == second
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_render_harness_json_caches_within_ttl -v`
Expected: FAIL — `calls["n"] == 2`.

- [ ] **Step 3: Add the cache fields**

In the `MetricsCollector` dataclass, beside the existing cache fields:

```python
    # Separate from ttl_seconds (60s, Prometheus): a fleet view must feel
    # live, but N polling clients should still share one render.
    harness_ttl_seconds: float = 2.0
    _harness_cached_json: str | None = field(default=None, init=False)
    _harness_cached_until: float = field(default=0.0, init=False)
```

- [ ] **Step 4: Cache in `render_harness_json`**

```python
    def render_harness_json(
        self,
        *,
        include_hosts: bool = True,
        include_sessions: bool = True,
    ) -> str:
        # Only the default full render is cached; partial renders are rare
        # and caching them would need a per-variant key for no real gain.
        full = include_hosts and include_sessions
        now = time.monotonic()
        if full and self._harness_cached_json is not None:
            if now < self._harness_cached_until:
                return self._harness_cached_json
        snapshot = self.harness_snapshot(
            include_hosts=include_hosts,
            include_sessions=include_sessions,
        )
        rendered = json.dumps(snapshot, sort_keys=True, default=str) + "\n"
        if full:
            self._harness_cached_json = rendered
            self._harness_cached_until = now + self.harness_ttl_seconds
        return rendered
```

- [ ] **Step 5: Add the expiry test**

```python
def test_render_harness_json_refreshes_after_ttl(tmp_path, monkeypatch):
    collector, registry = _metrics_collector_with_registry(tmp_path)
    registry.create_session(host_id="h1", harness="shell", command="sh")
    collector.harness_ttl_seconds = 0.0

    calls = {"n": 0}
    real = collector.harness_snapshot

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(collector, "harness_snapshot", counting)
    collector.render_harness_json()
    collector.render_harness_json()

    assert calls["n"] == 2
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v -k harness`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run black src/drover/server/metrics.py tests/test_metrics.py
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "perf(hub): cache fleet snapshot renders behind a 2s TTL"
```

---

### Task 11: Remove the same copy from the quality and observatory snapshots

Lower frequency than the fleet poll, but the identical defect.

**Files:**
- Modify: `src/drover/server/metrics.py:1166-1181` (`_quality_snapshot`), `:1183-1200` (`_observatory_snapshot`)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `quality_snapshot(duckdb_path=...)` and `pipeline_observatory_snapshot(duckdb_path=...)` — both take a path and are unchanged.
- Produces: no API change.

- [ ] **Step 1: Write the failing test**

```python
def test_quality_and_observatory_snapshots_do_not_copy_the_database(
    tmp_path, monkeypatch
):
    collector, _ = _metrics_collector_with_registry(tmp_path)

    copies: list = []
    real_copy = shutil.copy2
    monkeypatch.setattr(
        shutil, "copy2", lambda *a, **k: (copies.append(a), real_copy(*a, **k))[1]
    )

    quality = collector._quality_snapshot()
    collector._observatory_snapshot(quality)

    assert copies == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_quality_and_observatory_snapshots_do_not_copy_the_database -v`
Expected: FAIL — two copies recorded.

- [ ] **Step 3: Pass the live path directly**

In `_quality_snapshot`, drop the `tempfile`/`copy2` wrapper and call `quality_snapshot(duckdb_path=source, incoming_dir=self.incoming_dir, deep=False)` directly.

In `_observatory_snapshot`, do the same: call `pipeline_observatory_snapshot(duckdb_path=source, runtime_audit=audit, max_artifacts=10, max_projects=10)` inside the existing `try`/`except`, with no temp directory.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run black src/drover/server/metrics.py tests/test_metrics.py
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "perf(hub): stop copying the database for quality/observatory snapshots"
```

---

### Task 12: Verify end to end and measure

**Files:** none modified.

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: PASS, no failures. Note the count; it should be within a few of 1058 (some chunk tests were removed, several added).

- [ ] **Step 2: Confirm no chunk references survive**

Run: `git grep -n "transcript_chunk\|TranscriptChunk\|record_chunk" -- src/ tests/ apps/`
Expected: no output. Doc references under `docs/` are fine and expected.

- [ ] **Step 3: Measure the snapshot win**

```bash
uv run python -c "
import sys, time; sys.path.insert(0,'src')
from pathlib import Path
from drover.server.metrics import MetricsCollector
c = MetricsCollector(duckdb_path=Path.home()/'.drover/drover.duckdb',
                     incoming_dir=Path.home()/'.drover/incoming',
                     summarizer_report={})
t0=time.time(); c.harness_snapshot(); print(f'uncached: {time.time()-t0:.4f}s')
t0=time.time(); c.render_harness_json(); c.render_harness_json(); print(f'2 cached renders: {time.time()-t0:.4f}s')
"
```

Expected: uncached well under 0.1 s (was 0.78 s); two cached renders effectively free. Record the numbers in the commit message.

- [ ] **Step 4: Confirm CI is green**

```bash
git push origin main
gh run list --limit 1
```

Expected: the run completes with `success`. If black fails, run `uv run black` on the reported files and push the fix.

---

## Notes for the implementer

**Why the drop is safe.** On the live database, the chunk table and `terminal.output` events held byte-identical content 1:1 (159 rows / 1,910 chars each for the busiest session). The chunk column is called `content_redacted`, but nothing redacts — the stored bytes are raw ANSI, the same as the event text. Events are strictly richer: they also carry `content_preview`, the ANSI-cleaned form.

**Why retry rather than raise.** `emit()` runs on a driver's stdout-pump thread. An escaped exception kills that thread and freezes the session — this was observed live with DuckDB's concurrent-connect `BinderException`. The contract is: never raise, but never lose silently either.

**If a task's test passes before you write the implementation,** the test is not exercising the path it claims. Tighten it before moving on. This plan exists partly because `tests/test_structured_codex.py` asserted a broken `--sandbox` argv and passed for weeks against a fake CLI that accepted any flag.
