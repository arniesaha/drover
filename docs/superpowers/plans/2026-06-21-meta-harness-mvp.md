# Meta Harness MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Nexus Meta Harness MVP that lets Arnab control local CLI harness sessions from a phone while Nexus stores durable session context.

**Architecture:** Nexus remains the control/context plane. A per-host `nexus-harnessd` data-plane daemon owns PTY/tmux processes. Redis Streams coordinate retryable control events; WebSockets carry live terminal data; DuckDB stores durable metadata and redacted transcript chunks.

**Tech Stack:** Python, Click, DuckDB, Redis Streams, WebSockets, existing Nexus built-in web UI, pytest.

## Global Constraints

- Keep Nexus local-first and single-user for MVP.
- Do not stream raw terminal bytes through Redis.
- Use WebSockets for live terminal output/input.
- Use Redis only for coordination, leases, heartbeats, retry, and DLQ.
- Treat all harnesses as terminal programs in v1.
- Keep native iOS, video streaming, and enterprise policy out of MVP.
- Protect all public routes behind existing Nexus/Cloudflare Access deployment.
- Do not modify the dirty `/home/Arnab/dev/nexus` checkout; use clean worktrees.

---

## File Structure

- Create `docs/meta-harness-mvp.md` for product and architecture scope.
- Create `src/nexus/server/harness/models.py` for host/session/event records.
- Create `src/nexus/server/harness/schema.py` for DuckDB table bootstrap.
- Create `src/nexus/server/harness/registry.py` for host/session CRUD.
- Create `src/nexus/server/harness/daemon.py` for `nexus-harnessd`.
- Create `src/nexus/server/harness/pty.py` for PTY/tmux session management.
- Create `src/nexus/server/harness/ws.py` for terminal WebSocket attach/input.
- Create `src/nexus/server/harness/control_streams.py` for Redis-backed control events.
- Modify `src/nexus/server/__main__.py` to expose CLI commands and runtime wiring.
- Modify the built-in web UI files that currently serve `/` and `/ui` to add Harness navigation.
- Add focused tests under `tests/test_harness_*.py`.

## Task 1: Host And Session Registry

**Files:**
- Create: `src/nexus/server/harness/models.py`
- Create: `src/nexus/server/harness/schema.py`
- Create: `src/nexus/server/harness/registry.py`
- Modify: `src/nexus/schema.py`
- Test: `tests/test_harness_registry.py`

**Interfaces:**
- Produces: `bootstrap_harness_tables(con) -> None`
- Produces: `HarnessRegistry.register_host(...) -> HarnessHost`
- Produces: `HarnessRegistry.create_session(...) -> HarnessSession`
- Produces: `HarnessRegistry.update_session_status(...) -> HarnessSession`

- [ ] **Step 1: Write failing registry tests**

```python
def test_register_host_upserts_capabilities(tmp_path):
    from nexus.schema import bootstrap
    from nexus.server.harness.registry import HarnessRegistry

    db_path = tmp_path / "nexus.duckdb"
    bootstrap(str(db_path))
    registry = HarnessRegistry(str(db_path))

    host = registry.register_host(
        host_id="nas",
        display_name="NAS",
        kind="linux",
        local_url="http://192.168.1.70:7081",
        tailscale_url=None,
        capabilities={"harnesses": ["shell", "codex"]},
    )

    assert host.host_id == "nas"
    assert host.status == "online"
    assert registry.list_hosts()[0].capabilities["harnesses"] == ["shell", "codex"]
```

- [ ] **Step 2: Run test and verify it fails**

Run: `uv run pytest tests/test_harness_registry.py -q`

Expected: FAIL because harness modules do not exist.

- [ ] **Step 3: Add models, schema, and registry**

Implement records for `HarnessHost`, `HarnessSession`, and `HarnessEvent`; add
DuckDB bootstrap tables from `docs/meta-harness-mvp.md`; add CRUD helpers in
`HarnessRegistry`.

- [ ] **Step 4: Wire schema bootstrap**

Call `bootstrap_harness_tables(con)` from the existing Nexus bootstrap path so
fresh databases always include harness tables.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_harness_registry.py tests/test_schema.py -q`

Expected: PASS.

## Task 2: Local PTY/tmux Session Manager

**Files:**
- Create: `src/nexus/server/harness/pty.py`
- Test: `tests/test_harness_pty.py`

**Interfaces:**
- Produces: `PtySessionManager.start(command, cwd, env, rows, cols) -> PtySession`
- Produces: `PtySessionManager.write(session_id, data) -> None`
- Produces: `PtySessionManager.read(session_id, max_bytes) -> bytes`
- Produces: `PtySessionManager.terminate(session_id) -> None`

- [ ] **Step 1: Write failing PTY tests**

Test a shell command that prints a sentinel string and exits. Add a second test
that starts an interactive shell, writes `echo NEXUS_OK\n`, and reads the output.

- [ ] **Step 2: Implement minimal PTY manager**

Use Python stdlib `pty`, `os`, `select`, `subprocess`, and process groups. Keep
tmux integration optional behind a later adapter if stdlib PTY is enough for MVP.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_harness_pty.py -q`

Expected: PASS on Linux/macOS.

## Task 3: `nexus-harnessd` Host Daemon

**Files:**
- Create: `src/nexus/server/harness/daemon.py`
- Modify: `src/nexus/server/__main__.py`
- Test: `tests/test_harness_daemon.py`

**Interfaces:**
- Produces CLI command: `nexus-server harnessd --host-id nas --listen 127.0.0.1:7081`
- Produces HTTP endpoints: `GET /healthz`, `GET /capabilities`, `POST /sessions`

- [ ] **Step 1: Write failing CLI smoke test**

Assert `nexus-server harnessd --help` shows host id, listen address, Nexus URL,
and token options.

