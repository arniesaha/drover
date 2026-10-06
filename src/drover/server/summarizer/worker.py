"""SummarizerWorker — drains ``summarize_session`` ledger jobs into session memory.

Designed for both threaded poll-loop use (start/stop) and one-shot
drain (drain_once for tests / explicit catch-up runs).

Jobs live in the PostgreSQL job ledger (#480); the session events they read
stay in the analytical DuckDB. Each claimed job:
  1. Lease it from the ledger (``claim``; the lease token fences everything after).
  2. Read session events from agent_events.
  3. Compute deterministic fields (files_touched, tools_used).
  4. Build prompt → call backend → parse JSON.
  5. In ONE PostgreSQL transaction: ``complete`` the lease, write the final
     phase of ``session_memory``, and enqueue the embed job (and the brief job
     when the session has a project). If ``complete`` finds the lease gone --
     expired and reclaimed, or superseded by a newer source generation -- the
     transaction rolls back and the result is discarded, so a stale generation
     can never overwrite a newer one or fan out downstream work.
On failure the ledger spends one attempt: retry with backoff, dead-letter when
the budget is spent, or quarantine a non-retryable input fault (validation
failures, a session with no events). A missing API key is not the job's fault:
the lease is released without spending an attempt.

There is no startup recovery pass any more: a worker that dies mid-job leaves
an expiring lease, and the next ``claim`` reclaims it (spending one failure,
so a job that keeps killing its worker dead-letters instead of looping).

Backend selection prefers the claude-code CLI unless Anthropic credentials are
configured under a cloud/hybrid policy. See ``backends.select_backend``.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.db import open_duckdb_connection
from drover.server.ledger import (
    BRIEF_PROJECT,
    EMBED_SESSION,
    SUMMARIZE_SESSION,
    ClaimedJob,
    JobLedger,
    memory_store_available,
    transaction,
)
from drover.server.memory_store import MemoryRepository, SessionSummary
from drover.server.summarizer.backends import (
    BackendError,
    LLMBackend,
    SummarizerBackendConfig,
    select_backend,
)
from drover.server.summarizer.client import (
    DEFAULT_MODEL,
    NoApiKeyError,
    SummarizerClientError,
    call_claude_summary,
)
from drover.server.summarizer.derive import compute_files_touched, compute_tools_used
from drover.server.summarizer.prompt import build_summary_prompt
from drover.server.summarizer.retry import classify_summarize_error

log = logging.getLogger("drover.summarizer.worker")

#: How long a job waits after its lease is released because no API key is
#: configured. Releasing does not spend an attempt, so a hub without
#: credentials never burns a session's budget -- but every claim still writes an
#: attempt row, so the job must not come back every poll tick either.
UNCONFIGURED_RELEASE_SECONDS = 300.0


class _BackendUnavailable(RuntimeError):
    """The worker cannot call a model at all (no API key): not the job's fault."""


class _LeaseLost(Exception):
    """``complete`` found the lease gone; roll the success transaction back."""


def _open_summarizer_db(duckdb_path: Path) -> duckdb.DuckDBPyConnection:
    from drover.server.lake.serving import open_history, selected_config

    if selected_config(duckdb_path).backend == "ducklake":
        return open_history(duckdb_path)
    return open_duckdb_connection(duckdb_path, role="summarizer")


def _session_agent_events_ctes(*, lower: bool = False, upper: bool = False) -> str:
    """The session's canonical events, optionally within one time slice.

    ``lower`` binds ``timestamp >= ?`` and ``upper`` binds ``timestamp < ?``
    (NULL timestamps, oldest in every order, fall in the slice with no lower
    bound), in that order after the session id. The bounds sit on the session
    scan, before the dedup window, so the scan prunes on them.
    """
    bounds = ""
    if lower:
        bounds += " AND timestamp >= ?::TIMESTAMPTZ"
    if upper:
        bounds += " AND (timestamp < ?::TIMESTAMPTZ OR timestamp IS NULL)"
    return f"""
session_agent_events AS (
  SELECT *
  FROM agent_events
  WHERE session_id = ?{bounds}
),
{canonical_agent_events_cte(source="session_agent_events")}
""".strip()


def _describe(exc: BaseException) -> str:
    """The failure text for the log and the job: a lake code plus its cause."""
    detail = getattr(exc, "detail", None)
    return f"{exc}: {detail}" if detail else str(exc)


