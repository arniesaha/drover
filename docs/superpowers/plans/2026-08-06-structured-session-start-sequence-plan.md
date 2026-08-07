# Structured Session Start Sequence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every new structured session begins with a sequenced `session.started` event and remains fully contiguous.

**Architecture:** Add optional sequence forwarding to the daemon's best-effort event helper, then assign sequence 1 only to the structured-session start event. The structured manager's existing `max_event_seq()` initialization will continue later messages from sequence 2 under its existing per-session lock.

**Tech Stack:** Python 3.12, DuckDB-backed `HarnessRegistry`, pytest, uv

## Global Constraints

- Do not stop, restart, unload, or modify any launch agent or service.
- Do not read, write, migrate, or modify any production database or backup.
- Do not execute deployment commands or implement later rollout stages.
- Keep PTY session event sequencing unchanged.

---

### Task 1: Sequence the Structured Session Start Event

**Files:**
- Modify: `tests/test_harness_daemon.py:1604`
- Modify: `src/drover/server/harness/daemon.py:1730`
- Modify: `src/drover/server/harness/daemon.py:2398`

**Interfaces:**
- Consumes: `HarnessRegistry.append_event(..., seq: int | None = None) -> HarnessEvent` and `HarnessRegistry.max_event_seq(session_id: str) -> int`.
- Produces: `_safe_append_event(..., seq: int | None = None)` forwarding the canonical sequence to the registry; structured `session.started` persisted with `seq=1`.

- [x] **Step 1: Write the failing regression assertion**

In `test_structured_session_full_lifecycle`, replace the event-type-only read with ordered event assertions:

```python
        events = state.registry.list_events(sid)
        event_types = [event.event_type for event in events]
        assert events[0].event_type == "session.started"
        assert events[0].seq == 1
        assert all(event.seq is not None for event in events)
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert "approval_prompt" in event_types
        assert "approval_response" in event_types
```

- [x] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run --extra dev python -m pytest -q tests/test_harness_daemon.py::test_structured_session_full_lifecycle
```

Expected: FAIL because `events[0].seq` is `None` instead of `1`.

- [x] **Step 3: Implement the minimal sequence forwarding**

Add the optional parameter to `_safe_append_event` and forward it:

```python
    def _safe_append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        normalized_type: str | None = None,
        normalized_source: str | None = None,
        content_preview: str | None = None,
        seq: int | None = None,
    ):
        try:
            session = self.server.state.registry.get_session(session_id)
            event = self.server.state.registry.append_event(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                harness=session.harness if session else None,
                normalized_type=normalized_type,
                normalized_source=normalized_source,
                content_preview=content_preview,
                seq=seq,
            )
```

Pass sequence 1 at the structured start call only:

```python
        self._safe_append_event(
            session_id=session_id,
            event_type="session.started",
            payload=started_payload,
            seq=1,
        )
```

- [x] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
uv run --extra dev python -m pytest -q tests/test_harness_daemon.py::test_structured_session_full_lifecycle
```

Expected: PASS.

- [x] **Step 5: Run affected regression suites**

Run:

```bash
uv run --extra dev python -m pytest -q tests/test_harness_daemon.py tests/test_harness_registry.py tests/test_metrics.py tests/test_harness_websocket.py
```

Expected: all selected tests pass.

- [x] **Step 6: Review and commit the implementation**

Run:

```bash
git diff --check
git diff -- src/drover/server/harness/daemon.py tests/test_harness_daemon.py
git add src/drover/server/harness/daemon.py tests/test_harness_daemon.py docs/superpowers/plans/2026-08-06-structured-session-start-sequence-plan.md
git commit -m "fix(harness): sequence structured session start"
```
