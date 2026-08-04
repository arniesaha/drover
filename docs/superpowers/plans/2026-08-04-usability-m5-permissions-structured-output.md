# Usability M5: Permission Posture + Structured-Output UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Structured Claude sessions run with permissions bypassed by default (no more silent tool failures); Codex/Gemini emit properly-typed thinking/tool events; the iOS chat collapses intermediate steps into paired step cards; scroll-to-bottom no longer gets stuck.

**Architecture:** Server-side normalization — the three drivers in `src/drover/server/harness/structured/` emit the existing shared event vocabulary (`assistant_output`+`thinking`, `tool_action`, `tool_result` with `tool`/`tool_use_id` payload keys); the iOS app stays harness-agnostic and pairs action/result into one collapsed card in `TranscriptItem`. A new `permission_mode` session field (default `auto`) drives a `--permission-mode bypassPermissions` flag on the Claude spawn command.

**Tech Stack:** Python 3.12 (stdlib-only server, pytest via `uv run pytest`), SwiftUI/iOS 18 (NexusKit SPM package with Swift Testing `@Test`, app in `apps/drover`).

**Spec:** `docs/superpowers/specs/2026-08-03-usability-permissions-structured-output-design.md`

## Global Constraints

- Python server code is stdlib-only; tests run with `uv run pytest tests/<file> -x -q` from the repo root `/Volumes/M2 1/drover` (note the space — always quote the path).
- NexusKit tests: `cd "apps/drover/NexusKit" && swift test`. MockNetworkTests-style suites that share URLProtocol state must stay `.serialized` (existing rule).
- Drivers must NEVER drop events: unknown JSON event kinds degrade to `type="status"`, unparseable lines to `type="raw"` (existing `ProcessDriver` discipline).
- Wire vocabulary is frozen: only the 9 existing message types (`driver.py:22-34`). No new `MessageType` cases on either side.
- Old recorded sessions (no `tool`/`tool_use_id` payload keys) must still render — every iOS change needs a graceful fallback.
- Commit after every task; commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Verified wire facts (live probes, 2026-08-04, this Mac)

These captures ground Tasks 1, 3, 4. Do not re-derive from docs.

**gemini 0.46.0** `gemini -p <text> -o stream-json --approval-mode yolo --skip-trust` emits NDJSON on stdout, one JSON object per line:

```json
{"type":"init","timestamp":"2026-08-04T11:43:19.742Z","session_id":"a05c8061-8f5b-40f6-aba5-25c971c50136","model":"auto"}
{"type":"message","timestamp":"2026-08-04T11:43:19.742Z","role":"user","content":"Say exactly: probe-ok, then list files in the current directory"}
{"type":"tool_use","timestamp":"2026-08-04T11:43:36.125Z","tool_name":"list_directory","tool_id":"list_directory__859y31fx","parameters":{"dir_path":"."}}
{"type":"tool_result","timestamp":"2026-08-04T11:43:36.249Z","tool_id":"list_directory__859y31fx","status":"success"}
{"type":"message","timestamp":"2026-08-04T11:43:45.086Z","role":"assistant","content":"probe-ok\n\nHere are the files in the current directory (`/private/tmp","delta":true}
{"type":"message","timestamp":"2026-08-04T11:43:45.087Z","role":"assistant","content":"`):\n\n* **Directories:** ...","delta":true}
{"type":"result","timestamp":"2026-08-04T11:43:45.100Z","status":"success","stats":{"total_tokens":46783,"input_tokens":44974,"output_tokens":561,"cached":8160,"input":36814,"duration_ms":25358,"tool_calls":1,"models":{}}}
```

Notes: assistant text arrives as MANY small `"delta":true` chunks; `tool_result` carries `status` but no output text; the error envelope on nonzero exit (stderr, exit 41/55) is unchanged from `-o json`. No thought/reasoning event type was observed (flash model) — handle `"type":"thought"` defensively but don't rely on it.

**codex-cli 0.144.4** `codex exec --json`: vocabulary unchanged from FINDINGS.md (`thread.started`, `turn.started`, `item.started`/`item.completed` with `item.type` in `{agent_message, command_execution}`, `turn.completed`). `item.started` and `item.completed` for the same command share `item.id` (e.g. `"item_0"`) — that is the pairing key. **No reasoning items are emitted even with `-c show_raw_agent_reasoning=true`** (verified live; `reasoning_output_tokens` > 0 in usage but no item). Map `item.type == "reasoning"` (text field) defensively anyway — protocol documents it and future builds may emit it.

---

### Task 1: Commit captured Gemini stream-json fixture + FINDINGS.md addendum

**Files:**
- Create: `tests/fixtures/structured/gemini_stream.ndjson`
- Modify: `tests/fixtures/structured/FINDINGS.md` (append addendum at end)

**Interfaces:**
- Produces: `gemini_stream.ndjson` — golden fixture consumed by Task 4's tests.

- [ ] **Step 1: Write the fixture** — `tests/fixtures/structured/gemini_stream.ndjson`, exactly the 7 lines from "Verified wire facts" above (the second delta line verbatim as shown, with the literal `...` inside the string — it is representative text, not a placeholder).

- [ ] **Step 2: Append addendum to FINDINGS.md:**

```markdown
## Addendum 2026-08-04 — stream-json probes (gemini 0.46.0, codex-cli 0.144.4)

- Gemini `-o stream-json` verified live (this Mac, GEMINI auth working). NDJSON on
  stdout: `init` (session_id, model), `message` (role user echo; role assistant with
  `"delta": true` streamed chunks), `tool_use` (tool_name, tool_id, parameters),
  `tool_result` (tool_id, status — NO output text), `result` (status, stats)
  terminator. Captured as `gemini_stream.ndjson`. Error envelope on stderr +
  nonzero exit is unchanged from `-o json`.