def _classify_failure(exc: BaseException) -> tuple[bool, str]:
    """(retryable, category) for one failed summarize attempt.

    Validation failures (the model returned the wrong shape) and a session
    with no events are input faults: replaying the same input buys the same
    answer, so they are quarantined with the reason instead of spending the
    rest of the budget. Everything else -- auth, rate limits, backend
    availability, and errors nobody has classified yet -- is retried with
    backoff and dead-letters when the budget runs out.
    """
    message = _describe(exc)
    if message.startswith("no events for session"):
        return False, "no_events"
    category = classify_summarize_error(message)["category"]
    if category == "validation":
        return False, "validation"
    return True, category


# Tool facts (files touched, tools used) are derived from at most this many of
# a session's most recent raw events. A session with more stored events than
# this is summarized from its tail, and the summary says so.
MAX_RAW_EVENTS_PER_SESSION = 25000

# Every lake read is one query child with a 5 s deadline and a 2 GiB RSS cap,
# and scanning a session through the serving view costs both in proportion to
# its size (prod, 578k events: 1.8 s and 1.05 GB per full-session read). Reads
# of a long session are therefore bounded by timestamp slices of this many
# stored events, which the scan prunes on, so each child's cost stays flat
# (prod: a 100k slice is 0.9 s warm but 3.5 s on a cold file cache; 50k keeps
# a cold slice well inside the deadline).
SESSION_SLICE_EVENTS = 50_000
# Prompt turns are searched for, newest slice first, at most this far back. A
# longer session without a substantive turn in that span is an input fault.
MAX_PROMPT_SCAN_EVENTS = 2_000_000
PROMPT_TURNS = 30

# Leave headroom below the one MiB process reply ceiling for JSON framing,
# result metadata, and a pathological page's final row.
RAW_EVENT_PAGE_BYTES = 768 * 1024
RAW_EVENT_PAGE_ROWS = 1000


# Every raw field the tool projection reads, extracted by ONE multi-path
# ``json_extract_string`` call so each event is parsed once. Extracting them
# one call at a time parsed each event ~40 times per vector, which pushed the
# query child past its 768 MB DuckDB memory limit on an active Claude Code
# session whose tail holds megabyte-sized events (prod, Oct 2026; the tail's
# raw_data is only 17 MB in total). Invalid JSON is caught by ``try()``, not
# ``CASE WHEN json_valid(...)``: on DuckDB 1.5.5 a JSON call inside that CASE
# exhausts the limit on the same rows even when it is the only one.
_TOOL_PATHS = (
    "$.tool_name",
    "$.name",
    "$.tool.name",
    "$.tool_use_blocks[0].name",
    "$.tool",
    "$.tool_use_blocks",
    "$.path",
    "$.file_path",
    *(
        f"$.{base}.{key}"
        for base in ("tool.input", "tool.arguments", "input", "arguments")
        for key in ("path", "file_path", "patch", "command", "text")
    ),
)


def _tool_projection_sql() -> str:
    """A bounded raw-data projection sufficient for deterministic derivations.

    Tool names and file paths are the only raw fields the post-summary
    derivations consume. ``derived_files`` retains paths encoded in patch
    commands without sending the command itself through the bounded child
    reply. The fields are extracted once into a list that a single-element
    ``list_transform`` binds to ``f``, so the expression stays scalar.
    """

    def field(path: str) -> str:
        return f"f[{_TOOL_PATHS.index(path) + 1}]"

    def first(*paths: str) -> str:
        return "coalesce(" + ", ".join(field(p) for p in paths) + ")"

    patch_files = ",\n        ".join(
        f"coalesce(regexp_extract_all({field(f'$.{base}.{key}')}, "
        "'\\*\\*\\* (?:Add|Update|Delete) File: ([^\\n]+)', 1), []::VARCHAR[])"
        for base in ("tool.input", "tool.arguments", "input", "arguments")
        for key in ("patch", "command", "text")
    )
    paths = ", ".join(f"'{p}'" for p in _TOOL_PATHS)
    return f"""
    list_transform(
      list_filter([try(json_extract_string(raw_data, [{paths}]))], f -> f IS NOT NULL),
      f -> json_object(
      'tool_name', {first('$.tool_name', '$.name', '$.tool.name', '$.tool_use_blocks[0].name')},
      'tool_use_blocks', json_transform(
        {field('$.tool_use_blocks')},
        '[{{"name": "VARCHAR", "input": {{"path": "VARCHAR", "file_path": "VARCHAR"}}}}]'
      ),
      'tool', json_object(
        'name', {first('$.tool.name', '$.tool_name', '$.name', '$.tool')},
        'input', json_object(
          'path', {first('$.tool.input.path', '$.tool.arguments.path', '$.input.path', '$.arguments.path')},
          'file_path', {first('$.tool.input.file_path', '$.tool.arguments.file_path', '$.input.file_path', '$.arguments.file_path')}
        )
      ),
      'input', json_object(
        'path', {field('$.input.path')},
        'file_path', {field('$.input.file_path')}
      ),
      'arguments', json_object(
        'path', {field('$.arguments.path')},
        'file_path', {field('$.arguments.file_path')}
      ),
      'path', {first('$.path', '$.file_path')},
      'file_path', {field('$.file_path')},
      'derived_files', to_json(list_concat(
        {patch_files}
      ))
    )::VARCHAR)[1]
    """


