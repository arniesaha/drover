# Plan 6 — `nexus-hook` Per-Harness Lifecycle

**Status:** In implementation
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §3.1, §6.1, §6.2

---

## Goal

One small CLI (`nexus-hook`) invoked by per-harness lifecycle hooks
(Claude Code SessionStart/SessionEnd, OpenClaw, etc.) that:
- on **session-start**: resolves git context, calls `nexus_handoff`,
  prints handoff text to stdout for the harness to inject into the
  agent's system context.
- on **session-end**: calls `nexus_session_close(session_id)` so the
  summarizer can pick up the job.

Per spec §3.1: 2-second hard budget. Any error / timeout → print
`(nexus offline)` to stderr and exit 0 so the agent never blocks.

---

## Module layout

```
src/nexus/hook/
  __init__.py
  context.py    # detect agent_id, repo_owner, repo_name, branch
  client.py     # tiny MCP-over-streamable-HTTP wrapper with strict timeout
  render.py     # format handoff payload as a markdown block per harness
  __main__.py   # Click CLI: session-start, session-end

scripts/claude/
  README-claude-hooks.md   # how to wire into ~/.claude/settings.json

tests/
  test_hook_context.py
  test_hook_render.py
  test_hook_cli.py
```

`pyproject.toml` adds console script `nexus-hook = "nexus.hook.__main__:main"`.

---

## Tasks

### T1. context.py

```python
@dataclass(frozen=True)
class AgentContext:
    agent_id: str
    repo_owner: Optional[str]
    repo_name: Optional[str]
    branch: Optional[str]

def detect_context(*, cwd: Path, hook_config: dict | None = None) -> AgentContext:
    """Read agent_id from ~/.nexus/hook.toml; resolve git via subprocess."""
```

Tests: a real git repo fixture (init in tmp), a non-git fixture
(returns owner/name/branch as None but agent_id from config), missing
config (defaults `agent_id = hostname-claude`).

### T2. client.py

```python
def call_tool(
    *, mcp_url: str, tool: str, args: dict,
    timeout_s: float = 2.0,
) -> dict:
    """Call a single MCP tool via streamable-HTTP; raise on timeout / error."""
```

Tests: spin up a real `build_mcp_server().run("streamable-http")` in a
thread, call it. Verify timeout path raises `HookTimeout`.

### T3. render.py

```python
def render_handoff(payload: dict) -> str:
    """Format the nexus_handoff dict into the markdown block that
    the spec §6.1 step 5 shows."""
```

Tests: snapshot test on a fixture handoff payload.

### T4. CLI: session-start + session-end

```bash
nexus-hook session-start [--cwd <path>] [--mcp-url <url>] [--timeout 2.0]
nexus-hook session-end --session-id <id> [--mcp-url <url>] [--timeout 2.0]
```

session-start: `detect_context` → `nexus_handoff` → `render_handoff` →
print to stdout. On any error: print `(nexus offline)` to stderr,
exit 0.

session-end: `nexus_session_close(session_id)`. On error: silent
exit 0 (session already over; nothing to display).

Default `mcp_url = http://127.0.0.1:7077/mcp` (from `~/.nexus/hook.toml`
`mcp_url` if set).

### T5. Claude Code wiring docs

`scripts/claude/README-claude-hooks.md` — paste-able snippet for
`~/.claude/settings.json` SessionStart/SessionEnd hooks.

---

## Acceptance

- All new tests pass.
- Existing 177 tests still pass.
- `nexus-hook session-end --session-id sX` enqueues a row in
  summarize_jobs (verified by reading DuckDB).
- `nexus-hook session-start` against a live MCP server returns within
  2 seconds OR prints `(nexus offline)` and exits 0.
- Render output includes `task_id`, summary text, and active-session
  warning when a peer is active.