- Codex 0.144.4 emits NO reasoning items in `exec --json`, even with
  `-c show_raw_agent_reasoning=true` (usage reports reasoning_output_tokens > 0 but
  no item ever appears). Driver maps `item.type == "reasoning"` defensively; do not
  expect it live on this build.
```

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/structured/gemini_stream.ndjson tests/fixtures/structured/FINDINGS.md
git commit -m "test(fixtures): gemini stream-json live capture + codex reasoning addendum"
```

---

### Task 2: `permission_mode` session field + bypassPermissions spawn flag

**Files:**
- Modify: `src/drover/server/harness/schema.py:91-100` (harness_sessions `_ensure_harness_columns` dict)
- Modify: `src/drover/server/harness/models.py:51-100` (`HarnessSession`)
- Modify: `src/drover/server/harness/registry.py:146-199` (`create_session`)
- Modify: `src/drover/server/harness/structured/claude.py:24-34` (`default_command`)
- Modify: `src/drover/server/harness/daemon.py:1527-1560` (`_create_structured_session`)
- Test: `tests/test_structured_claude.py`, `tests/test_harness_registry.py`, `tests/test_harness_daemon.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `POST /sessions` accepts optional `permission_mode: "auto"` (default) — `"ask"` → 400 "permission_mode 'ask' is not supported yet (approval surfacing is a follow-up); use 'auto'", other values → 400 "unknown permission_mode: <value>". `HarnessSession.permission_mode: str | None`. `claude.default_command()` returns `[..., "--permission-mode", "bypassPermissions"]`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_structured_claude.py` add:

```python
def test_default_command_bypasses_permissions():
    command = default_command(binary="/bin/claude")
    assert command[-2:] == ["--permission-mode", "bypassPermissions"]
    # bypass flag must come after the stream-json plumbing flags
    assert "--output-format" in command
```

