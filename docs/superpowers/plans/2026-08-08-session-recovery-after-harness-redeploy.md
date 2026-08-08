# Session Recovery After Harness Redeploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lazily recover restart-lost Claude and Codex structured sessions under their original Drover session IDs and deliver the triggering turn exactly once.

**Architecture:** Persist provider-native IDs as soon as structured drivers report them. Add an authenticated, idempotent harness recovery endpoint that reconstructs a driver from the local registry and native ID. When central Drover receives the precise pre-dispatch `unknown structured session` 404 for a turn, recover once and retry the original turn once; otherwise preserve current error behavior.

**Tech Stack:** Python 3.12, stdlib `ThreadingHTTPServer`, DuckDB-backed `HarnessRegistry`, pytest, Swift 6/DroverKit tests.

## Global Constraints

- Preserve the original Drover session ID, transcript sequence, cwd, and worktree.
- Recover lazily on the next turn; do not revive sessions at daemon startup.
- Support Claude Code and Codex only; Gemini remains explicitly unsupported.
- Retry only the exact 404 emitted before harness dispatch, and retry at most once.
- Do not retry 409, timeout, transport failure, ambiguous 5xx, permission, or interrupt actions.
- Do not log message text, credentials, or raw trace content.
- Keep composer text and attachments intact when recovery cannot happen.

---

### Task 1: Persist provider-native session identity

**Files:**
- Modify: `src/drover/server/harness/registry.py`
- Modify: `src/drover/server/harness/structured/manager.py`
- Test: `tests/test_harness_registry.py`
- Test: `tests/test_structured_manager.py`

**Interfaces:**
- Produces: `HarnessRegistry.update_session_native_id(session_id: str, native_session_id: str) -> None`
- Produces: manager behavior that persists non-empty `payload["native_session_id"]` before forwarding the event.

- [ ] **Step 1: Write failing registry and manager tests**

Add a registry test that creates a structured session, calls
`update_session_native_id("harness-s1", "provider-1")`, and asserts the fetched
row contains `provider-1`. Add a manager test with a fake structured message
whose payload contains `native_session_id`, then assert the registry row is
updated while its event sequence remains monotonic.

```python
registry.update_session_native_id("harness-s1", "provider-1")
assert registry.get_session("harness-s1").native_session_id == "provider-1"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q \
  tests/test_harness_registry.py -k native_session_id \
  tests/test_structured_manager.py -k native_session_id
```

Expected: fail because `update_session_native_id` and manager persistence do not exist.

- [ ] **Step 3: Implement minimal persistence**

Add an atomic registry update that ignores blank IDs and updates only
`native_session_id` plus `updated_at`. In `StructuredSessionManager.emit`, after
the event append succeeds and while the per-session lock is still held, extract
a trimmed string ID and persist it. A failure follows the manager's existing
best-effort registry-write policy and must not kill the pump thread.

```python
native_session_id = payload.get("native_session_id")
if isinstance(native_session_id, str) and native_session_id.strip():
    registry.update_session_native_id(session_id, native_session_id.strip())
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/registry.py \
  src/drover/server/harness/structured/manager.py \
  tests/test_harness_registry.py tests/test_structured_manager.py
git commit -m "fix(harness): persist provider session identity"
```

### Task 2: Teach structured drivers to resume

**Files:**
- Modify: `src/drover/server/harness/structured/manager.py`
- Modify: `src/drover/server/harness/structured/codex.py`
- Modify: `src/drover/server/harness/structured/claude.py`
- Test: `tests/test_structured_codex.py`
- Test: `tests/test_structured_claude.py`
- Test: `tests/test_structured_manager.py`

**Interfaces:**
- Produces: `StructuredSessionManager.start(..., native_session_id: str | None = None) -> None`
- Produces: `CodexDriver(..., native_session_id: str | None = None)` whose next argv uses `exec resume` when restored.
- Produces: Claude default command augmented with `--resume <id>` only for restored sessions.

- [ ] **Step 1: Write failing driver tests**

Add a Codex test constructing a driver with `native_session_id="thread-1"` and
assert its first `_argv_for("continue")` contains `exec resume thread-1`. Add a
Claude manager/factory test that starts with `native_session_id="claude-1"` and
asserts the spawned command ends with `--resume claude-1`. Assert a normal
start remains unchanged.