def _is_lake(con) -> bool:
    from drover.server.lake.serving import HistoryConnection

    return isinstance(con, HistoryConnection)


def _session_bounds_sql(relation: str) -> str:
    """Timestamps of the newest stored events at the tail and slice boundaries.

    Row ``n`` is the ``n``-th newest stored event of the session, over physical
    rows: these are only scan bounds, and every bounded read applies the full
    serving and dedup semantics within them. Only the narrow timestamp column
    is read, so on the lake this runs against the base table rather than the
    serving view, whose per-row JSON costs ~10x as much (6.7 s vs 0.5 s for a
    GROUP BY over 3.3M prod events).
    """
    return f"""SELECT n, ts, total FROM (
      SELECT ts, row_number() OVER (ORDER BY ts DESC) AS n,
             count(*) OVER () AS total
      FROM (SELECT TRY_CAST(timestamp AS TIMESTAMPTZ) AS ts FROM {relation}
            WHERE session_id = ?)
      WHERE ts IS NOT NULL
    ) WHERE n = ? OR n % ? = 0 ORDER BY n LIMIT ?"""


def _session_bounds(con, session_id: str) -> tuple[Any, list[Any], int, bool]:
    """``(tail_lower, slice_lowers, stored_events, capped)`` for one session.

    ``tail_lower`` bounds the newest ``MAX_RAW_EVENTS_PER_SESSION`` stored
    events (None: the session is not longer than that). ``slice_lowers`` are
    the newest-first lower bounds of consecutive ``SESSION_SLICE_EVENTS``
    slices ([] : one unbounded read suffices); ``capped`` means history older
    than ``MAX_PROMPT_SCAN_EVENTS`` is never read.
    """
    max_slices = MAX_PROMPT_SCAN_EVENTS // SESSION_SLICE_EVENTS
    relation = "lake.agent_events" if _is_lake(con) else "agent_events"
    rows = con.execute(
        _session_bounds_sql(relation),
        [session_id, MAX_RAW_EVENTS_PER_SESSION, SESSION_SLICE_EVENTS, max_slices + 2],
    ).fetchall()
    tail_lower = next(
        (ts for n, ts, _ in rows if n == MAX_RAW_EVENTS_PER_SESSION), None
    )
    slices = [ts for n, ts, _ in rows if n % SESSION_SLICE_EVENTS == 0]
    stored = rows[0][2] if rows else 0
    return tail_lower, slices[:max_slices], stored, len(slices) > max_slices


def _is_final_assistant(event: dict) -> bool:
    return event.get("role") == "assistant" and event.get("event_type") not in (
        "tool_call",
        "tool_action",
        "tool_result",
    )


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _turn_order(event: dict):
    """``ORDER BY timestamp, id`` (NULLS LAST), as the window query orders."""
    ts = event.get("timestamp")
    return (ts is None, ts or _EPOCH, event.get("id") or "")


def _slice_reads(slices: list[Any], capped: bool):
    """``(ctes, bound params)`` per read, newest slice first; [] is one read."""
    if not slices:
        yield _session_agent_events_ctes(), []
        return
    upper = None
    for lower in slices + ([] if capped else [None]):
        yield (
            _session_agent_events_ctes(
                lower=lower is not None, upper=upper is not None
            ),
            [b for b in (lower, upper) if b is not None],
        )
        upper = lower


