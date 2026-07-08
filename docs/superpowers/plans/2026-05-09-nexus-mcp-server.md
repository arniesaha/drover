# Plan 4 — `nexus-server` MCP Tool Surface

**Status:** In implementation
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §6.3

---

## Goal

Expose the seven handoff/coordination tools as MCP tools so any agent on
any host can call them. Tools are thin DuckDB queries over the same
schema Plans 1–3 created.

The actual handoff *hook* (per-harness wrapper that fires on
SessionStart) is Plan 6.

---

## Tool surface (per spec §6.3)

| Tool | Args |
|---|---|
| `nexus_handoff` | repo_owner, repo_name, branch?, task_id? |
| `nexus_session_replay` | session_id, last_n_turns=30 |
| `nexus_session_summary` | session_id |
| `nexus_active_sessions` | task_id? |
| `nexus_search` | query, task_id?, repo?, since? |
| `nexus_files_touched` | task_id, since? |
| `nexus_task_status` | task_id |

---

## Module layout

```
src/nexus/server/mcp/
  __init__.py
  tools.py     # 7 pure functions: each takes (duckdb_path, ...) and returns a dict
  server.py    # build_mcp_server(duckdb_path) -> FastMCP — registers all 7 tools

tests/
  test_mcp_tools.py
  test_mcp_server.py  # smoke: register + introspect
```

`pyproject.toml` adds `mcp>=1.0`.

`nexus-server run` starts the MCP server on `mcp_http_port` from config
in addition to the watcher and OTLP receiver. New flag `--no-mcp`.

---

## Tasks (TDD)

### T1. tools.py — DuckDB query functions

**Test:** `tests/test_mcp_tools.py`

Implement 7 functions, each accepting `duckdb_path: Path` plus the
spec'd args. Return dicts that serialize cleanly through MCP (no
datetimes — convert to ISO strings; no Decimal — convert to float).

Tests use a tmp DuckDB seeded with synthetic agent_events, spans,
session_summaries, and tasks rows. One test per tool.

### T2. server.py — FastMCP registration

**Test:** `tests/test_mcp_server.py`

```python
def build_mcp_server(*, duckdb_path: Path, name: str = "nexus") -> FastMCP:
    mcp = FastMCP(name)
    @mcp.tool()
    def nexus_handoff(...): ...
    # (and the rest)
    return mcp
```

Smoke tests:
- Server has the 7 expected tools registered (introspect via FastMCP).
- Tool descriptions are non-empty.

### T3. Wire MCP server into `nexus-server run`

`nexus-server run` starts an HTTP transport on `cfg.mcp_http_port`. Adds
`--no-mcp` flag. Failure to bind logs and continues.

Initial implementation uses `mcp.server.fastmcp.FastMCP.run(transport="streamable-http")`
in a daemon thread.

### T4. README + plan pointer

Update README status to mention Plan 4 shipped.

---

## Acceptance

- All new tests pass.
- Existing 140 tests still pass.
- `nexus-server run --help` exposes `--no-mcp`.
- Calling each tool function against an empty lakehouse returns a
  well-formed empty result (no exceptions).
- Calling `nexus_handoff` against a task with one summary returns that
  summary in the payload.