```python
driver = CodexDriver(["codex"], "/tmp", emit, native_session_id="thread-1")
assert driver._argv_for("continue")[1:4] == ["exec", "resume", "thread-1"]
```

- [ ] **Step 2: Run tests and verify RED**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q \
  tests/test_structured_codex.py tests/test_structured_claude.py \
  tests/test_structured_manager.py -k 'resume or native_session_id'
```

Expected: constructor/start signatures reject `native_session_id`.

- [ ] **Step 3: Implement minimal resume plumbing**

Extend the manager factory contract with the optional ID. Initialize Codex's
`_thread_id` from it. For Claude, append `--resume` and the ID to a copy of the
structured command. Gemini receives no resume ID because the recovery layer
rejects it before manager start.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run Step 2's command. Expected: pass with normal launch regression tests green.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/structured/manager.py \
  src/drover/server/harness/structured/codex.py \
  src/drover/server/harness/structured/claude.py \
  tests/test_structured_codex.py tests/test_structured_claude.py \
  tests/test_structured_manager.py
git commit -m "feat(harness): restore structured provider sessions"
```

### Task 3: Add idempotent same-session recovery to harnessd

**Files:**
- Modify: `src/drover/server/harness/registry.py`
- Modify: `src/drover/server/harness/daemon.py`
- Test: `tests/test_harness_daemon.py`

**Interfaces:**
- Produces: `HarnessRegistry.mark_session_recovered(session_id: str, native_session_id: str) -> HarnessSession`
- Produces: authenticated `POST /sessions/{session_id}/recover` with body `{"native_session_id": "..."}`.
- Returns: 200 with `session_id`, `status`, `recovered`, and `native_session_id`; 409 with an actionable `error` for unsupported/impossible recovery.

- [ ] **Step 1: Write failing recovery tests**

Seed an errored structured Codex row with an existing cwd and native ID, start a
fresh daemon state, POST recovery, and assert the same session ID is live,
running, and retains monotonic sequence. Add cases for Claude, already-live
idempotence, two concurrent recoveries creating one entry, Gemini, missing ID,
terminated session, and missing cwd.

```python
status, body = _json_request(
    f"{base_url}/sessions/harness-lost/recover",
    payload={"native_session_id": "thread-1"},
)
assert status == 200
assert body["session_id"] == "harness-lost"
assert state.structured.session_ids() == ["harness-lost"]
```

- [ ] **Step 2: Run recovery tests and verify RED**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q \
  tests/test_harness_daemon.py -k 'recover_structured_session'
```

Expected: 404 because the recovery route does not exist.

- [ ] **Step 3: Implement registry recovery transition**

Add one SQL update that sets `status='running'`, `ended_at=NULL`,
`last_error=NULL`, `awaiting='input'`, the native ID, and `updated_at`. This
explicit method is required because `update_session_status` deliberately uses
`COALESCE` for terminal timestamps and cannot clear them.

- [ ] **Step 4: Implement locked recovery endpoint**

Route `POST /sessions/{id}/recover` before generic action routes. Add a
per-session lock map to daemon state. Inside the lock, return idempotent success
if the manager already owns the ID; otherwise validate the local row, provider,
native ID, and cwd, start the manager with `native_session_id`, mark the row
recovered, and append a metadata-only `session.recovered` event. If start fails,
close/remove any partial manager entry and return an actionable 409.

The unsupported copy is:

```text
Session cannot be resumed after the harness restart. Continue it in a new session.
```

- [ ] **Step 5: Run recovery and daemon regression tests**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q tests/test_harness_daemon.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/drover/server/harness/registry.py \
  src/drover/server/harness/daemon.py tests/test_harness_daemon.py
git commit -m "feat(harness): recover restart-lost sessions"
```

### Task 4: Recover and retry once in central Drover