(ensure `default_command` is imported at top: `from drover.server.harness.structured.claude import ..., default_command` — extend the existing import). In `tests/test_harness_registry.py` add (match the file's existing fixture style — it constructs `HarnessRegistry(tmp_path / "x.duckdb")`):

```python
def test_create_session_persists_permission_mode(tmp_path):
    registry = HarnessRegistry(tmp_path / "registry.duckdb")
    session = registry.create_session(
        host_id="h1", harness="claude-code", command="claude",
        mode="structured", permission_mode="auto",
    )
    fetched = registry.get_session(session.session_id)
    assert fetched is not None
    assert fetched.permission_mode == "auto"


def test_create_session_permission_mode_defaults_to_none(tmp_path):
    registry = HarnessRegistry(tmp_path / "registry.duckdb")
    session = registry.create_session(host_id="h1", harness="shell", command="sh")
    fetched = registry.get_session(session.session_id)
    assert fetched.permission_mode is None
```

In `tests/test_harness_daemon.py` add (uses the file's existing `_start_test_server`/`_json_request` helpers and `FAKE_STRUCTURED_CLI`, see `test_structured_session_full_lifecycle` at ~line 1594 for the pattern):

```python
def test_structured_session_rejects_ask_permission_mode(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        try:
            _json_request(
                f"{base_url}/sessions",
                payload={
                    "harness": "claude-code",
                    "mode": "structured",
                    "command": FAKE_STRUCTURED_CLI,
                    "cwd": str(tmp_path),
                    "permission_mode": "ask",
                },
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "not supported yet" in exc.read().decode()
        else:
            raise AssertionError("permission_mode=ask should be rejected")
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()


def test_structured_session_stores_default_permission_mode(tmp_path):
    server, state, base_url = _start_test_server(tmp_path)
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        session = state.registry.get_session(body["session_id"])
        assert session.permission_mode == "auto"
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_structured_claude.py tests/test_harness_registry.py tests/test_harness_daemon.py -x -q -k "permission_mode or bypasses_permissions"`
Expected: FAIL (`assert` on missing flag; `TypeError: unexpected keyword argument 'permission_mode'`; daemon accepts "ask").

- [ ] **Step 3: Implement.**

`schema.py` — in `bootstrap_harness_tables`, add to the harness_sessions `_ensure_harness_columns` dict (after `"last_activity": "TIMESTAMP"`):

```python
            "permission_mode": "VARCHAR",
```

`models.py` — `HarnessSession`: add field `permission_mode: str | None = None` (after `mode`), and in `from_row` add `permission_mode=row.get("permission_mode"),`.

`registry.py` — `create_session`: add keyword param `permission_mode: str | None = None` (after `mode`), add `permission_mode` to the INSERT column list, a 17th `?`, and the value after `mode` in the params list.

`claude.py` — `default_command` returns:

```python
    return [
        resolved_binary or "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        # M5: structured sessions run headless — without an answered
        # control_request channel, any gated tool call fails outright
        # ("requested permissions ... but you haven't granted it"), so the
        # only workable posture until approval surfacing (Part B) lands is
        # full bypass, matching codex danger-full-access / gemini yolo.
        "--permission-mode",
        "bypassPermissions",
    ]
```

`daemon.py` — top of `_create_structured_session` (after the `cwd` validation block):

```python
        permission_mode = str(body.get("permission_mode") or "auto")
        if permission_mode == "ask":
            self._write_json(
                {
                    "error": "permission_mode 'ask' is not supported yet "
                    "(approval surfacing is a follow-up); use 'auto'"
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if permission_mode != "auto":
            self._write_json(
                {"error": f"unknown permission_mode: {permission_mode}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
```

and pass `permission_mode=permission_mode,` in the `registry.create_session(...)` call (after `mode="structured"`).

- [ ] **Step 4: Run the tests again**

Run: `uv run pytest tests/test_structured_claude.py tests/test_harness_registry.py tests/test_harness_daemon.py -x -q`
Expected: PASS (full files, not just -k — catches regressions in neighboring tests that assert on `default_command()` argv or session listings).

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness tests/test_structured_claude.py tests/test_harness_registry.py tests/test_harness_daemon.py
git commit -m "feat(harnessd): permission_mode session field; structured claude defaults to bypassPermissions"
```

---

### Task 3: Codex driver — tool payload keys + defensive reasoning mapping

**Files:**
- Modify: `src/drover/server/harness/structured/codex.py:275-317`
- Test: `tests/test_structured_codex.py`

**Interfaces:**
- Consumes: codex NDJSON vocabulary (see "Verified wire facts").
- Produces: `tool_action.payload = {"tool": "shell", "tool_use_id": <item.id>, "input": {"command": <command>}, **item}`; `tool_result.payload = {"tool": "shell", "tool_use_id": <item.id>, "exit_code": ..., "status": ..., **item}`; reasoning items → `assistant_output` with `payload={"thinking": True}`. Task 6's iOS pairing relies on `tool_use_id` being identical across the started/completed pair.

- [ ] **Step 1: Write the failing tests.** In `tests/test_structured_codex.py`:

```python
def _messages_for_line(line: str) -> list:
    driver = CodexDriver(["codex"], cwd=None, emit=lambda m: None)
    return driver.parse_line(json.dumps(line) if isinstance(line, dict) else line)


def test_command_item_started_payload_has_tool_keys():
    [msg] = _messages_for_line(
        {"type": "item.started",
         "item": {"id": "item_1", "type": "command_execution",
                  "command": "pytest -x", "status": "in_progress"}}
    )
    assert msg.type == "tool_action"
    assert msg.payload["tool"] == "shell"
    assert msg.payload["tool_use_id"] == "item_1"
    assert msg.payload["input"] == {"command": "pytest -x"}


def test_command_item_completed_payload_has_tool_keys():
    [msg] = _messages_for_line(
        {"type": "item.completed",
         "item": {"id": "item_1", "type": "command_execution",
                  "command": "pytest -x", "aggregated_output": "3 passed\n",
                  "exit_code": 0, "status": "completed"}}
    )
    assert msg.type == "tool_result"
    assert msg.payload["tool"] == "shell"
    assert msg.payload["tool_use_id"] == "item_1"
    assert msg.payload["exit_code"] == 0
    assert msg.text == "3 passed\n"


def test_reasoning_item_maps_to_thinking():
    [msg] = _messages_for_line(
        {"type": "item.completed",
         "item": {"id": "item_0", "type": "reasoning", "text": "let me look"}}
    )
    assert msg.type == "assistant_output"
    assert msg.payload["thinking"] is True
    assert msg.text == "let me look"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_structured_codex.py -x -q -k "tool_keys or reasoning_item"`
Expected: FAIL (`KeyError: 'tool'`; reasoning falls through to `type == "status"`).

- [ ] **Step 3: Implement.** In `codex.py`, replace `_on_item_started` and the `command_execution`/default branches of `_on_item_completed`:

```python
    def _on_item_started(self, obj: dict[str, Any]) -> list[StructuredMessage]:
        item = obj.get("item") or {}
        if item.get("type") == "command_execution":
            return [
                StructuredMessage(
                    type="tool_action",
                    role="assistant",
                    text=str(item.get("command") or ""),
                    payload={
                        "tool": "shell",
                        "tool_use_id": item.get("id"),
                        "input": {"command": item.get("command")},
                        **item,
                    },
                )
            ]
        return []  # e.g. agent_message item.started, if it ever occurs

    def _on_item_completed(self, obj: dict[str, Any]) -> list[StructuredMessage]:
        item = obj.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=item.get("text") or "",
                )
            ]
        if item_type == "reasoning":
            # Defensive: codex 0.144.4 never emits this in exec --json (even
            # with show_raw_agent_reasoning=true, verified live 2026-08-04),
            # but the protocol documents it and newer builds may.
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=item.get("text") or "",
                    payload={"thinking": True},
                )
            ]
        if item_type == "command_execution":
            output = item.get("aggregated_output") or ""
            return [
                StructuredMessage(
                    type="tool_result",
                    role="tool",
                    text=output,
                    payload={
                        "tool": "shell",
                        "tool_use_id": item.get("id"),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                        **item,
                    },
                )
            ]
        return [
            StructuredMessage(
                type="status", role="system", text=str(item_type), payload=obj
            )
        ]
