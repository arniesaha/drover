# Headless Harness Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `structured` harness sessions that drive Claude Code / Codex / Gemini in their headless JSON modes, normalize their streams into one message schema, and push events continuously to the central server.

**Architecture:** A new `nexus.server.harness.structured` package holds a driver per CLI plus shared subprocess plumbing; each driver normalizes its CLI's NDJSON into `StructuredMessage`s. The daemon appends messages to its local registry (new `seq` ordering, new `awaiting`/`last_activity` session fields) and a pusher thread batches them to central's new authed ingest endpoint. Central serves history via REST and live updates via a per-session WebSocket. PTY sessions are untouched.

**Tech Stack:** Python 3.11 stdlib only (`subprocess`, `threading`, `queue`, `json`, `shutil`); DuckDB via the existing registry; pytest.

**Spec:** `docs/superpowers/specs/2026-07-06-headless-harness-protocol-design.md`

## Global Constraints

- No new runtime dependencies (stdlib only; DuckDB already present).
- Python `>=3.11`, line length 88 (black).
- Every new HTTP/WS route on central goes through the existing `_gate` (bearer or session cookie); every new daemon route goes through the existing daemon `_gate`. Never log or echo the token.
- Existing PTY sessions and routes: behavior unchanged. `mode`/`awaiting`/`last_activity` are NULL/`"pty"` for them and the UI renders as today.
- Message vocabulary (exact strings): `assistant_output`, `user_input`, `tool_action`, `tool_result`, `approval_prompt`, `approval_response`, `status`, `error`, `raw`.
- Session `awaiting` values (exact strings): `"input"`, `"approval"`, or NULL.
- Unparseable driver output must become a `raw` message — never dropped.
- CLI flags written in this plan are indicative; **Task 0's findings file is authoritative**. Driver implementers must read it and adjust spawn commands / wire shapes to what Task 0 actually captured.
- Test command: `uv run python -m pytest`.

---

### Task 0: Probe installed CLIs and capture golden fixtures

Manual/scripted verification — no production code. Everything later builds on what this task records.

**Files:**
- Create: `tests/fixtures/structured/FINDINGS.md`
- Create: `tests/fixtures/structured/claude_basic.ndjson` (+ `claude_approval.ndjson` if capturable)
- Create: `tests/fixtures/structured/codex_basic.ndjson`
- Create: `tests/fixtures/structured/gemini_basic.json`

**Interfaces:**
- Produces: FINDINGS.md documenting, per CLI: exact spawn argv for machine mode, whether stdin stays open across turns, the JSON shape of (a) a user turn submission, (b) assistant output, (c) tool use/result, (d) permission/approval requests and how to answer them, (e) turn-complete markers, (f) session id + resume mechanism. Fixture files contain real captured output (secrets/keys redacted).

- [ ] **Step 1: Probe Claude Code**

```bash
claude --version && claude --help 2>&1 | grep -iE "input-format|output-format|permission|resume|continue"
mkdir -p tests/fixtures/structured
printf '%s\n' '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Say exactly: hello nexus"}]}}' \
  | claude -p --input-format stream-json --output-format stream-json --verbose \
  > tests/fixtures/structured/claude_basic.ndjson 2>/tmp/claude_err.log || cat /tmp/claude_err.log
head -c 2000 tests/fixtures/structured/claude_basic.ndjson
```

Record in FINDINGS.md: does it emit `system/init`, `assistant`, `result` events; does the process stay alive after `result` for a second stdin turn (test by piping two user lines with a `sleep 20` between via a small script); how permission requests surface (`control_request` with subtype `can_use_tool`?) and the exact `control_response` reply shape. If a safe approval flow can be triggered (e.g. a `Bash(echo hi)` tool call with default permission mode in a scratch dir), capture it as `claude_approval.ndjson`.

- [ ] **Step 2: Probe Codex**

```bash
codex --version && codex exec --help 2>&1 | head -30 && codex proto --help 2>&1 | head -20
printf '%s\n' '{"id":"t1","op":{"type":"user_input","items":[{"type":"text","text":"Say exactly: hello nexus"}]}}' \
  | timeout 120 codex proto > tests/fixtures/structured/codex_basic.ndjson 2>/tmp/codex_err.log || true
head -c 2000 tests/fixtures/structured/codex_basic.ndjson; cat /tmp/codex_err.log | head -5
```

If `codex proto` is absent/renamed, fall back to `codex exec --json "…"` and record that structured Codex sessions are per-turn respawn with `codex exec resume <id> --json`. Record event type names actually observed (`session_configured`, `agent_message`, `exec_approval_request`, `task_complete`, …) and the approval reply op shape.

- [ ] **Step 3: Probe Gemini**

```bash
gemini --version && gemini --help 2>&1 | grep -iE "output|prompt|resume|session|approval|yolo"
gemini -p "Say exactly: hello nexus" -o json > tests/fixtures/structured/gemini_basic.json 2>/tmp/gemini_err.log || cat /tmp/gemini_err.log
head -c 1500 tests/fixtures/structured/gemini_basic.json
```

Record: JSON output shape (`response`/`stats` keys?), whether a streaming NDJSON output mode exists, the resume mechanism for a follow-up turn (`--resume`? chat tag?), and which approval-mode flag makes tool use non-interactive (or whether tools are auto-approved headlessly).

- [ ] **Step 4: Write FINDINGS.md and commit**

FINDINGS.md must answer, per CLI, every item in the Produces block above, quoting captured JSON lines. Redact absolute home paths where irrelevant and any API keys.

```bash
git add tests/fixtures/structured/
git commit -m "test: capture CLI machine-mode fixtures and findings for structured drivers"
```

---

### Task 1: Registry + schema — seq, mode, awaiting, last_activity

**Files:**
- Modify: `src/nexus/server/harness/schema.py` (the `_ensure_harness_columns` calls in `bootstrap_harness_tables`)
- Modify: `src/nexus/server/harness/registry.py` (`create_session`, `append_event`, new methods)
- Modify: `src/nexus/server/harness/models.py` (`HarnessSession`, `HarnessEvent` fields)
- Test: `tests/test_harness_registry.py` (extend the existing registry test file; if tests live elsewhere, locate the file that currently tests `create_session` and extend it)

**Interfaces:**
- Consumes: existing `HarnessRegistry` (`registry.py:114` `create_session`, `:232` `append_event`), `_ensure_harness_columns` migration helper (`schema.py:105`).
- Produces (later tasks rely on these exact names):
  - `harness_sessions` columns: `mode VARCHAR`, `awaiting VARCHAR`, `last_activity TIMESTAMP`
  - `harness_events` column: `seq INTEGER`
  - `create_session(..., mode: str = "pty")`
  - `append_event(..., seq: int | None = None)`
  - `update_session_activity(session_id: str, *, awaiting: str | None, last_activity: datetime | None = None) -> None` (always writes `awaiting`, including to NULL; `last_activity` defaults to now; does NOT touch `status` or `updated_at`)
  - `max_event_seq(session_id: str) -> int` (0 when no events)
  - `list_events_after(session_id: str, after_seq: int) -> list[HarnessEvent]` (only rows with `seq IS NOT NULL AND seq > after_seq`, ordered by `seq`)
  - `HarnessSession.mode/awaiting/last_activity`, `HarnessEvent.seq` dataclass fields

- [ ] **Step 1: Write the failing tests**

Add to the registry test file:

```python
def test_structured_session_fields_roundtrip(tmp_path):
    registry = _make_registry(tmp_path)  # reuse the file's existing factory helper
    session = registry.create_session(
        host_id="h1", harness="claude-code", command="claude -p", mode="structured"
    )
    assert session.mode == "structured"
    assert session.awaiting is None
    registry.update_session_activity(session.session_id, awaiting="approval")
    updated = registry.get_session(session.session_id)
    assert updated.awaiting == "approval"
    assert updated.last_activity is not None
    registry.update_session_activity(session.session_id, awaiting=None)
    assert registry.get_session(session.session_id).awaiting is None


def test_default_mode_is_pty(tmp_path):
    registry = _make_registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="shell", command="/bin/sh")
    assert session.mode == "pty"
    assert session.last_activity is None


def test_event_seq_ordering(tmp_path):
    registry = _make_registry(tmp_path)
    session = registry.create_session(host_id="h1", harness="claude-code", command="c")
    sid = session.session_id
    assert registry.max_event_seq(sid) == 0
    for seq in (1, 2, 3):
        registry.append_event(
            session_id=sid, event_type="assistant_output",
            payload={"seq": seq}, seq=seq,
        )
    assert registry.max_event_seq(sid) == 3
    tail = registry.list_events_after(sid, 1)
    assert [event.seq for event in tail] == [2, 3]
    # events without seq (PTY mirror path) are excluded from seq listings
    registry.append_event(session_id=sid, event_type="terminal.output", payload={})
    assert [e.seq for e in registry.list_events_after(sid, 0)] == [1, 2, 3]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/ -q -k "structured_session_fields or default_mode or event_seq"`
Expected: FAIL — `create_session() got an unexpected keyword argument 'mode'` (and missing attributes).

- [ ] **Step 3: Add the columns in schema.py**

In `bootstrap_harness_tables` (`schema.py`), extend the existing `_ensure_harness_columns` calls:

```python
    _ensure_harness_columns(
        con,
        "harness_sessions",
        {
            "native_session_id": "VARCHAR",
            "native_resume_label": "VARCHAR",
            "source_session_id": "VARCHAR",
            "handoff_mode": "VARCHAR",
            "mode": "VARCHAR",
            "awaiting": "VARCHAR",
            "last_activity": "TIMESTAMP",
        },
    )
```

and for events:

```python
    _ensure_harness_columns(
        con,
        "harness_events",
        {
            "normalized_type": "VARCHAR",
            "normalized_source": "VARCHAR",
            "content_preview": "VARCHAR",
            "seq": "INTEGER",
        },
    )
```

- [ ] **Step 4: Extend models and registry**

`models.py`: add `mode: str | None = None`, `awaiting: str | None = None`, `last_activity: datetime | None = None` to `HarnessSession`, and `seq: int | None = None` to `HarnessEvent`, wiring each into the class's `from_row` mapping the same way neighboring optional columns are handled (look at how `handoff_mode` / `content_preview` are mapped and mirror it exactly).

`registry.py`:
- `create_session` gains `mode: str = "pty"`; add `mode` to the INSERT column list and parameter list (alongside `handoff_mode`).
- `append_event` gains `seq: int | None = None`; add `seq` to the INSERT columns/params.
- New methods on `HarnessRegistry`:

```python
    def update_session_activity(
        self,
        session_id: str,
        *,
        awaiting: str | None,
        last_activity: datetime | None = None,
    ) -> None:
        stamp = last_activity or _now()
        with self._connect() as con:
            con.execute(
                "UPDATE harness_sessions SET awaiting = ?, last_activity = ? "
                "WHERE session_id = ?",
                [awaiting, stamp, session_id],
            )

    def max_event_seq(self, session_id: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM harness_events "
                "WHERE session_id = ?",
                [session_id],
            ).fetchone()
        return int(row[0] or 0)

    def list_events_after(
        self, session_id: str, after_seq: int
    ) -> list[HarnessEvent]:
        with self._connect() as con:
            rows = _rows(
                con,
                "SELECT * FROM harness_events WHERE session_id = ? "
                "AND seq IS NOT NULL AND seq > ? ORDER BY seq",
                [session_id, after_seq],
            )
        return [HarnessEvent.from_row(row) for row in rows]
```

- [ ] **Step 5: Run the tests, then the full suite**

Run: `uv run python -m pytest tests/ -q -k "structured_session_fields or default_mode or event_seq"` → PASS
Run: `uv run python -m pytest tests/ -q` → PASS (existing callers unaffected: new params have defaults).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/harness/schema.py src/nexus/server/harness/registry.py src/nexus/server/harness/models.py tests/
git commit -m "feat: add structured-session fields (mode, awaiting, last_activity, event seq)"
```

---

### Task 2: Message schema + driver base (structured/driver.py)

**Files:**
- Create: `src/nexus/server/harness/structured/__init__.py`
- Create: `src/nexus/server/harness/structured/driver.py`
- Test: `tests/test_structured_driver.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces (exact names later tasks rely on):
  - `StructuredMessage` frozen dataclass: `type: str`, `role: str`, `text: str`, `payload: dict[str, Any]` (default `{}`), `turn_id: str | None = None`, `event_id: str` (default `f"harness-event-{uuid4()}"`), `ts: str` (default now, ISO-8601 UTC); method `to_payload() -> dict[str, Any]` returning all fields as a JSON-safe dict.
  - `MESSAGE_TYPES` frozenset of the nine vocabulary strings.
  - `EmitFn = Callable[[StructuredMessage], None]`
  - `class ProcessDriver` — shared subprocess plumbing. Constructor `(command: list[str], cwd: str | None, emit: EmitFn)`. Methods: `start()`, `send_line(obj: dict) -> None` (JSON+newline to stdin, thread-safe), `interrupt()` (SIGINT to child), `close()` (terminate child, join threads), `is_alive() -> bool`. Subclasses override `parse_line(line: str) -> list[StructuredMessage]` and may override `on_exit(returncode: int) -> list[StructuredMessage]`.
  - Behavior contract: each stdout line → `parse_line`; a line that raises or is not JSON becomes `StructuredMessage(type="raw", role="system", text=line, payload={"stream": "stdout"})`; on child exit, `on_exit` default emits `status` with `payload={"exited": returncode}` plus, for nonzero returncode, an `error` message whose text is the tail of captured stderr.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the structured driver base: message schema + subprocess pump."""

from __future__ import annotations

import json
import sys
import time

from nexus.server.harness.structured.driver import (
    MESSAGE_TYPES,
    ProcessDriver,
    StructuredMessage,
)


def test_message_defaults_and_payload_roundtrip():
    message = StructuredMessage(type="assistant_output", role="assistant", text="hi")
    assert message.event_id.startswith("harness-event-")
    data = message.to_payload()
    assert data["type"] == "assistant_output"
    assert data["text"] == "hi"
    assert json.dumps(data)  # JSON-safe


def test_vocabulary_is_exact():
    assert MESSAGE_TYPES == {
        "assistant_output", "user_input", "tool_action", "tool_result",
        "approval_prompt", "approval_response", "status", "error", "raw",
    }


class _EchoDriver(ProcessDriver):
    def parse_line(self, line: str):
        obj = json.loads(line)
        return [
            StructuredMessage(
                type="assistant_output", role="assistant", text=obj["text"]
            )
        ]


def _collect_driver(script: str):
    got: list[StructuredMessage] = []
    driver = _EchoDriver([sys.executable, "-c", script], None, got.append)
    driver.start()
    return driver, got


def _wait_for(got, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met; got {[m.type for m in got]}")


def test_pump_parses_and_falls_back_to_raw():
    script = 'print(\'{"text": "one"}\'); print("not json"); import sys; sys.exit(0)'
    driver, got = _collect_driver(script)
    _wait_for(got, lambda g: any(m.type == "status" for m in g))
    types = [m.type for m in got]
    assert "assistant_output" in types
    assert "raw" in types
    raw = next(m for m in got if m.type == "raw")
    assert raw.text == "not json"
    driver.close()


def test_nonzero_exit_emits_error_with_stderr_tail():
    script = 'import sys; print("boom", file=sys.stderr); sys.exit(3)'
    driver, got = _collect_driver(script)
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    error = next(m for m in got if m.type == "error")
    assert "boom" in error.text
    status = next(m for m in got if m.type == "status")
    assert status.payload["exited"] == 3
    driver.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_structured_driver.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.server.harness.structured'`.

- [ ] **Step 3: Implement driver.py**

`structured/__init__.py`:

```python
"""Structured (headless JSON) harness drivers. See the 2026-07-06 design spec."""
```

`structured/driver.py`:

```python
"""Base plumbing for structured harness drivers.