- [ ] **Step 2: Implement daemon command and health endpoint**

Start with health/capabilities and no PTY streaming. Register configured
harness presets for shell, Claude Code, Codex, Gemini, and OpenClaw.

- [ ] **Step 3: Add create-session endpoint**

Validate command preset and cwd, start PTY session through `PtySessionManager`,
and return `session_id`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_daemon.py tests/test_server_cli.py -q`

Expected: PASS.

## Task 4: WebSocket Terminal Attach

**Files:**
- Create: `src/nexus/server/harness/ws.py`
- Modify: `src/nexus/server/harness/daemon.py`
- Test: `tests/test_harness_ws.py`

**Interfaces:**
- Produces endpoint: `GET /sessions/{session_id}/terminal`
- Message types: `output`, `input`, `resize`, `ping`, `error`, `closed`

- [ ] **Step 1: Write failing WebSocket integration test**

Start a daemon test server, create a shell session, connect WebSocket, send
`echo WS_OK\n`, and assert the output message contains `WS_OK`.

- [ ] **Step 2: Implement terminal stream loop**

Use WebSocket receive for input/resize and a read loop for PTY output. Keep
bounded buffers and close cleanly when the process exits.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_harness_ws.py -q`

Expected: PASS.

## Task 5: Nexus Harness API And UI Shell

**Files:**
- Modify the existing metrics/UI server module that serves `/` and `/ui`
- Create or modify tests for observability/UI routing
- Test: `tests/test_harness_ui.py`

**Interfaces:**
- Produces route: `/ui/harness`
- Produces route: `/ui/harness/sessions/:id`
- Produces JSON route: `/harness/hosts`
- Produces JSON route: `/harness/sessions`

- [ ] **Step 1: Write failing UI route tests**

Assert `/ui` links to Harness, `/ui/harness` renders host/session containers,
and JSON routes return empty arrays against a fixture DB.

- [ ] **Step 2: Add navigation and empty-state UI**

Add Harness navigation beside Pipeline Observatory. Build empty states first.

- [ ] **Step 3: Add host/session list rendering**

Render host status, supported harnesses, active sessions, and attach links.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_ui.py tests/test_metrics.py tests/test_observatory.py -q`

Expected: PASS.

## Task 6: Transcript Capture And Summaries

**Files:**
- Modify: `src/nexus/server/harness/registry.py`
- Modify: `src/nexus/server/harness/ws.py`
- Modify summarizer enqueue path as needed
- Test: `tests/test_harness_transcripts.py`

**Interfaces:**
- Produces: `HarnessRegistry.append_transcript_chunk(session_id, sequence, content_redacted, byte_count)`
- Produces: `HarnessRegistry.close_session(session_id, status)`
- Produces summary enqueue bridge from harness session to existing session summary pipeline

- [ ] **Step 1: Write failing transcript tests**

Create a session, append chunks, close it, and assert chunks are ordered and
summarization can be enqueued without duplicating existing `session_summaries`.

- [ ] **Step 2: Implement chunk persistence**

Persist bounded redacted chunks, record byte counts, and avoid storing raw
secret-bearing terminal buffers beyond the configured cap.

- [ ] **Step 3: Add close-and-summarize path**

On session close, create a Nexus summary input from metadata and transcript
chunks, then reuse the existing summarizer backend policy.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_transcripts.py tests/test_summarizer_worker.py -q`

Expected: PASS.

## Task 7: Redis Control Events

**Files:**
- Create: `src/nexus/server/harness/control_streams.py`
- Modify: `src/nexus/server/harness/daemon.py`
- Test: `tests/test_harness_control_streams.py`

**Interfaces:**
- Produces streams for launch request, heartbeat, state change, and dead-letter replay
- Reuses existing `RedisJobStream` contract where possible

- [ ] **Step 1: Write failing stream contract tests**

Assert a launch request is delivered once, ACKed after durable session creation,
redelivered after a simulated crash-before-ACK, and dead-lettered after retry
budget exhaustion.

- [ ] **Step 2: Implement stream wrapper**

Adapt existing Redis job stream primitives for harness control messages.

- [ ] **Step 3: Emit heartbeat and state events**

Have `nexus-harnessd` publish heartbeat and lifecycle events. Keep terminal
bytes out of Redis.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_control_streams.py tests/test_redis_job_streams.py -q`

Expected: PASS.

## Task 8: Multi-Host Dogfood Rollout

**Files:**
- Create: `docs/install-harnessd.md`
- Modify: `docs/meta-harness-mvp.md`
- Test: manual runbook evidence

**Interfaces:**
- Produces launchd/systemd install instructions for NAS, Mac Mini, and GPU PC
- Produces validation checklist for local/Tailscale access

- [ ] **Step 1: Write install docs**

Document service install, config, host token, listen address, and health check
commands for Linux and macOS.

- [ ] **Step 2: Deploy Mac Mini first**

Run `nexus-harnessd` locally, validate health, start shell session, attach from
desktop browser, and summarize.

- [ ] **Step 3: Deploy NAS**

Validate Linux service, start shell/Codex session, attach from phone via Nexus.

- [ ] **Step 4: Deploy GPU PC**

Validate heartbeat and optional shell/Codex/Gemini launch. Keep GPU wake/shutdown
outside the MVP unless already online.

- [ ] **Step 5: Record evidence**

Update issue comments with host health, session IDs, screenshots if useful,
and test command output.

## Self-Review

- Spec coverage: tasks cover registry, daemon, PTY, WebSocket, UI, transcript
  persistence, Redis coordination, and rollout.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: task interfaces consistently use host/session/event naming.
- Scope discipline: native iOS, enterprise policy, video streaming, and deep CLI
  semantic adapters are deliberately excluded from MVP.
