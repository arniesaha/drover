# Plan 5 — Summarizer Worker

**Status:** In implementation
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §6.2

---

## Goal

Process the `summarize_jobs` queue: for each pending job, read the
session's events, call Claude (claude-haiku-4-5-20251001) for an
LLM-generated summary, derive `files_touched` + `tools_used`
deterministically, and write a `session_summaries` row.

The MCP tool `nexus_session_close(session_id)` enqueues the job; the
SessionEnd hook (Plan 6) calls that tool.

---

## Module layout

```
src/nexus/server/summarizer/
  __init__.py
  prompt.py        # build_summary_prompt(events) → str + load template
  derive.py        # compute_files_touched / compute_tools_used
  client.py        # call_claude_summary(prompt, *, api_key, model) → dict
                   # (factored so tests inject a fake)
  worker.py        # SummarizerWorker — poll loop, pulls one job at a time

src/nexus/prompts/
  session_summary.md   # versioned prompt template

src/nexus/server/mcp/tools.py
  nexus_session_close(session_id) → enqueues a summarize_jobs row

tests/
  test_summarizer_derive.py
  test_summarizer_prompt.py
  test_summarizer_worker.py
  test_session_close_tool.py
```

---

## Tasks

### T1. Deterministic derivations

`compute_files_touched(events) → list[str]` — distinct file paths from
`Edit` / `Write` / `Bash` (heuristic for `cat <`, `tee`, `> file`)
tool_use blocks.
`compute_tools_used(events) → dict[str, int]` — counter over tool names.

Tests: pure functions over fixture event dicts.

### T2. Prompt template + builder

Template at `src/nexus/prompts/session_summary.md` with placeholders for
last 30 turns. `build_summary_prompt(events) → str` substitutes events
into the template.

Test: snapshot the prompt produced from a fixture session.

### T3. Anthropic client wrapper

```python
def call_claude_summary(
    prompt: str, *, api_key: str, model: str = "claude-haiku-4-5-20251001",
    _client: Any = None,
) -> dict:
    """Returns {summary_md, next_steps_md, open_questions, last_user_prompt, last_assistant}.
    Raises on API error. _client injection for tests."""
```

Tests use a fake client returning a canned response.

### T4. SummarizerWorker

```python
class SummarizerWorker:
    def __init__(self, *, duckdb_path, api_key, model="claude-haiku-4-5-20251001",
                 poll_interval_s=5.0, _llm_call=None):
    def start(self) -> None:
    def stop(self, timeout=5.0) -> None:
```

Poll loop:
1. `SELECT session_id FROM summarize_jobs WHERE status='pending' LIMIT 1`
2. Mark `status='in_progress'`
3. Read last 30 events from agent_events for that session
4. Compute deterministic fields
5. Call LLM (skip cleanly if api_key is None — write status='errored', message='no_api_key')
6. INSERT INTO session_summaries; mark job `status='completed'`
7. On any exception: mark job `status='errored'`, log

Tests: inject `_llm_call` fake, seed jobs + events, drain queue, assert
session_summaries rows.

### T5. nexus_session_close MCP tool

Adds to `tools.py` and registers in `server.py`. Inserts a pending
`summarize_jobs` row. Idempotent: if a row for the session already
exists in any non-`completed` state, no-op.

### T6. Wire SummarizerWorker into nexus-server run

Starts in a daemon thread. New flag `--no-summarizer`. Reads
`ANTHROPIC_API_KEY` from env; missing → worker still runs but jobs go
straight to `errored` (visible in `nexus-server status`).

---

## Acceptance

- All new tests pass.
- Existing 152 tests still pass.
- Calling `nexus_session_close('s1')` adds a row to `summarize_jobs`.
- Drain a queued job with a fake `_llm_call` → `session_summaries` has
  one row with `summary_md` populated and `tools_used` derived from
  the events.
- Without `ANTHROPIC_API_KEY`, the worker doesn't crash; the job is
  marked `errored` with a clear message.