def _first_in_slices(
    con, session_id: str, select: str, slices: list[Any], capped: bool
) -> Optional[tuple]:
    """First row whose first column is set, reading slices newest first.

    For ``any_value`` lookups (task id, repository): any slice's answer is a
    valid answer, so a long session stops at its newest slice that has one.
    """
    row = None
    for ctes, bounds in _slice_reads(slices, capped):
        row = con.execute(f"WITH {ctes} {select}", [session_id, *bounds]).fetchone()
        if row and row[0] is not None:
            return row
    return row


def _read_prompt_window(
    con, session_id: str, slices: list[Any], capped: bool
) -> list[dict]:
    """The prompt's substantive turns, walking time slices newest first.

    Without slices this is one ``select_substantive_window`` read of the whole
    session, exactly as before. Otherwise each slice contributes its own
    window; slices are disjoint in time, so once newer slices hold
    ``PROMPT_TURNS`` turns and a final assistant reply, older ones cannot
    change the result. A logical event duplicated across a slice boundary is
    kept once (newest slice wins) rather than ranked against its older copy.
    """
    from drover.server.summarizer.derive import select_substantive_window

    if not slices:
        return select_substantive_window(
            con, _session_agent_events_ctes(), session_id, PROMPT_TURNS
        )
    found: dict[Any, dict] = {}
    for ctes, bounds in _slice_reads(slices, capped):
        for event in select_substantive_window(
            con, ctes, session_id, PROMPT_TURNS, bounds=bounds
        ):
            key = event.get("dedup_key") or (event.get("id"), event.get("timestamp"))
            found.setdefault(key, event)
        if len(found) >= PROMPT_TURNS and any(
            _is_final_assistant(e) for e in found.values()
        ):
            break
    newest = sorted(
        found.values(),
        key=lambda e: (e.get("timestamp") is not None, *_turn_order(e)[1:]),
        reverse=True,
    )
    window = newest[:PROMPT_TURNS]
    final = next((e for e in newest if _is_final_assistant(e)), None)
    if final is not None and final not in window:
        window.append(final)
    return sorted(window, key=_turn_order)


def _tool_facts_sql(*, lower: bool, after: bool) -> str:
    """Distinct tool facts of the session's newest raw events, with counts.

    Choose the newest ``MAX_RAW_EVENTS_PER_SESSION`` canonical raw events on
    the raw rows first, then run the JSON/regex tool projection over at most
    that many rows (projecting before ``LIMIT`` costs ~1.6 GB of expression
    memory for a 40k-event session), then collapse identical projections into
    one ``(event_type, raw_data, n)`` group. The derivations only count and
    collect, so groups lose nothing, and a tail of thousands of events is
    typically a handful of rows: one child instead of one per 1,000 events.
    Groups page by keyset on ``(event_type, raw_data)``.
    """
    keyset = " WHERE (event_type, raw_data) > (?, ?)" if after else ""
    return f"""WITH {_session_agent_events_ctes(lower=lower)},
    recent_events AS (
      SELECT event_type, raw_data FROM canonical_agent_events
      WHERE raw_data IS NOT NULL
      ORDER BY timestamp DESC, coalesce(id, '') DESC,
               coalesce(dedup_key, '') DESC, hash(raw_data) DESC
      LIMIT ?
    ),
    tool_facts AS (
      SELECT coalesce(event_type, '') AS event_type,
             coalesce({_tool_projection_sql()}, '') AS raw_data
      FROM recent_events
    )
    SELECT event_type, raw_data, count(*) AS n FROM tool_facts{keyset}
    GROUP BY event_type, raw_data ORDER BY event_type, raw_data LIMIT ?"""


def _read_tool_facts(
    con, session_id: str, tail_lower: Any
) -> tuple[list[str], dict[str, int]]:
    """``(files_touched, tools_used)`` over the session's newest raw events."""
    from collections import Counter

    files: set[str] = set()
    tools: Counter[str] = Counter()
    after: list[str] = []
    bound = [] if tail_lower is None else [tail_lower]
    sql = {
        flag: _tool_facts_sql(lower=tail_lower is not None, after=flag)
        for flag in (False, True)
    }
    while True:
        params = [session_id, *bound, MAX_RAW_EVENTS_PER_SESSION, *after]
        rows = _execute_raw_event_page(
            con, sql[bool(after)], params + [RAW_EVENT_PAGE_ROWS]
        ).fetchall()
        for event_type, raw_data, n in rows:
            event = [{"event_type": event_type, "raw_data": raw_data}]
            files.update(compute_files_touched(event))
            for name, count in compute_tools_used(event).items():
                tools[name] += count * n
        if len(rows) < RAW_EVENT_PAGE_ROWS:
            return sorted(files), dict(tools)
        after = [rows[-1][0], rows[-1][1]]