A driver wraps one CLI subprocess running in its machine/JSON mode, parses
its stdout stream into normalized StructuredMessages, and hands each message
to an emit callback. Unparseable output degrades to type="raw" — never
dropped. Subclasses implement parse_line() plus CLI-specific turn/permission
writes via send_line().
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

MESSAGE_TYPES = frozenset(
    {
        "assistant_output", "user_input", "tool_action", "tool_result",
        "approval_prompt", "approval_response", "status", "error", "raw",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StructuredMessage:
    type: str
    role: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    event_id: str = field(default_factory=lambda: f"harness-event-{uuid4()}")
    ts: str = field(default_factory=_now_iso)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "role": self.role,
            "text": self.text,
            "payload": self.payload,
            "turn_id": self.turn_id,
            "ts": self.ts,
        }


EmitFn = Callable[[StructuredMessage], None]

_STDERR_TAIL_LINES = 20


class ProcessDriver:
    """Owns one CLI subprocess; pumps stdout lines through parse_line."""

    def __init__(
        self, command: list[str], cwd: str | None, emit: EmitFn
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.emit = emit
        self._process: subprocess.Popen[str] | None = None
        self._stdin_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        for target in (self._pump_stdout, self._pump_stderr):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def interrupt(self) -> None:
        if self.is_alive():
            assert self._process is not None
            self._process.send_signal(signal.SIGINT)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in self._threads:
            thread.join(timeout=2)

    # -- I/O ---------------------------------------------------------------

    def send_line(self, obj: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("driver process is not running")
        line = json.dumps(obj)
        with self._stdin_lock:
            process.stdin.write(line + "\n")
            process.stdin.flush()

    # -- hooks for subclasses ----------------------------------------------

    def parse_line(self, line: str) -> list[StructuredMessage]:
        raise NotImplementedError

    def on_exit(self, returncode: int) -> list[StructuredMessage]:
        messages = [
            StructuredMessage(
                type="status", role="system", text="process exited",
                payload={"exited": returncode},
            )
        ]
        if returncode != 0:
            tail = "\n".join(self._stderr_tail)
            messages.insert(
                0,
                StructuredMessage(
                    type="error", role="system",
                    text=tail or f"exited with code {returncode}",
                    payload={"returncode": returncode},
                ),
            )
        return messages

    # -- pumps ---------------------------------------------------------------

    def _emit_all(self, messages: list[StructuredMessage]) -> None:
        for message in messages:
            self.emit(message)

    def _pump_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                messages = self.parse_line(line)
            except Exception:  # noqa: BLE001 - protocol drift degrades to raw
                messages = [
                    StructuredMessage(
                        type="raw", role="system", text=line,
                        payload={"stream": "stdout"},
                    )
                ]
            self._emit_all(messages)
        returncode = process.wait()
        self._emit_all(self.on_exit(returncode))

    def _pump_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip("\n"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_structured_driver.py -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/harness/structured/ tests/test_structured_driver.py
git commit -m "feat: add structured message schema and subprocess driver base"
```

---

### Task 3: Claude driver

**Files:**
- Create: `src/nexus/server/harness/structured/claude.py`
- Test: `tests/test_structured_claude.py`

**Interfaces:**
- Consumes: `ProcessDriver`, `StructuredMessage` from Task 2; **Task 0 FINDINGS.md** (authoritative wire shapes — adjust the mappings below to it).
- Produces: `ClaudeDriver(ProcessDriver)` with:
  - `default_command(binary: str | None = None) -> list[str]` (module function) → `[binary or shutil.which("claude") or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]`
  - `send_turn(text: str, turn_id: str) -> None`
  - `answer_permission(request_id: str, decision: str, note: str | None = None) -> None` (`decision` ∈ {"allow", "deny"})

- [ ] **Step 1: Write the failing tests**

Unit tests feed literal NDJSON lines straight through `parse_line` (no subprocess), plus a structural test over the Task 0 golden fixture:

```python
"""Tests for the Claude Code structured driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.server.harness.structured.claude import ClaudeDriver, default_command

FIXTURE = Path("tests/fixtures/structured/claude_basic.ndjson")


def _driver(sink: list) -> ClaudeDriver:
    return ClaudeDriver(["true"], None, sink.append)


def test_default_command_shape():
    command = default_command("/opt/bin/claude")
    assert command[0] == "/opt/bin/claude"
    assert "--output-format" in command and "stream-json" in command


def test_parse_assistant_text_block():
    line = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hello nexus"},
        ]},
    })
    messages = _driver([]).parse_line(line)
    assert [m.type for m in messages] == ["assistant_output"]
    assert messages[0].text == "hello nexus"


def test_parse_tool_use_and_result():
    use = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "ls"}},
        ]},
    })
    result = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
        ]},
    })
    driver = _driver([])
    action = driver.parse_line(use)[0]
    assert action.type == "tool_action" and action.payload["tool"] == "Bash"
    outcome = driver.parse_line(result)[0]
    assert outcome.type == "tool_result"


def test_parse_result_marks_turn_complete():
    line = json.dumps({"type": "result", "subtype": "success",
                       "total_cost_usd": 0.01})
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.payload["turn_complete"] is True
    assert message.payload["awaiting"] == "input"


def test_parse_control_request_becomes_approval_prompt():
    line = json.dumps({
        "type": "control_request", "request_id": "req-1",
        "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                    "input": {"command": "rm -rf /tmp/x"}},
    })
    message = _driver([]).parse_line(line)[0]
    assert message.type == "approval_prompt"
    assert message.payload["request_id"] == "req-1"
    assert message.payload["tool"] == "Bash"


def test_send_turn_and_answer_permission_wire_shapes(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.send_turn("do the thing", turn_id="t1")
    assert sent[0]["type"] == "user"
    assert sent[0]["message"]["content"][0]["text"] == "do the thing"
    driver.answer_permission("req-1", "allow")
    assert sent[1]["type"] == "control_response"
    assert sent[1]["response"]["request_id"] == "req-1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Task 0 fixture not captured")
def test_golden_fixture_parses_without_raw_fallback():
    driver = _driver([])
    types: list[str] = []
    for line in FIXTURE.read_text().splitlines():
        if line.strip():
            types.extend(m.type for m in driver.parse_line(line))
    assert "assistant_output" in types
    assert any(t == "status" for t in types)  # turn completion
    assert "raw" not in types  # every real line must map to a typed message
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_structured_claude.py -q`
Expected: FAIL with `ModuleNotFoundError` for `structured.claude`.

- [ ] **Step 3: Implement claude.py**

Adjust field names to Task 0's FINDINGS.md if they differ from this sketch (the fixture test enforces reality):

```python
"""Claude Code structured driver (bidirectional stream-json)."""

from __future__ import annotations

import shutil
from typing import Any

from nexus.server.harness.structured.driver import ProcessDriver, StructuredMessage

import json


def default_command(binary: str | None = None) -> list[str]:
    return [
        binary or shutil.which("claude") or "claude",
        "-p", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
    ]


class ClaudeDriver(ProcessDriver):
    def parse_line(self, line: str) -> list[StructuredMessage]:
        obj = json.loads(line)
        kind = obj.get("type")
        if kind == "system":
            return [StructuredMessage(
                type="status", role="system",
                text=str(obj.get("subtype") or "system"),
                payload={"native_session_id": obj.get("session_id"), **obj},
            )]
        if kind == "assistant":
            return self._from_content(obj, role="assistant")
        if kind == "user":
            return self._from_content(obj, role="tool")
        if kind == "result":
            return [StructuredMessage(
                type="status", role="system", text="turn complete",
                payload={"turn_complete": True, "awaiting": "input",
                         "result": obj},
            )]
        if kind == "control_request":
            request = obj.get("request") or {}
            return [StructuredMessage(
                type="approval_prompt", role="system",
                text=f"approval needed: {request.get('tool_name')}",
                payload={"request_id": obj.get("request_id"),
                         "tool": request.get("tool_name"),
                         "input": request.get("input")},
            )]
        return [StructuredMessage(
            type="raw", role="system", text=line, payload={"unhandled": kind},
        )]

    def _from_content(self, obj: dict[str, Any], *, role: str):
        messages: list[StructuredMessage] = []
        content = (obj.get("message") or {}).get("content") or []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                messages.append(StructuredMessage(
                    type="assistant_output", role="assistant",
                    text=block.get("text") or "",
                ))
            elif block_type == "tool_use":
                messages.append(StructuredMessage(
                    type="tool_action", role="assistant",
                    text=f"{block.get('name')}",
                    payload={"tool": block.get("name"),
                             "tool_use_id": block.get("id"),
                             "input": block.get("input")},
                ))
            elif block_type == "tool_result":
                messages.append(StructuredMessage(
                    type="tool_result", role="tool",
                    text=_result_text(block),
                    payload={"tool_use_id": block.get("tool_use_id")},
                ))
        return messages or [StructuredMessage(
            type="raw", role=role, text=json.dumps(obj),
            payload={"unhandled": "empty-content"},
        )]

    def send_turn(self, text: str, turn_id: str) -> None:
        self.send_line({
            "type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]},
        })

    def answer_permission(
        self, request_id: str, decision: str, note: str | None = None
    ) -> None:
        behavior = "allow" if decision == "allow" else "deny"
        self.send_line({
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id,
                         "response": {"behavior": behavior,
                                      "message": note or ""}},
        })


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content
            if isinstance(item, dict)
        )
    return ""
```

- [ ] **Step 4: Run tests (including the golden fixture) to verify they pass**

Run: `uv run python -m pytest tests/test_structured_claude.py -q` → PASS. If the fixture test fails, the fixture is right and the parser is wrong — fix the parser.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/harness/structured/claude.py tests/test_structured_claude.py
git commit -m "feat: add Claude Code structured driver"
```

---

### Task 4: Codex driver

**Files:**
- Create: `src/nexus/server/harness/structured/codex.py`
- Test: `tests/test_structured_codex.py`

**Interfaces:**
- Consumes: Task 2 base; **Task 0 FINDINGS.md** (authoritative — including whether `codex proto` exists or per-turn `codex exec --json` respawn is required; implement whichever Task 0 found, keeping the public surface below).
- Produces: `CodexDriver(ProcessDriver)` with `default_command(binary=None) -> list[str]` (→ `[..., "proto"]` or the Task 0 alternative), `send_turn(text, turn_id)`, `answer_permission(request_id, decision, note=None)`.

- [ ] **Step 1: Write the failing tests**

Same pattern as Task 3, adapted to the codex event envelope (`{"id": ..., "msg": {"type": ...}}` — adjust to FINDINGS.md):

```python
"""Tests for the Codex structured driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.server.harness.structured.codex import CodexDriver, default_command

FIXTURE = Path("tests/fixtures/structured/codex_basic.ndjson")


def _driver(sink: list) -> CodexDriver:
    return CodexDriver(["true"], None, sink.append)


def test_parse_agent_message():
    line = json.dumps({"id": "e1", "msg": {"type": "agent_message",
                                           "message": "hello nexus"}})
    message = _driver([]).parse_line(line)[0]
    assert message.type == "assistant_output"
    assert message.text == "hello nexus"


def test_parse_exec_approval_request():
    line = json.dumps({"id": "e2", "msg": {
        "type": "exec_approval_request",
        "command": ["rm", "-rf", "/tmp/x"], "cwd": "/tmp",
    }})
    message = _driver([]).parse_line(line)[0]
    assert message.type == "approval_prompt"
    assert message.payload["request_id"] == "e2"


def test_parse_task_complete_marks_awaiting_input():
    line = json.dumps({"id": "e3", "msg": {"type": "task_complete"}})
    message = _driver([]).parse_line(line)[0]
    assert message.type == "status"
    assert message.payload["awaiting"] == "input"


def test_send_turn_and_approval_wire_shapes(monkeypatch):
    sent: list[dict] = []
    driver = _driver([])
    monkeypatch.setattr(driver, "send_line", sent.append)
    driver.send_turn("do it", turn_id="t1")
    assert sent[0]["op"]["type"] == "user_input"
    driver.answer_permission("e2", "allow")
    assert sent[1]["op"]["type"] in {"exec_approval", "patch_approval"}
    assert sent[1]["op"]["decision"] in {"approved", "approve", "allow"}


@pytest.mark.skipif(not FIXTURE.exists(), reason="Task 0 fixture not captured")
def test_golden_fixture_parses_without_raw_fallback():
    driver = _driver([])
    types: list[str] = []
    for line in FIXTURE.read_text().splitlines():
        if line.strip():
            types.extend(m.type for m in driver.parse_line(line))
    assert "assistant_output" in types
    assert "raw" not in types
```

- [ ] **Step 2: Run to verify failure** → `ModuleNotFoundError` for `structured.codex`.

- [ ] **Step 3: Implement codex.py**

Mirror Task 3's structure. Mapping table (adjust names to FINDINGS.md):
`session_configured` → `status` (payload carries `native_session_id`); `agent_message` → `assistant_output` (ignore `agent_message_delta`); `exec_command_begin` / `patch_apply_begin` → `tool_action`; `exec_command_end` / `patch_apply_end` → `tool_result`; `exec_approval_request` / `apply_patch_approval_request` → `approval_prompt` with `request_id` = the event envelope `id`; `task_complete` → `status` with `{"turn_complete": True, "awaiting": "input"}`; `error` → `error`; anything else → `raw` with `payload={"unhandled": <type>}`. `send_turn` writes `{"id": turn_id, "op": {"type": "user_input", "items": [{"type": "text", "text": text}]}}`. `answer_permission` writes the approval op shape Task 0 captured (decision "approved"/"denied"). Keep the file under ~120 lines; one `parse_line` dispatching on `msg["type"]` via a dict of small handler functions is fine.

- [ ] **Step 4: Run tests** → PASS (fixture test included).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/harness/structured/codex.py tests/test_structured_codex.py
git commit -m "feat: add Codex structured driver"
```

---

### Task 5: Gemini driver (per-turn respawn)

**Files:**
- Create: `src/nexus/server/harness/structured/gemini.py`
- Test: `tests/test_structured_gemini.py`

**Interfaces:**
- Consumes: `StructuredMessage`, `EmitFn` from Task 2 (NOT `ProcessDriver` — gemini has no long-lived bidirectional mode per the spec's worst-case assumption; confirm/adjust per FINDINGS.md).
- Produces: `GeminiDriver` with the same public surface as the others so the manager treats all three uniformly: `__init__(command: list[str], cwd, emit)` (command = base argv, e.g. `["gemini"]`), `start()` (no-op, emits a `status` "ready" message with `awaiting: "input"`), `send_turn(text, turn_id)` (spawns `command + ["-p", text, "-o", "json"]` plus the Task 0 resume flag when a prior turn recorded a session tag; runs in a worker thread; on completion emits `assistant_output` from the response and a turn-complete `status`), `answer_permission(...)` (raises `RuntimeError("gemini driver has no interactive approvals")` — headless gemini auto-resolves tools per Task 0 findings), `interrupt()` (kills the in-flight turn subprocess if any), `close()`, `is_alive() -> bool` (True until `close()`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the Gemini structured driver (per-turn respawn)."""

from __future__ import annotations

import sys
import time

from nexus.server.harness.structured.gemini import GeminiDriver

FAKE_GEMINI = (
    'import json,sys; args=sys.argv[1:]; '
    'idx=args.index("-p"); prompt=args[idx+1]; '
    'print(json.dumps({"response": "echo: " + prompt, "stats": {}}))'
)


def _driver(sink):
    return GeminiDriver([sys.executable, "-c", FAKE_GEMINI], None, sink.append)


def _wait_for(got, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError([m.type for m in got])


def test_start_reports_ready():
    got = []
    driver = _driver(got)
    driver.start()
    assert got[0].type == "status"
    assert got[0].payload["awaiting"] == "input"
    driver.close()


def test_turn_roundtrip_emits_output_then_complete():
    got = []
    driver = _driver(got)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(
        m.type == "status" and m.payload.get("turn_complete") for m in g
    ))
    output = next(m for m in got if m.type == "assistant_output")
    assert output.text == "echo: hello"
    assert output.turn_id == "t1"
    driver.close()


def test_failed_turn_emits_error():
    got = []
    driver = GeminiDriver([sys.executable, "-c", "import sys; sys.exit(2)"],
                          None, got.append)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    driver.close()
```

- [ ] **Step 2: Run to verify failure** → `ModuleNotFoundError` for `structured.gemini`.

- [ ] **Step 3: Implement gemini.py**

~90 lines. Store the callback as `self.emit` (Task 6's manager calls `driver.emit(...)` directly on every driver type, so the attribute name is part of the driver contract). Worker thread per turn runs `subprocess.run(argv, capture_output=True, text=True, cwd=self.cwd)` where `argv = list(self.command) + ["-p", text, "-o", "json"] + resume_args`; `resume_args` come from the Task 0-documented resume mechanism when `self._session_tag` is set (record the tag from the first turn's output if the CLI provides one; otherwise leave resume off and note it in the module docstring). Parse stdout as JSON → `assistant_output` (text = `response` field, `turn_id` threaded through) + `status` `{"turn_complete": True, "awaiting": "input"}`; JSON parse failure → `raw` + same status; nonzero returncode → `error` with stderr tail + `status` `{"exited": rc}`. Only one in-flight turn: `send_turn` while a turn is running raises `RuntimeError("turn already in flight")`. Also provide module-level `default_command(binary=None) -> list[str]` returning `[binary or shutil.which("gemini") or "gemini"]`.

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/harness/structured/gemini.py tests/test_structured_gemini.py
git commit -m "feat: add Gemini structured driver (per-turn respawn)"
```

---

### Task 6: Daemon — structured session manager and routes

**Files:**
- Create: `src/nexus/server/harness/structured/manager.py`
- Modify: `src/nexus/server/harness/daemon.py` (`HarnessDaemonState`, `_create_session`, new route branches in `do_POST`, session listing, terminate path)
- Test: `tests/test_harness_daemon.py` (extend)

**Interfaces:**
- Consumes: Tasks 1–5 (registry methods, three drivers), existing daemon fixtures/route dispatch, existing `_gate`.
- Produces:
  - `StructuredSessionManager` with: `start(session_id, *, harness, cwd, command, registry, on_message: Callable[[str, dict], None], finalize: Callable[[str, int], None]) -> None` (builds the driver — `claude-code` → `ClaudeDriver`, `codex` → `CodexDriver`, `gemini` → `GeminiDriver`, else `ValueError`; `command` overrides `default_command()`; wires the pump), `send_turn(session_id, text) -> str` (returns `turn_id`; also appends the `user_input` message itself), `answer_permission(session_id, request_id, decision, note) -> None`, `interrupt(session_id)`, `close(session_id)`, `has(session_id) -> bool`, `awaiting(session_id) -> str | None`.
  - Pump behavior (inside manager): every driver message gets `seq` = per-session counter (initialized from `registry.max_event_seq`), is appended via `registry.append_event(session_id=..., event_type=message.type, payload=message.to_payload() | {"seq": seq}, seq=seq, harness=..., normalized_source="structured")`, then `registry.update_session_activity(session_id, awaiting=<derived>)` where derived = `"approval"` if type is `approval_prompt`, `"input"` if a `status` payload has `awaiting == "input"`, unchanged otherwise (pass the current value; track it in the manager entry), and finally `on_message(session_id, full_event_dict)` for Task 7's pusher. A `status` with `exited` payload finalizes: status → `completed` (rc==0) / `errored` (rc!=0) via the daemon's existing `_finalize_session` semantics.
  - Daemon HTTP surface: `POST /sessions` accepts `mode: "structured"` + optional `prompt`; `POST /sessions/<id>/turns` `{"text": ...}` → `202 {"turn_id": ...}` (409 if `awaiting == "approval"` — answer the approval first); `POST /sessions/<id>/permission` `{"request_id", "decision", "note"?}` → `200 {"ok": true}`; session listings include `mode`, `awaiting`, `last_activity`.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_harness_daemon.py`, reusing its existing daemon-server fixture pattern (the same one the auth tests extended with `api_token`). Use a fake structured CLI so no real agent runs:

```python
FAKE_STRUCTURED_CLI = [
    sys.executable, "-c",
    (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    obj=json.loads(line)\n"
        "    if obj.get('type')=='control_response':\n"
        "        print(json.dumps({'type':'assistant','message':{'role':'assistant',"
        "'content':[{'type':'text','text':'approved and done'}]}}),flush=True)\n"
        "        print(json.dumps({'type':'result','subtype':'success'}),flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'type':'control_request','request_id':'req-1',"
        "'request':{'subtype':'can_use_tool','tool_name':'Bash',"
        "'input':{'command':'ls'}}}),flush=True)\n"
    ),
]


def test_structured_session_full_lifecycle(daemon_server):  # adapt fixture name
    server, port = daemon_server
    status, body = _post_json(port, "/sessions", {
        "harness": "claude-code", "mode": "structured",
        "prompt": "list files", "command": FAKE_STRUCTURED_CLI,
    })
    assert status == 201
    sid = body["session_id"]
    _wait_until(lambda: _get_session(port, sid)["awaiting"] == "approval")
    status, _ = _post_json(port, f"/sessions/{sid}/turns", {"text": "more"})
    assert status == 409  # approval pending blocks new turns
    status, _ = _post_json(port, f"/sessions/{sid}/permission",
                           {"request_id": "req-1", "decision": "allow"})
    assert status == 200
    _wait_until(lambda: _get_session(port, sid)["awaiting"] == "input")
    listing = _get_session(port, sid)
    assert listing["mode"] == "structured"
    assert listing["last_activity"] is not None


def test_structured_turn_appends_user_input_and_seq_is_monotonic(daemon_server):
    ...  # create as above; after awaiting=="input" send a turn; read the
    # daemon registry (the fixture exposes state.registry) and assert:
    # one user_input event exists with the turn text; seq values across all
    # structured events are strictly increasing 1..N with no gaps.


def test_structured_unknown_harness_rejected(daemon_server):
    ...  # POST /sessions {"harness": "shell", "mode": "structured"} -> 400
```

Write the `...` bodies fully, reusing the file's existing `_post_json`-style helpers (or adding small module-level ones next to them: `_post_json(port, path, payload)` sending `Authorization` if the fixture sets a token, `_get_session(port, sid)` = GET `/sessions` and pick by id, `_wait_until(predicate, timeout=15)` polling at 0.1s).

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_harness_daemon.py -q -k structured`
Expected: FAIL — 400 `unknown harness preset` / missing fields (no `mode` handling yet).

- [ ] **Step 3: Implement manager.py**

```python
"""Owns live structured-session drivers for one harnessd instance."""

from __future__ import annotations

import threading
from typing import Any, Callable
from uuid import uuid4

from nexus.server.harness.registry import HarnessRegistry
from nexus.server.harness.structured import claude, codex, gemini
from nexus.server.harness.structured.driver import StructuredMessage

_FACTORIES = {
    "claude-code": (claude.ClaudeDriver, claude.default_command),
    "codex": (codex.CodexDriver, codex.default_command),
    "gemini": (gemini.GeminiDriver, gemini.default_command),
}


class _Entry:
    def __init__(self, driver: Any, harness: str) -> None:
        self.driver = driver
        self.harness = harness
        self.seq = 0
        self.awaiting: str | None = None
        self.lock = threading.Lock()


class StructuredSessionManager:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def has(self, session_id: str) -> bool:
        return session_id in self._entries

    def awaiting(self, session_id: str) -> str | None:
        entry = self._entries.get(session_id)
        return entry.awaiting if entry else None

    def start(
        self,
        session_id: str,
        *,
        harness: str,
        cwd: str | None,
        command: list[str] | None,
        registry: HarnessRegistry,
        on_message: Callable[[str, dict[str, Any]], None],
        finalize: Callable[[str, int], None],
    ) -> None:
        if harness not in _FACTORIES:
            raise ValueError(f"harness has no structured driver: {harness}")
        driver_cls, default_command = _FACTORIES[harness]
        entry = _Entry(None, harness)
        entry.seq = registry.max_event_seq(session_id)

        def emit(message: StructuredMessage) -> None:
            with entry.lock:
                entry.seq += 1
                seq = entry.seq
                payload = message.payload or {}
                if message.type == "approval_prompt":
                    entry.awaiting = "approval"
                elif message.type == "approval_response":
                    entry.awaiting = None
                elif message.type == "status" and payload.get("awaiting") == "input":
                    entry.awaiting = "input"
                elif message.type == "user_input":
                    entry.awaiting = None
                awaiting = entry.awaiting
            event_payload = message.to_payload()
            event_payload["seq"] = seq
            event_payload["session_id"] = session_id
            registry.append_event(
                session_id=session_id,
                event_type=message.type,
                payload=event_payload,
                seq=seq,
                harness=harness,
                normalized_source="structured",
            )
            registry.update_session_activity(session_id, awaiting=awaiting)
            on_message(session_id, event_payload)
            if message.type == "status" and "exited" in payload:
                finalize(session_id, int(payload["exited"]))

        entry.driver = driver_cls(command or default_command(), cwd, emit)
        self._entries[session_id] = entry
        entry.driver.start()

    def send_turn(self, session_id: str, text: str) -> str:
        entry = self._entries[session_id]
        if entry.awaiting == "approval":
            raise PermissionError("approval pending; answer it first")
        turn_id = f"turn-{uuid4()}"
        # record the user's side of the conversation before dispatching
        entry.driver.emit(  # type: ignore[attr-defined]
            StructuredMessage(
                type="user_input", role="user", text=text, turn_id=turn_id
            )
        )
        entry.driver.send_turn(text, turn_id)
        return turn_id

    def answer_permission(
        self, session_id: str, request_id: str, decision: str, note: str | None
    ) -> None:
        entry = self._entries[session_id]
        entry.driver.emit(  # type: ignore[attr-defined]
            StructuredMessage(
                type="approval_response", role="user", text=decision,
                payload={"request_id": request_id, "decision": decision,
                         "note": note},
            )
        )
        entry.driver.answer_permission(request_id, decision, note)

    def interrupt(self, session_id: str) -> None:
        self._entries[session_id].driver.interrupt()

    def close(self, session_id: str) -> None:
        entry = self._entries.pop(session_id, None)
        if entry is not None:
            entry.driver.close()
```

(Note: `entry.driver.emit` is the emit callback stored on the driver by `ProcessDriver.__init__` / `GeminiDriver.__init__` — same pump path as driver-originated messages, so seq/awaiting/push stay consistent.)

- [ ] **Step 4: Wire the daemon**

In `daemon.py`:
1. `HarnessDaemonState` gains `structured: StructuredSessionManager` (construct alongside `pty` in `run_harnessd` and in the test fixture path — find where `state.pty` is built and mirror it).
2. In `_create_session`, after the existing `cwd` validation, branch on `mode = str(body.get("mode") or "pty")`. For `"structured"`: skip the preset requirement (drivers own their commands); create the registry session with `mode="structured"`, `status="running"`, `command=_command_label(command or default)`; call `state.structured.start(...)` with `on_message=self.server.state.push_event` (a no-op attribute until Task 7 — set `state.push_event = lambda sid, evt: None` default) and `finalize=` a small closure calling the existing `_finalize_session` machinery (`"completed"` if rc == 0 else `"errored"`); if `body.get("prompt")` is a non-empty string, call `state.structured.send_turn(session_id, prompt)` after start; respond `201` with the same session JSON shape as PTY plus `"mode": "structured"`. On driver `ValueError` → 400.
3. New `do_POST` branches (following the existing terminate/continue path-suffix style):
   - `/sessions/<id>/turns`: 404 if not `state.structured.has(id)`; body `text` required (400 if missing/empty); `PermissionError` → 409 `{"error": "approval pending"}`; else `202 {"turn_id": ...}`.
   - `/sessions/<id>/permission`: 404 if unknown; `request_id` + `decision` required, `decision` must be `allow`/`deny` (400 otherwise); `200 {"ok": true}`.
   - `/sessions/<id>/interrupt`: 404 if not a structured session; calls `state.structured.interrupt(id)`; `200 {"ok": true}` (maps to the spec's "interrupt maps to each CLI's native cancel"; drivers emit a `status` message from the resulting stream activity).
4. Session listing (`_list_sessions` / the GET `/sessions` handler): include `mode`, `awaiting`, `last_activity` (ISO string or None) from the registry rows — the fields exist after Task 1; ensure the listing code passes them through rather than whitelisting them away.
5. Terminate path: if `state.structured.has(id)`, call `state.structured.close(id)` in addition to the existing finalize logic (PTY kill path untouched).

- [ ] **Step 5: Run the tests, then the full suite**

Run: `uv run python -m pytest tests/test_harness_daemon.py -q -k structured` → PASS
Run: `uv run python -m pytest tests/ -q` → PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/harness/structured/manager.py src/nexus/server/harness/daemon.py tests/test_harness_daemon.py
git commit -m "feat: run structured harness sessions in harnessd (turns, approvals)"
```

---

### Task 7: Event push to central + central ingest endpoint

**Files:**
- Create: `src/nexus/server/harness/structured/pusher.py`
- Modify: `src/nexus/server/harness/daemon.py` (wire pusher into state + `run_harnessd`)
- Modify: `src/nexus/server/web/app.py` (`POST /harness/events` ingest route)
- Test: `tests/test_structured_pusher.py`, `tests/test_metrics.py` (ingest route tests)

**Interfaces:**
- Consumes: Task 6's `on_message(session_id, event_payload)` hook; central `_gate` + registry pattern from `_mirror_harness_event_frame` (`app.py`); daemon `state.api_token` (Task 6 of the auth plan).
- Produces:
  - `EventPusher` with `__init__(central_url: str, token: str, *, batch_interval: float = 2.0)`, `push(session_id: str, event: dict) -> None` (enqueue; flush immediately when `event["type"] == "status"` and `event["payload"].get("turn_complete")` or `"exited" in event["payload"]`), `start()` / `stop()`; POSTs `{"events": [...]}` (each event dict already contains `session_id`, `event_id`, `seq`, `type`, …) to `central_url + "/harness/events"` with `Authorization: Bearer <token>`; on HTTP/network failure re-queues the batch at the front and backs off 5 s; queue capped at 5000 events (drop-oldest with a stderr log line — never block the pump).
  - Central route `POST /harness/events` (auth-gated like everything else): idempotent by `event_id` (skip if `get_event(event_id)` exists), inserts via `append_event(session_id=..., event_type=..., payload=event, seq=event.get("seq"), normalized_source="structured", event_id=..., created_at=parsed ts)`; responds `200 {"ingested": <n_new>}`; malformed body → 400.

- [ ] **Step 1: Write the failing tests**

`tests/test_structured_pusher.py` — stand up a tiny `ThreadingHTTPServer` capturing POST bodies + auth header (mirror the `_FakeHarnessHandler` pattern in `tests/test_metrics.py`); assert: events batch into one POST; bearer header present; turn-complete event triggers a flush without waiting the full interval (send one ordinary + one turn-complete event, assert delivery < 1.5 s); a 500-then-200 server sequence redelivers the same events exactly once each (idempotency is central's job — the pusher may redeliver, must not drop).

`tests/test_metrics.py` — add:

```python
def test_harness_events_ingest_idempotent(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(host="127.0.0.1", port=0,
                                  collector=collector, auth=_TEST_AUTH)
    try:
        port = server.server_address[1]
        event = {"event_id": "harness-event-x1", "session_id": "harness-s1",
                 "seq": 1, "type": "assistant_output", "role": "assistant",
                 "text": "hi", "payload": {}, "turn_id": None,
                 "ts": "2026-07-06T00:00:00+00:00"}
        for expected_new in (1, 0):  # second POST is a replay
            status, body = _json_request(
                f"http://127.0.0.1:{port}/harness/events",
                payload={"events": [event]},
            )
            assert status == 200
            assert body["ingested"] == expected_new
    finally:
        server.shutdown()


def test_harness_events_ingest_requires_auth(tmp_path):
    ...  # same setup; POST without Authorization -> 401
```

Write the `...` body fully using the existing no-auth request pattern from the Task 4 auth tests.

- [ ] **Step 2: Run to verify failure** → pusher module missing; ingest route 404.

- [ ] **Step 3: Implement pusher.py**

~80 lines: `queue.SimpleQueue` fed by `push()`; worker thread loop: drain everything currently queued (plus block up to `batch_interval` for the first item), POST once via `urllib.request.Request(url, data=json.dumps({"events": batch}).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})` with a 10 s timeout; success → loop; failure → hold the batch, `time.sleep(5)`, retry (max 3 attempts, then drop with one stderr line including only counts — never event text, never the token). A `threading.Event` set by turn-boundary pushes wakes the worker early. `stop()` flushes once more synchronously.

- [ ] **Step 4: Wire daemon + central**

Daemon (`run_harnessd`): if `state.api_token` and a central URL is configured (same variable `_post_central_json` uses), build `EventPusher(central_url, state.api_token)`, `start()` it, and set `state.push_event = pusher.push`; otherwise leave the no-op (structured sessions still work locally). Central (`app.py` `do_POST`): add the `/harness/events` branch after `/auth/login`, before the proxy branches; parse body, validate `events` is a list of dicts each having `event_id`/`session_id`/`type` (400 otherwise); construct the registry exactly the way `_mirror_harness_event_frame` does; loop with the idempotency check; respond with the ingested count.

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `uv run python -m pytest tests/test_structured_pusher.py tests/test_metrics.py -q` → PASS
Run: `uv run python -m pytest tests/ -q` → PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/harness/structured/pusher.py src/nexus/server/harness/daemon.py src/nexus/server/web/app.py tests/
git commit -m "feat: push structured session events to central continuously"
```

---

### Task 8: Central read APIs — messages REST + session WebSocket stream

**Files:**
- Modify: `src/nexus/server/web/app.py` (two new `do_GET` branches)
- Modify: `README.md` (API table/section: three new endpoints)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: Task 1 `list_events_after` / `max_event_seq`; Task 7 ingested events; existing WS helpers in `app.py` (`accept_key`, `send_frame`, `OPCODE_TEXT`, `OPCODE_CLOSE`) already imported for the terminal proxy.
- Produces:
  - `GET /harness/sessions/<id>/messages?after_seq=N` → `200 {"messages": [<event payload dicts, ordered by seq>], "max_seq": M}` (`after_seq` defaults 0; non-integer → 400).
  - `GET /harness/sessions/<id>/stream` with WebSocket upgrade headers → 101, then each new event (seq order) as one JSON text frame; poll interval 1 s; server closes cleanly on client close frame or socket error. Auth: covered by `_gate` exactly like the terminal WS (cookie rides the upgrade).

- [ ] **Step 1: Write the failing tests**

```python
def _ingest_events(port: int, events: list[dict]) -> None:
    status, _ = _json_request(f"http://127.0.0.1:{port}/harness/events",
                              payload={"events": events})
    assert status == 200


def _event(seq: int, text: str) -> dict:
    return {"event_id": f"harness-event-m{seq}", "session_id": "harness-s2",
            "seq": seq, "type": "assistant_output", "role": "assistant",
            "text": text, "payload": {}, "turn_id": None,
            "ts": "2026-07-06T00:00:00+00:00"}


def test_messages_endpoint_orders_and_filters_by_seq(tmp_path):
    collector = _make_collector(tmp_path)
    server = start_metrics_server(host="127.0.0.1", port=0,
                                  collector=collector, auth=_TEST_AUTH)
    try:
        port = server.server_address[1]
        _ingest_events(port, [_event(2, "b"), _event(1, "a"), _event(3, "c")])
        with _authed_get(
            f"http://127.0.0.1:{port}/harness/sessions/harness-s2/messages"
            "?after_seq=1"
        ) as response:
            body = json.loads(response.read())
        assert [m["text"] for m in body["messages"]] == ["b", "c"]
        assert body["max_seq"] == 3
    finally:
        server.shutdown()


def test_session_stream_ws_delivers_new_events(tmp_path):
    ...  # start server with _TEST_AUTH; ingest _event(1, "a"); open a raw
    # socket and client_handshake(path="/harness/sessions/harness-s2/stream",
    # headers=_AUTH_HEADERS) (helper from the auth work); recv one text frame,
    # assert json text == "a"; ingest _event(2, "b"); recv again -> "b";
    # close socket; server must not raise (assert a follow-up REST call works).
```

Write the `...` body fully using `client_handshake`/`recv_frame` from `nexus.server.harness.websocket` the way `tests/test_harness_websocket.py` does.

- [ ] **Step 2: Run to verify failure** → 404 on both routes.

- [ ] **Step 3: Implement the two branches in app.py**

In `do_GET`, alongside the existing `/harness/sessions/` branches (order: `/stream` and `/messages` suffix checks BEFORE the bare `/harness/sessions/<id>` branch, mirroring how `/terminal` is matched first):

```python
        if path.startswith("/harness/sessions/") and path.endswith("/messages"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/messages")
            ).strip("/")
            params = parse_qs(parsed.query)
            raw_after = (params.get("after_seq") or ["0"])[0]
            if not raw_after.lstrip("-").isdigit():
                self._send(400, "application/json",
                           '{"error": "after_seq must be an integer"}\n')
                return
            registry = self._harness_registry()  # same construction as the mirror
            events = registry.list_events_after(session_id, int(raw_after))
            body = json.dumps({
                "messages": [json.loads(e.payload_json or "{}") for e in events],
                "max_seq": registry.max_event_seq(session_id),
            })
            self._send(200, "application/json", body + "\n")
            return
```

(If `HarnessEvent` exposes a parsed `payload` attribute instead of `payload_json`, use that — match whatever `_mirror_harness_event_frame` reads/writes.) Extract the registry construction used by `_mirror_harness_event_frame` into a small `self._harness_registry()` helper and reuse it in all three places (mirror, ingest, these reads).

The stream branch (before `/messages`):

```python
        if path.startswith("/harness/sessions/") and path.endswith("/stream"):
            session_id = unquote(
                path.removeprefix("/harness/sessions/").removesuffix("/stream")
            ).strip("/")
            if (self.headers.get("Upgrade") or "").lower() != "websocket":
                self._send(400, "application/json",
                           '{"error": "websocket upgrade required"}\n')
                return
            self._stream_session_messages(session_id)
            return
```

with:

```python
    def _stream_session_messages(self, session_id: str) -> None:
        key = self.headers.get("Sec-WebSocket-Key") or ""
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        sock = self.connection
        sock.settimeout(0.2)
        registry = self._harness_registry()
        last_seq = 0
        try:
            while True:
                for event in registry.list_events_after(session_id, last_seq):
                    last_seq = event.seq or last_seq
                    send_frame(sock, OPCODE_TEXT,
                               (event.payload_json or "{}").encode("utf-8"))
                try:
                    opcode, _payload = recv_frame(sock)
                except socket.timeout:
                    time.sleep(0.8)
                    continue
                if opcode == OPCODE_CLOSE:
                    break
        except OSError:
            pass
```

(Import `time` if not present; `recv_frame`'s exact signature/behavior on timeout — match how `_proxy_terminal_websocket` reads the client side and reuse its idiom if it differs from this sketch.)

- [ ] **Step 4: Update README**

Add three rows/bullets to the API documentation section: `POST /harness/events` (daemon-internal ingest), `GET /harness/sessions/<id>/messages?after_seq=N`, `WS /harness/sessions/<id>/stream` — one line each, noting all require auth.

- [ ] **Step 5: Run tests, full suite, black**

Run: `uv run python -m pytest tests/test_metrics.py -q` → PASS
Run: `uv run python -m pytest tests/ -q` → PASS
Run: `uv run python -m black --check src/nexus/server/web/ src/nexus/server/harness/structured/` → clean

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/web/app.py README.md tests/test_metrics.py
git commit -m "feat: serve structured session messages over REST and WebSocket"
```

---

### Task 9: End-to-end test + final verification

**Files:**
- Test: `tests/test_structured_e2e.py`
- Modify: `.superpowers/sdd/progress.md` bookkeeping only (no production code expected; fix anything the E2E flushes out)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the E2E test**

One test that wires daemon → pusher → central with real HTTP servers and the fake structured CLI from Task 6:

```python
def test_structured_session_events_reach_central(tmp_path):
    # 1. central: collector + start_metrics_server(auth=_TEST_AUTH)
    # 2. daemon: existing daemon fixture with api_token="test-token",
    #    plus EventPusher(central_url=f"http://127.0.0.1:{central_port}",
    #    token="test-token", batch_interval=0.2) wired as state.push_event
    # 3. POST /sessions (mode=structured, FAKE_STRUCTURED_CLI, prompt)
    # 4. answer the approval via POST /sessions/<id>/permission
    # 5. poll central GET /harness/sessions/<id>/messages until it contains,
    #    in seq order: user_input, approval_prompt, approval_response,
    #    assistant_output("approved and done"), status(turn_complete)
    # 6. assert seq values strictly increasing with no gaps
    # 7. assert daemon listing shows awaiting == "input"
```

Write it fully — every step above becomes real code reusing helpers already present in `tests/test_metrics.py` and `tests/test_harness_daemon.py` (import or duplicate the small ones rather than reaching across test modules if imports are awkward).

- [ ] **Step 2: Run it**

Run: `uv run python -m pytest tests/test_structured_e2e.py -q` → PASS (fix whatever it exposes; that is this task's real work).

- [ ] **Step 3: Full suite + lint**

Run: `uv run python -m pytest tests/ -q && uv run python -m black --check src/nexus/server/harness/structured/ src/nexus/server/web/app.py`
Expected: PASS / clean.

- [ ] **Step 4: Manual real-CLI smoke (requires API credentials; document, don't automate)**

From the repo root on the Mac Mini: start a local daemon + central pair per Task 8 of the auth plan's E2E pattern, create one real `claude-code` structured session with a trivial prompt, watch `GET .../messages` populate. Record the outcome (works / driver fix needed) in the task report.

- [ ] **Step 5: Commit**

```bash
git add tests/test_structured_e2e.py
git commit -m "test: end-to-end structured session flow from daemon to central"
```

---

## Out of scope (this plan)

Per the spec: the Swift app, context-plane features (briefs/handoff/MCP), web-UI adoption of the message stream, PTY event-push, ACP adoption, OTLP changes.