```

(`**item` after the explicit keys is safe: `item` has no `tool`/`tool_use_id`/`input` keys of its own, and keeping the raw item fields preserves today's payload contents for any downstream consumer.)

- [ ] **Step 4: Run the full codex test file**

Run: `uv run pytest tests/test_structured_codex.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/structured/codex.py tests/test_structured_codex.py
git commit -m "feat(harnessd): codex driver emits tool/tool_use_id payload keys + defensive reasoning mapping"
```

---

### Task 4: Gemini driver — stream-json line pump with delta coalescing

**Files:**
- Modify: `src/drover/server/harness/structured/gemini.py` (argv, `_run_turn`, new parsing; module docstring)
- Test: `tests/test_structured_gemini.py`

**Interfaces:**
- Consumes: gemini stream-json NDJSON (see "Verified wire facts", fixture `gemini_stream.ndjson`).
- Produces: per-turn message sequence — `status` (init, carries `native_session_id`), `tool_action` (`{"tool": <tool_name>, "tool_use_id": <tool_id>, "input": <parameters>}`), `tool_result` (`{"tool_use_id": <tool_id>, "status": ...}`), ONE coalesced `assistant_output` per contiguous delta run, `status` "turn complete" (`{"turn_complete": True, "awaiting": "input", "stats": ...}`). Errors unchanged (`parse_error` envelope path).

- [ ] **Step 1: Write the failing tests.** In `tests/test_structured_gemini.py`, replace the fake gemini's `-o json` blob with an NDJSON emitter and add mapping tests. Add at module level:

```python
FIXTURES_DIR = Path("tests/fixtures/structured")