def _execute_raw_event_page(con, sql: str, params: list[Any]):
    """Execute one page with an explicit reply budget on the lake facade."""
    if _is_lake(con):
        from drover.server.lake.query_process import QueryLimits

        return con.execute(
            sql,
            params,
            limits=QueryLimits(bytes=RAW_EVENT_PAGE_BYTES, rows=RAW_EVENT_PAGE_ROWS),
        )
    return con.execute(sql, params)


class SummarizerWorker:
    def __init__(
        self,
        *,
        duckdb_path: Path,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        poll_interval_s: float = 5.0,
        _llm_call: Optional[Callable[..., dict]] = None,
        backend: Optional[LLMBackend] = None,
        backend_config: Optional[SummarizerBackendConfig] = None,
        job_kind: str = "incremental",
        batch_size: int = 1,
        worker_id: str = "summarizer",
        _jitter: Callable[[float, float], float] = random.uniform,
        _before_success_effects: Callable[[], None] = lambda: None,
        _before_failure_finish: Callable[[], None] = lambda: None,
        _after_completion_commit: Callable[[], None] = lambda: None,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.api_key = api_key
        self.model = model
        self.poll_interval_s = poll_interval_s
        self._llm_call = _llm_call or call_claude_summary
        self._backend = backend
        self._backend_config = backend_config
        self.job_kind = job_kind
        self.batch_size = max(1, int(batch_size))
        self.worker_id = worker_id
        self._jitter = _jitter
        self._before_success_effects = _before_success_effects
        self._before_failure_finish = _before_failure_finish
        self._after_completion_commit = _after_completion_commit
        self._unavailable_logged = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _resolve_backend(self) -> Optional[LLMBackend]:
        """Return an explicit backend, or pick one from config — or None.

        ``None`` means fall back to ``self._llm_call`` (legacy path used by
        existing tests). Errors during selection are logged and swallowed
        so the worker can still mark jobs errored cleanly.
        """
        if self._backend is not None:
            return self._backend
        if self._backend_config is None:
            return None
        try:
            return select_backend(job_kind=self.job_kind, config=self._backend_config)
        except BackendError as e:
            log.warning("backend selection failed: %s", e)
            return None

    def _ledger(self) -> Optional[JobLedger]:
        """The job ledger, or None when derived memory is unavailable.

        A hub still on the DuckDB control store has no ledger: the worker
        idles (logging that once) instead of crashing its poll loop.
        """
        if not memory_store_available(self.duckdb_path):
            if not self._unavailable_logged:
                log.info(
                    "summarizer idle: derived memory requires the PostgreSQL "
                    "control store (control_store.backend = 'postgres')"
                )
                self._unavailable_logged = True
            return None
        return JobLedger(self.duckdb_path, jitter=self._jitter)

    # --- public lifecycle ---

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-summarizer", daemon=True
        )
        self._thread.start()
        log.info("summarizer worker started (model=%s)", self.model)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001
                log.exception("summarizer drain loop crashed (will retry)")
            self._stop.wait(self.poll_interval_s)

    # --- core ---

    def drain_once(self) -> int:
        """Process one due job. Returns 1 if a job was handled, 0 otherwise."""
        return self.drain_batch(max_jobs=1)

    def drain_batch(self, *, max_jobs: Optional[int] = None) -> int:
        """Process up to ``max_jobs`` due jobs back-to-back.

        Defaults to ``self.batch_size``. Returns the number of jobs
        processed (≥0). Intended for use with a local backend so the
        WoL cold-start amortizes across the batch.
        """
        if max_jobs is None:
            max_jobs = self.batch_size
        ledger = self._ledger()
        if ledger is None:
            return 0

        # Check for due jobs BEFORE selecting a backend so that backend
        # fallback warnings are never emitted on idle poll ticks (fixes #55).
        if not ledger.has_due(SUMMARIZE_SESSION):
            return 0

        backend = self._resolve_backend()
        # Pre-warm: if it's an Ollama-style backend, do the wake check now
        # so all jobs in the batch land warm.
        if backend is not None and hasattr(backend, "ensure_ready"):
            try:
                backend.ensure_ready()
            except BackendError as e:
                log.warning("backend ensure_ready failed: %s", e)
                # Fall through; per-job summarize will raise & mark errored.

        processed = 0
        for _ in range(max_jobs):
            # One at a time: a lease taken for the whole batch up front would
            # sit expiring behind every model call ahead of it.
            claimed = ledger.claim(SUMMARIZE_SESSION, worker_id=self.worker_id, limit=1)
            if not claimed:
                break
            outcome = self._process_job(ledger, claimed[0], backend)
            processed += 1
            if outcome == "released":
                # Every job would hit the same missing credential.
                break
        return processed

    def _process_job(
        self, ledger: JobLedger, job: ClaimedJob, backend: Optional[LLMBackend]
    ) -> str:
        """Run one leased job to a ledger transition; return what happened."""
        session_id = job.subject_key
        try:
            written = self._summarize_session(ledger, job, backend)
        except _BackendUnavailable as exc:
            log.warning(
                "summarize %s deferred: %s (lease released, no attempt spent)",
                session_id,
                exc,
            )
            ledger.release(
                job, delay_seconds=UNCONFIGURED_RELEASE_SECONDS, reason=str(exc)
            )
            return "released"
        except Exception as exc:  # noqa: BLE001
            log.exception("summarize %s failed: %s", session_id, _describe(exc))
            return self._finish_failure(ledger, job, exc)

        if not written:
            log.info(
                "summary for %s (source %s) discarded: lease lost or superseded",
                session_id,
                job.source_version,
            )
            return "stale"
        self._after_completion_commit()
        return "succeeded"

    def _finish_failure(
        self, ledger: JobLedger, job: ClaimedJob, exc: BaseException
    ) -> str:
        """Spend one failure of the leased generation's budget."""
        self._before_failure_finish()
        retryable, category = _classify_failure(exc)
        outcome = ledger.fail(
            job, _describe(exc), retryable=retryable, category=category
        )
        if outcome == "stale":
            # Superseded or reclaimed while the model ran: the newer owner
            # keeps its budget, and this failure is simply dropped.
            log.info(
                "failure for %s (source %s) dropped: lease lost or superseded",
                job.subject_key,
                job.source_version,
            )
        elif outcome in ("dead_lettered", "quarantined"):
            log.warning(
                "summarize %s %s (%s): %s",
                job.subject_key,
                outcome,
                category,
                _describe(exc),
            )
        return outcome

    def _summarize_session(
        self,
        ledger: JobLedger,
        job: ClaimedJob,
        backend: Optional[LLMBackend] = None,
    ) -> bool:
        """Summarize one leased generation. False when the result was discarded."""
        session_id = job.subject_key
        con = _open_summarizer_db(self.duckdb_path)
        try:
            tail_lower, slices, stored, capped = _session_bounds(con, session_id)
            events = _read_prompt_window(con, session_id, slices, capped)
            if not events:
                scope = (
                    f" in its most recent {MAX_PROMPT_SCAN_EVENTS} events"
                    if capped
                    else ""
                )
                raise RuntimeError(
                    f"no events for session {session_id}: no substantive turns{scope}"
                )
            files, tools = _read_tool_facts(con, session_id, tail_lower)
        finally:
            con.close()
        truncation = None
        if tail_lower is not None:
            log.warning(
                "Truncated session %s to %d raw events for summarization "
                "(%d stored events)",
                session_id,
                MAX_RAW_EVENTS_PER_SESSION,
                stored,
            )
            truncation = (
                f"Files and tools are derived from the most recent "
                f"{MAX_RAW_EVENTS_PER_SESSION:,} of this session's "
                f"{stored:,} stored events."
            )

        agent_id = events[-1].get("agent_id") or "unknown"

        last_user = next(
            (e["content"] for e in reversed(events) if e.get("role") == "user"), ""
        )
        last_assistant = next(
            (
                e["content"]
                for e in reversed(events)
                if e.get("role") == "assistant"
                and e.get("event_type")
                not in ("tool_call", "tool_action", "tool_result")
                and e.get("content")
            ),
            "",
        )

        prompt = build_summary_prompt(
            events=[
                {
                    "role": e.get("role"),
                    "content": e.get("content"),
                    "timestamp": _iso(e.get("timestamp")),
                    "event_type": e.get("event_type"),
                }
                for e in events
            ],
            session_id=session_id,
            agent_id=agent_id,
            started_at=_iso(events[0].get("timestamp")),
            ended_at=_iso(events[-1].get("timestamp")),
        )

        if backend is not None:
            try:
                llm = backend.summarize(prompt)
            except BackendError as e:
                raise RuntimeError(str(e)) from e
            generator_model = backend.model
        else:
            try:
                llm = self._llm_call(prompt, api_key=self.api_key, model=self.model)
            except NoApiKeyError:
                raise _BackendUnavailable(
                    "ANTHROPIC_API_KEY not configured (no_api_key)"
                )
            generator_model = self.model

        # Preserve exact final commit/issue evidence even if the model omits it.
        from drover.server.summarizer.derive import final_references

        refs = final_references(last_assistant)
        missing_refs = [ref for ref in refs if ref not in llm["summary_md"]]
        if missing_refs:
            llm["summary_md"] += "\n\nFinal references: " + ", ".join(missing_refs)
        if truncation:
            llm["summary_md"] += "\n\n_" + truncation + "_"

        # The test seam is deliberately before the single completion transaction:
        # any superseding generation either wins first and makes this stale, or is
        # ordered after all durable success effects commit together.
        self._before_success_effects()

        # Everything the success transaction needs from DuckDB is read first,
        # so the PostgreSQL transaction holds its row locks for writes only.
        con = _open_summarizer_db(self.duckdb_path)
        try:
            task_id = events[0].get("has_raw_data") and _safe_task_id(
                con, session_id, slices, capped
            )
            project_row = _first_in_slices(
                con,
                session_id,
                """SELECT any_value(repo_owner), any_value(repo_name)
                   FROM canonical_agent_events
                   WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL""",
                slices,
                capped,
            )
        finally:
            con.close()
        project_key = (
            f"{project_row[0]}/{project_row[1]}"
            if project_row and project_row[0] and project_row[1]
            else None
        )
        summary = SessionSummary(
            session_id=session_id,
            summary_md=llm["summary_md"],
            next_steps_md=llm["next_steps_md"],
            task_id=task_id,
            agent_id=agent_id,
            project_key=project_key,
            ended_at=_as_utc(events[-1].get("timestamp")),
            files_touched=tuple(files),
            tools_used=tools,
            open_questions=tuple(llm.get("open_questions") or []),
            last_user_prompt=(llm.get("last_user_prompt") or last_user or "")[-500:],
            last_assistant=(llm.get("last_assistant") or last_assistant or "")[-500:],
            status="completed",
            source_version=job.source_version,
            generator_model=generator_model,
        )

        # One generation-fenced transaction for every durable success effect.
        # ``complete`` matches on this claim's lease token: if a newer source
        # generation superseded the job, or the lease expired and another
        # worker reclaimed it, it returns False and nothing below happens.
        try:
            with ledger.connection() as pg, transaction(pg):
                if not ledger.complete(job, con=pg):
                    raise _LeaseLost()
                MemoryRepository.put_summary(pg, summary)
                ledger.enqueue(
                    EMBED_SESSION,
                    session_id,
                    source_version=job.source_version,
                    con=pg,
                )
                if project_key is not None:
                    ledger.enqueue(
                        BRIEF_PROJECT,
                        project_key,
                        source_version=f"{session_id}:{job.source_version}",
                        payload={
                            "source_session_id": session_id,
                            "source_version": job.source_version,
                        },
                        con=pg,
                    )
        except _LeaseLost:
            return False
        return True


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _as_utc(ts: Any) -> Any:
    """A naive DuckDB timestamp is UTC; say so before it reaches TIMESTAMPTZ."""
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _safe_task_id(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    slices: Sequence[Any] = (),
    capped: bool = False,
) -> Optional[str]:
    try:
        row = _first_in_slices(
            con,
            session_id,
            "SELECT any_value(task_id) FROM canonical_agent_events",
            list(slices),
            capped,
        )
        return row[0] if row else None
    except duckdb.Error:
        return None