**Files:**
- Modify: `src/drover/server/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: harness `POST /sessions/{id}/recover` from Task 3.
- Produces: `_native_session_id_for_recovery(session_id: str) -> str | None`.
- Produces: turn proxy flow that performs one recovery request and one retry only for the exact pre-dispatch 404.

- [ ] **Step 1: Extend the fake harness and write failing proxy tests**

Make the fake harness return 404 with `unknown structured session` on the first
turn, accept recovery, then accept the retried turn. Assert request order is
`turns`, `recover`, `turns`, the turn body is identical, and only two turn calls
occur. Add tests proving no recovery for generic 404, 409, 500, 502, permission,
or interrupt; and actionable 409 when no native ID/recovery is unsupported.

```python
assert [request["path"] for request in requests] == [
    "/sessions/harness-running/turns",
    "/sessions/harness-running/recover",
    "/sessions/harness-running/turns",
]
assert requests[0]["body"] == requests[2]["body"]
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q \
  tests/test_metrics.py -k 'turn_recovery or does_not_recover'
```

Expected: current proxy returns the first 404 without recovery.

- [ ] **Step 3: Implement native-ID lookup**

Prefer `HarnessSession.native_session_id`; otherwise scan events newest-first
for a non-empty payload `native_session_id`. Return only a string. Do not inspect
or log message text.

- [ ] **Step 4: Implement narrow recovery and retry**

In `proxy_harness_session_action`, keep the first response. For `action ==
"turns"`, status 404, and parsed error beginning `unknown structured session:`,
call recovery with the native ID. On 2xx, mark the central row recovered,
invalidate the fleet cache, and retry the original turn once. Return all other
responses unchanged. Convert known unsupported recovery failures to the
actionable 409 copy.

- [ ] **Step 5: Run central proxy tests and verify GREEN**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q tests/test_metrics.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/drover/server/metrics.py tests/test_metrics.py
git commit -m "fix(server): recover lost sessions before retrying turns"
```

### Task 5: Lock actionable client fallback behavior

**Files:**
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`
- Modify only if the test requires it: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`

**Interfaces:**
- Consumes: central 409 error text from Task 4.
- Produces: regression coverage that preserves composer content and displays the server-authored recovery explanation.

- [ ] **Step 1: Write the client regression test**

Configure the mock client to return
`DroverError.conflict("Session cannot be resumed after the harness restart. Continue it in a new session.")`,
call `sendTurn()`, and assert `composerText` and attachments remain while `hint`
equals that text.

- [ ] **Step 2: Run the test and inspect RED/GREEN**

```bash
cd apps/drover/DroverKit
swift test --filter ChatModelTests
```

Expected: GREEN because `ChatModel.applyHint` currently surfaces 409 text.
If it is green, keep the test as a characterization test and do not change
production Swift. If it fails, make the smallest change to `applyHint` that
preserves the server text and composer state.

- [ ] **Step 3: Run all DroverKit tests**

```bash
cd apps/drover/DroverKit
swift test
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift \
  apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift
git commit -m "test(ios): preserve turns when recovery is unavailable"
```

### Task 6: Verify, review, and deploy

**Files:**
- Modify only for review fixes: files from Tasks 1-5.

**Interfaces:**
- Produces: a clean, reviewed branch and live recovery evidence.

- [ ] **Step 1: Run focused Python verification**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q \
  tests/test_harness_registry.py tests/test_structured_manager.py \
  tests/test_structured_claude.py tests/test_structured_codex.py \
  tests/test_harness_daemon.py tests/test_metrics.py
```

- [ ] **Step 2: Run the full Python suite**

```bash
/Volumes/M2\ 1/drover/.venv/bin/pytest -q
```

- [ ] **Step 3: Run static repository checks**

```bash
git diff --check
/Volumes/M2\ 1/drover/.venv/bin/python scripts/check_public_release.py
```

- [ ] **Step 4: Review the complete diff**

Verify that recovery is limited to Claude/Codex, all retries are bounded to one,
and no logs include message text. Apply only findings within this spec.

- [ ] **Step 5: Merge only after tests and review pass**

Merge `fix/session-recovery-after-redeploy` into local `main` without disturbing
other worktrees. Push/update PR #48 only if its open stacked state still matches
the intended integration route; otherwise create a dedicated recovery PR.

- [ ] **Step 6: Restart and verify live services**

Restart `com.drover.server` and `com.drover.harnessd`. Use a disposable
Claude/Codex session created before restart, send one unique turn afterward,
verify exactly one matching `user_input` on the same Drover ID and provider
output, confirm both registries report running, then terminate the disposable
session. Do not send diagnostics to the user's pre-existing session.