FAKE_GEMINI_STREAM = """
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("GEMINI_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        print(json.dumps(argv), file=fh)
for line in open(os.environ["GEMINI_STREAM_FIXTURE"]):
    if line.strip():
        print(line.rstrip(), flush=True)
"""
```

and tests (mirroring the file's existing fake-CLI harness helpers — it already builds a fake binary from a Python source string and collects emitted messages; reuse that helper, only the source string and env differ):

```python
def test_stream_json_turn_maps_events(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_STREAM_FIXTURE", str(FIXTURES_DIR / "gemini_stream.ndjson"))
    messages = _run_fake_turn(tmp_path, FAKE_GEMINI_STREAM)  # existing helper pattern
    types = [m.type for m in messages]
    # init status, tool_action, tool_result, ONE coalesced assistant_output,
    # then turn-complete status. The user-echo message line is skipped
    # (manager already records user_input for every sent turn).
    assert types == ["status", "tool_action", "tool_result", "assistant_output", "status"]
    action = messages[1]
    assert action.payload["tool"] == "list_directory"
    assert action.payload["tool_use_id"] == "list_directory__859y31fx"
    assert action.payload["input"] == {"dir_path": "."}
    result = messages[2]
    assert result.payload["tool_use_id"] == "list_directory__859y31fx"
    output = messages[3]
    assert output.text.startswith("probe-ok")
    assert "current directory" in output.text  # both delta chunks joined
    final = messages[4]
    assert final.payload["turn_complete"] is True
    assert final.payload["awaiting"] == "input"


def test_argv_uses_stream_json(tmp_path):
    driver = GeminiDriver(["gemini"], cwd=None, emit=lambda m: None)
    argv = driver._argv_for("hello")
    assert "-o" in argv and argv[argv.index("-o") + 1] == "stream-json"
    assert "--approval-mode" in argv and "yolo" in argv
    assert "--skip-trust" in argv
```

Keep every existing error-path test (`parse_error`, nonzero exit) — those behaviors are unchanged and must keep passing.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_structured_gemini.py -x -q`
Expected: FAIL (argv asserts `-o json`; one monolithic assistant_output instead of the mapped sequence).

- [ ] **Step 3: Implement.** In `gemini.py`:

`_argv_for`: change `"json"` → `"stream-json"`.

Replace `_run_turn`/`build_messages` with a line pump plus a per-turn delta buffer. The pump mirrors `codex.py`'s `_pump_turn` (stderr tail thread + line loop); on nonzero exit, the existing `parse_error` runs against the collected stderr tail:

```python
    def _run_turn(self, process: subprocess.Popen[str], turn_id: str) -> None:
        stderr_lines: list[str] = []

        def pump_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\n"))

        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stderr_thread.start()
        delta_buffer: list[str] = []

        def flush_deltas() -> None:
            if not delta_buffer:
                return
            text = "".join(delta_buffer)
            delta_buffer.clear()
            self.emit(
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=text,
                    turn_id=turn_id,
                )
            )

        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                for message in self.parse_stream_line(line, delta_buffer, turn_id):
                    flush_deltas()
                    self.emit(message)
            flush_deltas()
            returncode = process.wait()
            stderr_thread.join(timeout=2)
        finally:
            with self._turn_lock:
                self._turn_process = None
                self._turn_active = False
        if returncode != 0:
            self.emit(self.parse_error(returncode, "\n".join(stderr_lines), turn_id=turn_id))
            self.emit(
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn exited",
                    payload={"exited": returncode},
                    turn_id=turn_id,
                )
            )
```

New `parse_stream_line` (returns messages to emit; assistant deltas accumulate into `delta_buffer` and return `[]`, so a contiguous delta run becomes ONE `assistant_output` flushed by the caller when the run ends):

```python
    def parse_stream_line(
        self, line: str, delta_buffer: list[str], turn_id: str
    ) -> list[StructuredMessage]:
        try:
            obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return [
                StructuredMessage(
                    type="raw", role="system", text=line,
                    payload={"stream": "stdout"}, turn_id=turn_id,
                )
            ]
        kind = obj.get("type")
        if kind == "message":
            if obj.get("role") == "assistant":
                delta_buffer.append(str(obj.get("content") or ""))
                return []
            # role=user is the CLI echoing the prompt back; the manager
            # already records a user_input event for every sent turn, so
            # emitting this would duplicate it in the transcript.
            return []
        if kind == "init":
            return [
                StructuredMessage(
                    type="status", role="system", text="init",
                    payload={"native_session_id": obj.get("session_id"), **obj},
                    turn_id=turn_id,
                )
            ]
        if kind == "tool_use":
            return [
                StructuredMessage(
                    type="tool_action",
                    role="assistant",
                    text=str(obj.get("tool_name") or ""),
                    payload={
                        "tool": obj.get("tool_name"),
                        "tool_use_id": obj.get("tool_id"),
                        "input": obj.get("parameters"),
                    },
                    turn_id=turn_id,
                )
            ]
        if kind == "tool_result":
            return [
                StructuredMessage(
                    type="tool_result",
                    role="tool",
                    text=str(obj.get("output") or obj.get("status") or ""),
                    payload={
                        "tool_use_id": obj.get("tool_id"),
                        "status": obj.get("status"),
                        **obj,
                    },
                    turn_id=turn_id,
                )
            ]
        if kind == "thought":
            # Defensive: never observed live (2026-08-04 probe, flash model);
            # gemini docs suggest thought summaries may stream in some modes.
            return [
                StructuredMessage(
                    type="assistant_output",
                    role="assistant",
                    text=str(obj.get("content") or obj.get("text") or ""),
                    payload={"thinking": True},
                    turn_id=turn_id,
                )
            ]
        if kind == "result":
            return [
                StructuredMessage(
                    type="status",
                    role="system",
                    text="turn complete",
                    payload={
                        "turn_complete": True,
                        "awaiting": "input",
                        "stats": obj.get("stats"),
                        "status": obj.get("status"),
                    },
                    turn_id=turn_id,
                )
            ]
        return [
            StructuredMessage(
                type="status", role="system", text=str(kind), payload=obj,
                turn_id=turn_id,
            )
        ]
```

Delete `build_messages` (dead) and update its callers/tests; keep `parse_error` and `_try_parse_envelope` unchanged. Also update `send_turn`'s `Popen` to `bufsize=1` (line-buffered, matching codex). Update the module docstring: point 1 now reads "one `gemini -p <text> -o stream-json` subprocess per turn streaming NDJSON (verified live 2026-08-04, gemini 0.46.0, captured as gemini_stream.ndjson)".

- [ ] **Step 4: Run the full gemini test file**

Run: `uv run pytest tests/test_structured_gemini.py -x -q`
Expected: PASS.

- [ ] **Step 5: Run neighboring suites for regressions** (manager/e2e touch driver behavior)

Run: `uv run pytest tests/test_structured_manager.py tests/test_structured_e2e.py tests/test_structured_driver.py -x -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/drover/server/harness/structured/gemini.py tests/test_structured_gemini.py
git commit -m "feat(harnessd): gemini driver streams NDJSON — per-step tool events, coalesced deltas, no more one-blob turns"
```

---

### Task 5: Claude driver — `payload.tool` on tool_result

**Files:**
- Modify: `src/drover/server/harness/structured/claude.py` (`ClaudeDriver`)
- Test: `tests/test_structured_claude.py`

**Interfaces:**
- Consumes: existing claude stream-json `tool_use`/`tool_result` blocks.
- Produces: `tool_result.payload["tool"]` = the originating tool's name (joined via `tool_use_id`), when known.

- [ ] **Step 1: Write the failing test** in `tests/test_structured_claude.py`:

```python
def test_tool_result_payload_carries_tool_name():
    driver = ClaudeDriver(["claude"], cwd=None, emit=lambda m: None, env={})
    action_line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "ls"}}
        ]},
    })
    result_line = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
        ]},
    })
    driver.parse_line(action_line)
    [result] = driver.parse_line(result_line)
    assert result.type == "tool_result"
    assert result.payload["tool"] == "Bash"
```

(Match the file's existing `ClaudeDriver` construction — if its tests build the driver differently, e.g. without `env=`, mirror that.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_structured_claude.py -x -q -k carries_tool_name`
Expected: FAIL with `KeyError: 'tool'`.

- [ ] **Step 3: Implement.** In `ClaudeDriver`, add an instance map and populate/read it in `_from_content`. `ProcessDriver` subclasses define `__init__` via the base; add to `ClaudeDriver`:

```python
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # tool_use_id -> tool name, so a later tool_result can carry the
        # originating tool's name for display (iOS card titles). Unbounded
        # but tiny: one short string pair per tool call in the session.
        self._tool_names: dict[str, str] = {}
```

In `_from_content`, in the `tool_use` branch, after building the message, record the name:

```python
                if block.get("id") and block.get("name"):
                    self._tool_names[str(block["id"])] = str(block["name"])
```

and in the `tool_result` branch, build the payload as:

```python
                        payload={
                            **base_payload,
                            "tool_use_id": block.get("tool_use_id"),
                            "tool": self._tool_names.get(
                                str(block.get("tool_use_id"))
                            ),
                        },
```

- [ ] **Step 4: Run the full claude test file**

Run: `uv run pytest tests/test_structured_claude.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/structured/claude.py tests/test_structured_claude.py
git commit -m "feat(harnessd): claude tool_result payload carries originating tool name"
```

---

### Task 6: NexusKit — step pairing in TranscriptItem

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/Transcript.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift`

**Interfaces:**
- Consumes: `HarnessMessage.payload["tool_use_id"]` on `.toolAction`/`.toolResult` (Tasks 3-5).
- Produces: new case `TranscriptItem.step(action: HarnessMessage, result: HarnessMessage?)`; `id` = action's id (stable when the result attaches). `group(_:)` pairs by `tool_use_id`; unmatched results stay `.message`. `latestRowID(of:)` returns the id of `group(messages).last` (so a result that folds into an earlier step row targets that row).

- [ ] **Step 1: Write the failing tests** in `TranscriptTests.swift` (uses the existing `HarnessMessage.fixture` test-support initializer; payload values are `JSONValue` — use `.string(...)`):

```swift
@Suite struct StepPairingTests {
    private func action(_ seq: Int, id toolID: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolAction, role: "assistant",
                       text: "Bash", payload: ["tool": .string("Bash"),
                                               "tool_use_id": .string(toolID)])
    }
    private func result(_ seq: Int, id toolID: String) -> HarnessMessage {
        HarnessMessage(seq: seq, type: .toolResult, role: "tool",
                       text: "ok", payload: ["tool_use_id": .string(toolID)])
    }

    @Test func pairsActionWithItsResult() {
        let a = action(1, id: "t1"), r = result(2, id: "t1")
        let items = TranscriptItem.group([a, r])
        #expect(items == [.step(action: a, result: r)])
    }

    @Test func stepRowIDIsStableWhenResultAttaches() {
        let a = action(1, id: "t1"), r = result(2, id: "t1")
        #expect(TranscriptItem.group([a]).last?.id == a.id)
        #expect(TranscriptItem.group([a, r]).last?.id == a.id)
    }

    @Test func pairsAcrossInterveningMessages() {
        let a = action(1, id: "t1")
        let thinking = HarnessMessage(seq: 2, type: .assistantOutput,
                                      text: "hm", payload: ["thinking": .bool(true)])
        let r = result(3, id: "t1")
        let items = TranscriptItem.group([a, thinking, r])
        #expect(items.count == 2)
        #expect(items[0] == .step(action: a, result: r))
    }

    @Test func unmatchedResultStaysAMessage() {
        let r = result(1, id: "orphan")
        #expect(TranscriptItem.group([r]) == [.message(r)])
    }

    @Test func actionWithoutToolUseIDStaysAMessage() {
        let bare = HarnessMessage(seq: 1, type: .toolAction, text: "Bash")
        #expect(TranscriptItem.group([bare]) == [.message(bare)])
    }

    @Test func latestRowIDTargetsStepRowWhenResultIsNewest() {
        let a = action(1, id: "t1")
        let out = HarnessMessage(seq: 2, type: .assistantOutput, text: "mid")
        let r = result(3, id: "t1")
        #expect(TranscriptItem.latestRowID(of: [a, out, r]) == a.id)
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd "apps/drover/NexusKit" && swift test`
Expected: FAIL to compile (`.step` case doesn't exist).

- [ ] **Step 3: Implement** in `Transcript.swift`. Add the case and extend `group`:

```swift
public enum TranscriptItem: Identifiable, Equatable, Sendable {
    case message(HarnessMessage)
    /// Always non-empty; ordered as received.
    case thinkingRun([HarnessMessage])
    /// A tool call paired (by payload `tool_use_id`) with its result. The
    /// result attaches in place when it streams in; the row keeps the
    /// action's identity so SwiftUI updates rather than rebuilds it.
    case step(action: HarnessMessage, result: HarnessMessage?)

    public var id: String {
        switch self {
        case .message(let message): message.id
        case .thinkingRun(let run): run[0].id
        case .step(let action, _): action.id
        }
    }
```

In `group(_:)`, track pending steps by tool-use id (actions without an id, and results with no pending match, fall through to `.message` — old recorded sessions keep rendering):

```swift
    public static func group(_ messages: [HarnessMessage]) -> [TranscriptItem] {
        var items: [TranscriptItem] = []
        items.reserveCapacity(messages.count)
        var run: [HarnessMessage] = []
        /// tool_use_id -> index in `items` of the awaiting `.step` row.
        var pendingSteps: [String: Int] = [:]

        func flushRun() {
            guard !run.isEmpty else { return }
            items.append(.thinkingRun(run))
            run = []
        }

        for message in messages {
            if message.isThinking {
                run.append(message)
                continue
            }
            if message.type == .toolAction,
               let toolUseID = message.payload["tool_use_id"]?.stringValue {
                flushRun()
                pendingSteps[toolUseID] = items.count
                items.append(.step(action: message, result: nil))
                continue
            }
            if message.type == .toolResult,
               let toolUseID = message.payload["tool_use_id"]?.stringValue,
               let index = pendingSteps.removeValue(forKey: toolUseID),
               case .step(let action, nil) = items[index] {
                items[index] = .step(action: action, result: message)
                continue
            }
            flushRun()
            items.append(.message(message))
        }
        flushRun()
        return items
    }
```

Replace `latestRowID`'s body with the group-consistent version (the hand-rolled thinking-tail walk can't see step folding):

```swift
    public static func latestRowID(of messages: [HarnessMessage]) -> String? {
        group(messages).last?.id
    }
```

(O(n) per call; it already ran O(n) and callers are coalesced to ~8Hz, so this is fine.)

- [ ] **Step 4: Run the full NexusKit suite**

Run: `cd "apps/drover/NexusKit" && swift test`
Expected: PASS — including the existing `TranscriptGroupingTests` (`latestRowIDIsRunStartForThinkingTail` etc. must still hold under the new implementation).

- [ ] **Step 5: Commit**

```bash
git add apps/drover/NexusKit/Sources/NexusKit/Transcript.swift apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift
git commit -m "feat(ios): TranscriptItem.step — pair tool actions with results by tool_use_id"
```

---

### Task 7: iOS — collapsed StepCard + tool-card fallback fix

**Files:**
- Create: `apps/drover/Drover/Screens/Chat/StepCard.swift`
- Modify: `apps/drover/Drover/Screens/Chat/MessageBubble.swift` (fallback title/detail)
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift:131-142` (`row(for:isNewest:)`)

**Interfaces:**
- Consumes: `TranscriptItem.step` (Task 6); `payload` keys `tool`, `input`, `exit_code`, `status` (Tasks 3-5); existing `EditDiff`, `DisplayBlock`, `CodeBlockView`, `DiffBlockView`.
- Produces: `StepCard(action:result:)` view. `MessageBubble` keeps handling *unpaired* `.toolAction`/`.toolResult`.

- [ ] **Step 1: Write `StepCard.swift`:**

```swift
import SwiftUI
import NexusKit

/// One collapsed row per tool step: a `tool_action` paired (or awaiting
/// pairing) with its `tool_result`. Collapsed shows tool name + one-line
/// status (`running…` / ✓ / ✗); expanded shows the input and full result
/// through the shared code/diff rendering. This is the "everything
/// intermediate is compact" half of the M5 transcript design — final
/// assistant output stays full-size in MessageBubble.
struct StepCard: View {
    let action: HarnessMessage
    let result: HarnessMessage?
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "wrench.fill")
                    Text(title).lineLimit(1)
                    Spacer(minLength: 8)
                    statusChip
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(.callout)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("step-card")

            if isExpanded {
                VStack(alignment: .leading, spacing: 6) {
                    if let editDiff = EditDiff(message: action) {
                        if let filePath = editDiff.filePath {
                            Text(filePath)
                                .font(.system(.caption2, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        DiffBlockView(lines: editDiff.diffLines)
                    } else if let command = action.payload["input"]?.objectValue?["command"]?.stringValue {
                        CodeBlockView(language: "sh", code: command)
                    } else if let input = action.payload["input"]?.displayString, !input.isEmpty {
                        Text(input)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let result, !result.text.isEmpty {
                        Divider()
                        ForEach(Array(result.displayBlocks.enumerated()), id: \.offset) { _, block in
                            switch block {
                            case .text(let attributed):
                                Text(attributed).font(.caption)
                            case .code(let language, let code):
                                CodeBlockView(language: language, code: code)
                            case .diff(let lines):
                                DiffBlockView(lines: lines)
                            }
                        }
                    }
                }
                .transition(.opacity)
            }
        }
        .padding(10)
        .background(.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var title: String {
        let tool = action.payload["tool"]?.stringValue ?? action.text
        if tool == "shell",
           let command = action.payload["input"]?.objectValue?["command"]?.stringValue,
           let firstLine = command.split(separator: "\n").first {
            return String(firstLine.prefix(72))
        }
        if let filePath = action.payload["input"]?.objectValue?["file_path"]?.stringValue {
            return "\(tool) \(URL(fileURLWithPath: filePath).lastPathComponent)"
        }
        return tool
    }

    @ViewBuilder
    private var statusChip: some View {
        if let result {
            let exitCode = result.payload["exit_code"]?.numberValue.map { Int($0) }
            let failed = (exitCode ?? 0) != 0
                || result.payload["status"]?.stringValue == "failed"
            Label(exitCode.map { failed ? "exit \($0)" : "done" } ?? "done",
                  systemImage: failed ? "xmark.circle" : "checkmark.circle")
                .font(.caption)
                .foregroundStyle(failed ? .red : .secondary)
        } else {
            HStack(spacing: 4) {
                ProgressView().controlSize(.mini)
                Text("running…")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}
```

(`JSONValue` accessors verified in `Models.swift:195-230`: `stringValue`, `boolValue`, `numberValue`, `objectValue`, `displayString` exist; there is no subscript or `intValue` — nested access goes through `objectValue?[...]`, the same pattern `EditDiff.swift:25` uses.)

- [ ] **Step 2: Wire `.step` into `ChatView.row(for:isNewest:)`:**

```swift
        case .step(let action, let result):
            StepCard(action: action, result: result)
```

- [ ] **Step 3: Fix the unpaired fallback in `MessageBubble.swift`** — replace `toolName`/`toolDetail`:

```swift
    private var toolName: String {
        if let tool = message.payload["tool"]?.stringValue { return tool }
        // Old recorded sessions: tool results carried no tool key, and text
        // is the entire output — never use it as a title.
        return message.type == .toolResult ? "Tool result" : message.text
    }

    private var toolDetail: String? {
        if message.type == .toolResult {
            return message.text.isEmpty
                ? message.payload["result"]?.displayString : message.text
        }
        return message.payload["input"]?.displayString
            ?? message.payload["result"]?.displayString
    }
```

- [ ] **Step 4: Build + run app unit tests**

Run: `cd "apps/drover/NexusKit" && swift test` and build the app: `cd "apps/drover" && xcodegen 2>/dev/null; xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 16' build 2>&1 | tail -5` (regenerate the project first if the repo uses xcodegen — `Drover.xcodeproj` is gitignored on feature branches; follow whatever the M4 branch did).
Expected: build succeeds; NexusKit tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/drover/Drover/Screens/Chat/StepCard.swift apps/drover/Drover/Screens/Chat/MessageBubble.swift apps/drover/Drover/Screens/Chat/ChatView.swift apps/drover/NexusKit/Sources/NexusKit
git commit -m "feat(ios): collapsed StepCard for tool steps; tool-card fallback no longer titles with raw output"
```

---

### Task 8: iOS — scroll pinning fixes

**Files:**
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift:85-179`

**Interfaces:**
- Consumes: existing `isPinnedToBottom`, `scheduleScroll`, `TranscriptItem.latestRowID` (Task 6 version).
- Produces: behavior only — no API.

- [ ] **Step 1: Gate unpinning on user gestures.** Add state and phase tracking to `ChatView`:

```swift
    /// Current scroll phase — only user-driven phases may unpin (content
    /// growth pushing the bottom away must not; that was the stuck-button
    /// race: a tall new row unpinned before the coalesced scroll fired).
    @State private var scrollPhase: ScrollPhase = .idle
```

On the transcript `ScrollView`, add (next to `onScrollGeometryChange`):

```swift
            .onScrollPhaseChange { _, newPhase in
                scrollPhase = newPhase
            }
```

and change the geometry action so only re-pinning is unconditional:

```swift
            } action: { _, isNearBottom in
                guard isNearBottom != isPinnedToBottom else { return }
                // Re-pin whenever the bottom is reached, by any means; unpin
                // only mid-gesture (tracking/interacting/decelerating), so
                // content growth can't silently disable auto-scroll.
                let isUserDriven = scrollPhase == .tracking
                    || scrollPhase == .interacting
                    || scrollPhase == .decelerating
                guard isNearBottom || isUserDriven else { return }
                withAnimation(.snappy(duration: 0.2)) { isPinnedToBottom = isNearBottom }
            }
```

- [ ] **Step 2: Make the button re-pin explicitly and settle.** Replace the button's action:

```swift
        Button {
            guard let rowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            withAnimation(.snappy) {
                isPinnedToBottom = true
                proxy.scrollTo(rowID, anchor: .bottom)
            }
            // Late-measuring lazy rows (tall diffs) can land the animated
            // scroll short; one unanimated follow-up after layout settles
            // closes the gap so pinning actually holds.
            scheduleScroll(with: proxy)
        } label: {
```

- [ ] **Step 3: Add a settle pass to `scheduleScroll`:**

```swift
    private func scheduleScroll(with proxy: ScrollViewProxy) {
        guard pendingScroll == nil else { return }
        pendingScroll = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, isPinnedToBottom,
                  let rowID = TranscriptItem.latestRowID(of: model.messages) else {
                pendingScroll = nil
                return
            }
            proxy.scrollTo(rowID, anchor: .bottom)
            // Settle pass: rows that finish measuring after the first scroll
            // (LazyVStack + tall code/diff blocks) grow the content under us;
            // one more unanimated scroll pins the real bottom.
            try? await Task.sleep(for: .milliseconds(200))
            pendingScroll = nil
            guard !Task.isCancelled, isPinnedToBottom,
                  let settledRowID = TranscriptItem.latestRowID(of: model.messages) else { return }
            proxy.scrollTo(settledRowID, anchor: .bottom)
        }
    }
```

- [ ] **Step 4: Start at the bottom.** On the `ScrollView` (with the other modifiers) add:

```swift
            .defaultScrollAnchor(.bottom)
```

- [ ] **Step 5: Build + full iOS test pass**

Run: `cd "apps/drover/NexusKit" && swift test`, then the app test suite the repo already uses (M4 baseline): `cd "apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 16' test 2>&1 | tail -20`
Expected: PASS, zero new warnings.

- [ ] **Step 6: Commit**

```bash
git add apps/drover/Drover/Screens/Chat/ChatView.swift
git commit -m "fix(ios): scroll pinning — gesture-only unpin, explicit re-pin on button, settle pass, open at bottom"
```

---

### Task 9: Full-suite green, deploy, live verification

**Files:** none (operations)

- [ ] **Step 1: Full server suite**

Run: `uv run pytest -x -q`
Expected: PASS.

- [ ] **Step 2: Full iOS suite** (NexusKit + app scheme, as in Task 8 Step 5). Expected: PASS.

- [ ] **Step 3: Push and deploy harnessd to both hosts** (mac-mini + work-laptop) using the same restart procedure as the 2026-08-02 echo-fix deploy (launchd service restart on the Mac; the work laptop runs harnessd under its own launcher — check `deploy/` notes and memory `harnessd-echo-fix-live-verified`). The central server on this Mac restarts with the same build.

- [ ] **Step 4: Live verification checklist** (record results in the session/memory):
  - Terminate the stuck work-laptop session `harness-d5ba7c43-6d26-4c9a-aad5-8517b6445826` from the app.
  - New structured claude-code session on work-laptop → ask it to use the Linear MCP tool (the exact scenario that failed) → tool runs without a permission failure.
  - New gemini session → transcript shows step cards + coalesced output (no single wall of text).
  - New codex session → run a command (e.g. "run pytest -x in <repo>") → collapsed step card titled with the command, ✓/✗ status, output behind expand.
  - Scroll check on a long transcript: scroll up mid-stream → button appears → tap → lands at bottom, button disappears, auto-scroll resumes; reopen session → opens at bottom.
  - Old session replay: open a pre-M5 recorded codex session → tool results render as "Tool result" cards with output behind Details (not walls).

- [ ] **Step 5: Update memory + close out** — record deploy/verification status in auto-memory, note that spec Part B (approval surfacing) is the follow-up milestone.

---

## Self-review notes (already applied)

- Spec §6 called for a scroll-re-pin UITest; the existing smoke UITests attach to an externally-launched, live-fleet app (`PinchSmokeUITests`), which can't drive a streaming transcript deterministically. Replaced by Task 6's `latestRowID`/pairing unit tests plus the explicit scroll items in Task 9's live checklist. Spec updated accordingly.
- `permission_mode` is not mirrored into central's registry row (`_sync_created_harness_session`) — daemon-side storage only, per spec ("stored as a session column"); central sync tolerates unknown request keys untouched.
- Claude `--permission-mode bypassPermissions` is appended in `default_command()` unconditionally rather than threaded per-session: `ask` is rejected at creation in this milestone, so every structured claude session that spawns IS auto. Part B threads the field down to the driver.
