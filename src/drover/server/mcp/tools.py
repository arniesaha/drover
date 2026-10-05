"""MCP tool implementations.

These are pure functions over a DuckDB lakehouse path: each opens its
own connection, executes a query, and returns a JSON-serializable dict.
The MCP transport layer (server.py) thinly wraps them; tests exercise
them directly so we can validate behavior without a transport.

Derived memory -- session summaries, project briefs and session embeddings --
is read from the PostgreSQL control store registered for the same path
(:mod:`drover.server.memory_store`, #480). Events, tasks and the other
analytical relations still come from DuckDB. With a DuckDB control store
there is no derived memory at all: the tools answer with empty summaries
rather than failing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import duckdb

from drover.context_containers import normalize_context_type
from drover.event_identity import canonical_agent_events_cte
from drover.server.db import attached_control_plane_snapshot, open_duckdb_connection
from drover.server.ledger import (
    SUMMARIZE_SESSION,
    JobLedger,
    MemoryStoreUnavailable,
    memory_store_available,
)
from drover.server.mcp.contract import READ_CAPS, bounded_read
from drover.server.memory_store import (
    EmbeddingStore,
    MemoryRepository,
    SessionSummary,
    VectorStoreUnavailable,
)
from drover.server.observatory import pipeline_observatory_snapshot
from drover.server.project_activity import MAX_DAYS as PROJECT_ACTIVITY_MAX_DAYS
from drover.server.project_activity import project_activity
from drover.server.quality import quality_snapshot
from drover.server.summarizer.jobs import (
    enqueue_summary_generation,
    source_version_for_session,
)
from drover.task_id import compute_task_id

log = logging.getLogger("drover.mcp.tools")

#: Ingest placeholders that are never a real session; summaries keyed on them
#: are noise in every handoff.
_PLACEHOLDER_SESSIONS = frozenset({"unknown_openclaw"})

#: Bound on the repo -> task fan-out when resolving a repository's summaries
#: through ``tasks`` (one task per branch, plus explicit task ids).
_REPO_TASK_LOOKUP_LIMIT = 50

#: Newest summaries considered when deciding whether a project brief is stale.
_BRIEF_FRESHNESS_SUMMARIES = 50

#: Sessions a repo-scoped recall searches over (newest summaries first).
_RECALL_SCOPE_LIMIT = 1000

MEMORY_UNAVAILABLE_REASON = (
    "derived memory (summaries, briefs, embeddings) requires the PostgreSQL "
    "control store; this hub runs a DuckDB control store"
)

# Per-tool summary row shapes (the columns each tool used to select from the
# DuckDB session_summaries table).
_HANDOFF_SUMMARY_KEYS = (
    "session_id",
    "agent_id",
    "ended_at",
    "summary_md",
    "next_steps_md",
    "open_questions",
    "files_touched",
    "status",
    "generator_model",
)
_SESSION_SUMMARY_KEYS = (
    "session_id",
    "task_id",
    "agent_id",
    "ended_at",
    "summary_md",
    "files_touched",
    "tools_used",
    "last_user_prompt",
    "last_assistant",
    "next_steps_md",
    "open_questions",
    "status",
    "generator_model",
    "generated_at",
    "project_key",
    "source_version",
)
_RECENT_SESSION_KEYS = (
    "session_id",
    "agent_id",
    "ended_at",
    "summary_md",
    "next_steps_md",
    "open_questions",
    "files_touched",
    "generator_model",
)
_RESUME_SUMMARY_KEYS = (
    "session_id",
    "agent_id",
    "ended_at",
    "summary_md",
    "next_steps_md",
    "open_questions",
    "status",
    "generator_model",
)
_TASK_SUMMARY_KEYS = ("session_id", "agent_id", "summary_md", "ended_at")
_BRIEF_KEYS = (
    "project_key",
    "repo_owner",
    "repo_name",
    "brief_md",
    "recent_themes_md",
    "key_files",
    "open_questions",
    "next_steps_md",
    "session_count",
    "last_activity_at",
    "generator_model",
    "generated_at",
)


def _connect(duckdb_path: Path) -> duckdb.DuckDBPyConnection:
    # Read-write open even for read paths: a read_only connection has a
    # different config and DuckDB rejects it beside live writers (issue #2).
    return open_duckdb_connection(duckdb_path, role="diagnostic")


def _history_connect(path: Path):
    from drover.server.lake.serving import open_history, selected_config

    if selected_config(path).backend == "legacy":
        return _connect(path)
    return open_history(path)


def _history_time_bound(path: Path, value: str) -> str:
    """Preserve legacy host-local interpretation of naive SQL timestamp bounds.

    The lake engine is UTC. Carry an explicit offset instead of changing the
    authoritative legacy result around midnight or DST transitions.
    """
    from drover.server.lake.serving import selected_config

    if selected_config(path).backend == "ducklake":
        parsed = _parse_datetime(value)
        if parsed is not None and parsed.tzinfo is None:
            return parsed.astimezone().isoformat()
    return value


def _canonical_read(function):
    """A selected lake error is explicit unavailable, never a legacy retry."""
    from functools import wraps

    from drover.server.lake.runtime import LakeError
    from drover.server.lake.serving import check_selected, selected_config

    @wraps(function)
    def guarded(*args, **kwargs):
        path = kwargs["duckdb_path"]
        config = selected_config(path)
        if config.backend == "legacy":
            return function(*args, **kwargs)
        try:
            check_selected(path)
            return function(*args, **kwargs)
        except (LakeError, duckdb.Error, OSError) as exc:
            return {
                "status": "unavailable",
                "analytics_backend": "ducklake",
                "analytics_epoch": config.epoch,
                "reason": (
                    exc.code if isinstance(exc, LakeError) else "analytics_unavailable"
                ),
            }

    return guarded


def _row_to_dict(cursor: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [
        {col: _coerce(value) for col, value in zip(cols, row)}
        for row in cursor.fetchall()
    ]


def _coerce(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _resolve(duckdb_path: Path, session_id: str, *, history: bool = False) -> dict:
    from drover.server.memory_identity import resolve_session

    try:
        con = _history_connect(duckdb_path) if history else _connect(duckdb_path)
        try:
            return resolve_session(con, session_id, store_path=duckdb_path)
        finally:
            con.close()
    except (duckdb.Error, OSError):
        return {"status": "unavailable", "session_id": session_id}


def _missing(resolution: dict) -> dict:
    return {**resolution, "status": "unavailable"}


def drover_memory_acceptance(*, duckdb_path: Path, harness_ids: list[str]) -> dict:
    """Read-only evidence report for up to 25 explicitly selected sessions."""
    from drover.server.memory_audit import audit_session

    if len(harness_ids) > 25:
        raise ValueError("at most 25 harness IDs per audit read")
    try:
        con = _connect(duckdb_path)
        try:
            reports = []
            for sid in harness_ids:
                try:
                    reports.append(audit_session(con, sid, store_path=duckdb_path))
                except duckdb.Error:
                    reports.append({"harness_id": sid, "status": "unavailable"})
            return {"sessions": reports}
        finally:
            con.close()
    except (duckdb.Error, OSError):
        return {
            "sessions": [
                {"harness_id": sid, "status": "unavailable"} for sid in harness_ids
            ]
        }


def _as_utc(value: Any) -> datetime | None:
    """An aware UTC instant. Naive values are taken as UTC wall-clock.

    PostgreSQL hands back TIMESTAMPTZ (aware) while DuckDB TIMESTAMP columns
    are naive; comparing the two directly raises.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --- derived memory (PostgreSQL) ---------------------------------------------


def _memory(duckdb_path: Path) -> MemoryRepository | None:
    """The memory repository for this hub, or None without PostgreSQL."""
    if not memory_store_available(duckdb_path):
        return None
    try:
        return MemoryRepository(duckdb_path)
    except MemoryStoreUnavailable:
        return None


def _summary_row(memory_row: Any, keys: Sequence[str]) -> dict:
    """A SessionSummary/ProjectBrief in a tool's legacy row shape."""
    full = memory_row.as_dict()
    return {
        **{key: _coerce(full.get(key)) for key in keys},
        "generated_at": _coerce(full.get("generated_at")),
    }


def _newest_first(summaries: Iterable[SessionSummary]) -> list[SessionSummary]:
    """Order like ``ORDER BY ended_at DESC NULLS LAST``."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        summaries,
        key=lambda s: (s.ended_at is not None, _as_utc(s.ended_at) or floor),
        reverse=True,
    )


def _repo_summaries(
    repo: MemoryRepository,
    con: duckdb.DuckDBPyConnection,
    *,
    owner: str,
    name: str,
    branch: Optional[str] = None,
    limit: int,
    include_day_summary: bool = False,
) -> list[SessionSummary]:
    """A repository's newest final summaries.

    ``session_memory.project_key`` is the primary key into a repository: the
    summarizer stamps it from the session's own events. Summaries without one
    (or a branch-scoped question, which project_key cannot answer) resolve
    through DuckDB instead -- the repo's ``tasks`` rows, and optionally the
    ``agent_event_day_summary`` session index -- and are fetched by id.
    """
    from drover.server.lake.serving import HistoryConnection

    if isinstance(con, HistoryConnection):
        cursor = con.execute(
            """SELECT session_id, task_id FROM agent_events
            WHERE repo_owner=? AND repo_name=? AND (? IS NULL OR branch=?)
            GROUP BY session_id,task_id ORDER BY max(timestamp) DESC LIMIT ?""",
            [owner, name, branch, branch, _RECALL_SCOPE_LIMIT],
        )
        rows = cursor.fetchall()
        found = repo.summaries(row[0] for row in rows if row[0])
        for task in {row[1] for row in rows if row[1]}:
            for summary in repo.recent_summaries(task_id=task, limit=limit):
                found[summary.session_id] = summary
        if branch is None:
            for summary in repo.recent_summaries(
                project_key=f"{owner}/{name}", limit=limit
            ):
                found[summary.session_id] = summary
        return _newest_first(
            [
                summary
                for summary in found.values()
                if summary.session_id not in _PLACEHOLDER_SESSIONS
            ]
        )[:limit]
    limit = max(1, int(limit))
    # Each source may return a placeholder session that is dropped below.
    fetch = limit + len(_PLACEHOLDER_SESSIONS)
    found: dict[str, SessionSummary] = {}
    task_ids = [
        str(row[0])
        for row in con.execute(
            """SELECT task_id FROM tasks
                WHERE repo_owner = ? AND repo_name = ?
                  AND (? IS NULL OR branch = ?)
                ORDER BY last_activity_at DESC NULLS LAST
                LIMIT ?""",
            [owner, name, branch, branch, _REPO_TASK_LOOKUP_LIMIT],
        ).fetchall()
        if row[0]
    ]
    for task_id in task_ids:
        for summary in repo.recent_summaries(task_id=task_id, limit=fetch):
            found[summary.session_id] = summary
    if branch is None:
        for summary in repo.recent_summaries(
            project_key=f"{owner}/{name}", limit=fetch
        ):
            found[summary.session_id] = summary
        if include_day_summary:
            session_ids = [
                str(row[0])
                for row in con.execute(
                    """SELECT DISTINCT session_id FROM agent_event_day_summary
                        WHERE repo_owner = ? AND repo_name = ?""",
                    [owner, name],
                ).fetchall()
                if row[0]
            ]
            if session_ids:
                for summary in repo.recent_summaries(
                    session_ids=session_ids, limit=fetch
                ):
                    found[summary.session_id] = summary
    kept = [s for s in found.values() if s.session_id not in _PLACEHOLDER_SESSIONS]
    return _newest_first(kept)[:limit]


def _drop_closed_active_sessions(duckdb_path: Path, rows: list[dict]) -> list[dict]:
    """Apply the "a summary that covers the newest event closes it" rule.

    A summary is not an ending. The summarizer fires on an idle gap, so a
    session that is still working has one long before it stops, and treating
    "has a summary" as "closed" hid live sessions from every handoff. Only a
    summary whose ``ended_at`` already covers the newest event closes the
    session: a row stays when it has no summary, the summary has no
    ``ended_at``, or ``ended_at < last_event_at``. This used to be a join in
    the ``active_sessions`` view; summaries live in PostgreSQL now (#480).
    """
    if not rows:
        return rows
    repo = _memory(duckdb_path)
    if repo is None:
        return rows
    summaries = repo.summaries(row["session_id"] for row in rows)
    kept: list[dict] = []
    for row in rows:
        summary = summaries.get(row["session_id"])
        ended_at = _as_utc(summary.ended_at) if summary is not None else None
        last_event_at = _as_utc(row.get("last_event_at"))
        if ended_at is None or (last_event_at is not None and ended_at < last_event_at):
            kept.append(row)
    return kept


# --- drover_handoff -----------------------------------------------------------


def drover_handoff(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None,
    task_id: Optional[str] = None,
    max_summaries: int = 3,
    session_id: Optional[str] = None,
) -> dict:
    """Return recent session summaries + active sessions for a task or repo.

    Two query modes:

    * ``task_id`` given → exact lookup against that task hash.
    * ``(repo_owner, repo_name)`` given → JOIN through ``tasks`` so every
      branch of the repo is included. Pass ``branch`` to filter to a single
      branch; omit it to span all branches.

    Each ``task_id = compute_task_id(env_task_id, repo_owner, repo_name, branch)``
    folds the branch into the hash, so the cross-branch view was previously
    invisible to callers who only knew the repo (#53).
    """
    if session_id:
        summary = drover_session_summary(duckdb_path=duckdb_path, session_id=session_id)
        if summary.get("status") in {
            "unknown",
            "unmapped",
            "unavailable",
            "insufficient_input",
        }:
            return summary
        return {
            "status": "ok",
            "session_id": summary["session_id"],
            "summaries": [summary],
            "active_sessions": [],
        }
    by_repo = repo_owner is not None and repo_name is not None and task_id is None
    repo = _memory(duckdb_path)

    con = _connect(duckdb_path)
    try:
        if by_repo:
            found = (
                _repo_summaries(
                    repo,
                    con,
                    owner=repo_owner,
                    name=repo_name,
                    branch=branch,
                    limit=max_summaries,
                )
                if repo is not None
                else []
            )
            active = _row_to_dict(
                con.execute(
                    """SELECT session_id, agent_id, started_at, last_event_at,
                              event_count, repo_owner, repo_name, branch
                         FROM active_sessions
                        WHERE repo_owner = ? AND repo_name = ?
                          AND (? IS NULL OR branch = ?)
                        ORDER BY last_event_at DESC""",
                    [repo_owner, repo_name, branch, branch],
                )
            )
            tid = (
                compute_task_id(None, repo_owner, repo_name, branch) if branch else None
            )
        else:
            tid = task_id or compute_task_id(None, repo_owner, repo_name, branch)
            found = (
                [
                    s
                    for s in repo.recent_summaries(
                        task_id=tid,
                        limit=int(max_summaries) + len(_PLACEHOLDER_SESSIONS),
                    )
                    if s.session_id not in _PLACEHOLDER_SESSIONS
                ][: max(1, int(max_summaries))]
                if repo is not None
                else []
            )
            active = _row_to_dict(
                con.execute(
                    """SELECT session_id, agent_id, started_at, last_event_at, event_count,
                              repo_owner, repo_name, branch
                         FROM active_sessions
                        WHERE task_id = ?
                        ORDER BY last_event_at DESC""",
                    [tid],
                )
            )
    finally:
        con.close()

    summaries = [_summary_row(s, _HANDOFF_SUMMARY_KEYS) for s in found]
    active = _drop_closed_active_sessions(duckdb_path, active)
    return {
        "task_id": tid,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "summaries": summaries,
        "active_sessions": active,
    }


# --- drover_session_replay ----------------------------------------------------


@_canonical_read
def drover_session_replay(
    *,
    duckdb_path: Path,
    session_id: str,
    last_n_turns: int = 30,
    include_empty: bool = False,
) -> dict:
    resolution = _resolve(duckdb_path, session_id, history=True)
    if resolution["status"] != "ok":
        return resolution
    session_id = resolution["session_id"]
    con = _history_connect(duckdb_path)
    try:
        where = ["session_id = ?"]
        params: list[Any] = [session_id]
        if not include_empty:
            where.append("content IS NOT NULL AND trim(content) <> ''")
        cur = con.execute(
            f"""WITH candidate_agent_events AS (
                 SELECT * FROM agent_events
                 WHERE {" AND ".join(where)}
               ),
               {canonical_agent_events_cte(source="candidate_agent_events")}
               SELECT id, timestamp, agent_id, event_type, role, content, source
               FROM canonical_agent_events
               ORDER BY timestamp DESC
               LIMIT ?""",
            [*params, last_n_turns],
        )
        events = _row_to_dict(cur)
    finally:
        con.close()
    if not events:
        return _missing(resolution)
    return {**resolution, "include_empty": include_empty, "events": events}


# --- drover_session_summary ---------------------------------------------------


@_canonical_read
def drover_session_summary(
    *,
    duckdb_path: Path,
    session_id: str,
) -> dict:
    resolution = _resolve(duckdb_path, session_id, history=True)
    if resolution["status"] != "ok":
        return resolution
    repo = _memory(duckdb_path)
    if repo is None:
        return {**_missing(resolution), "memory_unavailable": True}
    summary = repo.summary(
        resolution.get("summary_session_id") or resolution["session_id"]
    )
    if summary is not None:
        return {
            **_summary_row(summary, _SESSION_SUMMARY_KEYS),
            **resolution,
            "status": summary.status,
            "artifact_session_id": summary.session_id,
        }
    job = JobLedger(duckdb_path).latest(SUMMARIZE_SESSION, resolution["session_id"])
    status = (
        "insufficient_input"
        if job and job.error_category == "no_events"
        else "unavailable"
    )
    return {
        **resolution,
        "status": status,
        "summary_status": job.status if job else "unavailable",
    }


def drover_active_sessions(
    *,
    duckdb_path: Path,
    task_id: Optional[str] = None,
) -> dict:
    return _control_active_sessions(duckdb_path, task_id=task_id)


# --- drover_search ------------------------------------------------------------


def _utc_partition_date(since: str) -> str | None:
    """The UTC date partition that contains the instant ``since`` names.

    agent_events partitions are keyed by UTC date. Taking the first ten
    characters of ``since`` instead is wrong for any positive offset:
    ``2026-09-22T03:30+05:30`` is 2026-09-21 in UTC, and bounding on 09-22
    skips the partition the instant falls in.

    A naive value is compared in the DuckDB session zone, which this function
    cannot see, so it gets one day of slack: that is safe for any real offset
    and costs at most one extra partition. Unparseable input returns None,
    which only forgoes pruning; the timestamp predicate still applies.
    """
    try:
        instant = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if instant.tzinfo is None:
        return (instant.date() - timedelta(days=1)).isoformat()
    return instant.astimezone(timezone.utc).date().isoformat()


@_canonical_read
def drover_search(
    *,
    duckdb_path: Path,
    query: str,
    task_id: Optional[str] = None,
    repo: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    default_since_days: int = 30,
    session_id: Optional[str] = None,
) -> dict:
    """Content LIKE search across agent_events.

    Until DuckDB FTS is wired up, we use case-insensitive LIKE on `content`.
    `repo` matches `<owner>/<name>` against `repo_owner || '/' || repo_name`.

    Unscoped searches default to a recent bounded window. This keeps MCP
    dogfood queries responsive on large live lakehouses and avoids broad view
    scans that can exhaust file descriptors. Pass `since` or a `repo`/`task_id`
    scope for explicit historical searches.
    """
    resolution = _resolve(duckdb_path, session_id, history=True) if session_id else None
    if resolution and resolution["status"] != "ok":
        return resolution
    scoped = bool(task_id or repo or since or session_id)
    where = ["content IS NOT NULL", "lower(content) LIKE ?"]
    params: list[Any] = [f"%{query.lower()}%"]
    if resolution:
        where.append("session_id = ?")
        params.append(resolution["session_id"])
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if repo:
        where.append("(repo_owner || '/' || repo_name) = ?")
        params.append(repo)
    if since:
        partition_date = _utc_partition_date(since)
        if partition_date is not None:
            where.append("date >= ?")
            params.append(partition_date)
        # The view reads mixed-era Parquet with union_by_name, so `timestamp`
        # can surface as VARCHAR. A bare `timestamp >= ?` then compares text,
        # and ' ' sorts before 'T': ISO-8601 bounds silently dropped 60 to 100
        # percent of matching events on the reference hub. Compare instants.
        where.append("TRY_CAST(timestamp AS TIMESTAMPTZ) >= CAST(? AS TIMESTAMPTZ)")
        params.append(_history_time_bound(duckdb_path, since))
    elif not scoped and default_since_days > 0:
        # Computed here and bound as parameters, not written as SQL over now().
        # The production view reads a list of per-partition globs, and an
        # expression over now() is not a plan-time constant there: it filtered
        # rows but pruned no partitions, so this path scanned the whole lake
        # (43.6s measured) while the explicit-since path, with a bound
        # parameter, pruned. Partitions are UTC dates, so the cutoff is UTC.
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(default_since_days))
        where.append("date >= ?")
        params.append(cutoff.date().isoformat())
        where.append("TRY_CAST(timestamp AS TIMESTAMPTZ) >= CAST(? AS TIMESTAMPTZ)")
        params.append(cutoff.isoformat())

    sql = f"""
      WITH candidate_agent_events AS (
        SELECT * FROM agent_events
        WHERE {" AND ".join(where)}
      ),
      {canonical_agent_events_cte(source="candidate_agent_events")}
      SELECT id, session_id, agent_id, timestamp, event_type, content, source
      FROM canonical_agent_events
      ORDER BY timestamp DESC
      LIMIT {int(limit) + 1}
    """
    con = _history_connect(duckdb_path)
    try:
        results = _row_to_dict(con.execute(sql, params))
    finally:
        con.close()
    if resolution and not results:
        return {
            **resolution,
            "status": "unavailable",
            "reason": "no_matches",
            "results": [],
        }
    return {
        "status": "ok",
        "query": query,
        "scoped": scoped,
        "since": since,
        "default_since_days": int(default_since_days) if not scoped else None,
        "results": results[:limit],
        "truncated": len(results) > limit,
    }


# --- drover_files_touched -----------------------------------------------------


@_canonical_read
def drover_files_touched(
    *,
    duckdb_path: Path,
    task_id: Optional[str] = None,
    since: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Return distinct file paths touched by Edit/Write/Bash tool_use blocks.

    Reads ``raw_data`` (JSON-encoded) from agent_events, walks any
    ``tool_use_blocks`` entries, and pulls ``input.file_path`` /
    ``input.path``.
    """
    resolution = _resolve(duckdb_path, session_id, history=True) if session_id else None
    if resolution and resolution["status"] != "ok":
        return resolution
    where = ["session_id = ?" if resolution else "task_id = ?"]
    params: list[Any] = [resolution["session_id"] if resolution else task_id]
    if since:
        where.append("timestamp >= ?")
        params.append(_history_time_bound(duckdb_path, since))
    sql = f"""
      WITH {canonical_agent_events_cte()}
      SELECT DISTINCT event_type, raw_data
      FROM canonical_agent_events
      WHERE {" AND ".join(where)} AND raw_data IS NOT NULL AND raw_data <> '{{}}'
      LIMIT {int(limit) + 1}
    """
    con = _history_connect(duckdb_path)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    from drover.server.summarizer.derive import compute_files_touched

    files = compute_files_touched(
        {"event_type": kind, "raw_data": raw} for kind, raw in rows
    )
    return {
        **(resolution or {}),
        "status": "ok",
        "task_id": task_id,
        "files": files[:limit],
        "truncated": len(rows) > limit or len(files) > limit,
    }


# --- drover_task_status -------------------------------------------------------


@_canonical_read
def drover_session_close(
    *,
    duckdb_path: Path,
    session_id: str,
) -> dict:
    """Enqueue the current source generation for ``session_id``.

    The source version is fingerprinted from the session's events in DuckDB;
    the job itself goes on the PostgreSQL job ledger (#480). ``status`` is the
    ledger's enqueue outcome: ``queued``, ``requeued`` (a different
    generation replaced a live job), ``already_queued``, ``already_done``,
    ``already_failed``, ``suppressed`` (dead-letter streak cap) or
    ``unavailable`` (no PostgreSQL control store).
    """
    resolution = _resolve(duckdb_path, session_id, history=True)
    # A native SessionEnd hook can arrive before collector ingestion. Keep
    # accepting that intent, while known harness identities must map explicitly.
    if resolution["status"] in ("unmapped", "unavailable"):
        return resolution
    session_id = resolution["session_id"]
    con = _history_connect(duckdb_path)
    try:
        source_version = source_version_for_session(con, session_id)
    finally:
        con.close()
    status = enqueue_summary_generation(duckdb_path, session_id, source_version)
    job_status = None
    if memory_store_available(duckdb_path):
        latest = JobLedger(duckdb_path).latest(SUMMARIZE_SESSION, session_id)
        job_status = latest.status if latest is not None else None
    return {"session_id": session_id, "status": status, "job_status": job_status}


# --- drover_project_brief -----------------------------------------------------


@_canonical_read
def drover_project_brief(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    project_key: Optional[str] = None,
) -> Optional[dict]:
    """Return the latest project_briefs row for a repository.

    Caller can pass either ``project_key="<owner>/<name>"`` or
    ``(repo_owner, repo_name)`` separately. Returns ``None`` if no brief
    has been generated yet. The returned row includes freshness metadata so
    agents do not mistake stale synthesized project briefs for current state.
    """
    if not project_key:
        if not (repo_owner and repo_name):
            raise ValueError(
                "project_brief: need project_key or (repo_owner, repo_name)"
            )
        project_key = f"{repo_owner}/{repo_name}"
    owner, _, name = project_key.partition("/")
    repo = _memory(duckdb_path)
    if repo is None:
        return None
    brief = repo.brief(project_key)
    if brief is None:
        return None
    row = _summary_row(brief, _BRIEF_KEYS)
    con = _history_connect(duckdb_path)
    try:
        # Newest activity the brief could have missed: summaries of the repo's
        # sessions (ended or regenerated) and the tasks' own activity marker.
        candidates: list[datetime | None] = []
        for summary in _repo_summaries(
            repo, con, owner=owner, name=name, limit=_BRIEF_FRESHNESS_SUMMARIES
        ):
            candidates.extend(
                [_as_utc(summary.ended_at), _as_utc(summary.generated_at)]
            )
        from drover.server.lake.serving import selected_config

        if selected_config(duckdb_path).backend == "ducklake":
            task_latest = con.execute(
                "SELECT max(timestamp) FROM agent_events WHERE repo_owner=? AND repo_name=?",
                [owner, name],
            ).fetchone()
        else:
            task_latest = con.execute(
                """SELECT MAX(TRY_CAST(last_activity_at AS TIMESTAMP))
                     FROM tasks WHERE repo_owner = ? AND repo_name = ?""",
                [owner, name],
            ).fetchone()
        candidates.append(_as_utc(task_latest[0]) if task_latest else None)
    finally:
        con.close()
    present = [value for value in candidates if value is not None]
    latest_activity = max(present) if present else None

    generated_at = _as_utc(brief.generated_at)
    last_activity_at = _as_utc(brief.last_activity_at)
    latest_activity_dt = latest_activity
    stale = False
    warning = ""
    if latest_activity_dt and generated_at and latest_activity_dt > generated_at:
        stale = True
        warning = (
            "project brief may be stale: newer session activity exists after "
            "the brief was generated; prefer drover_recent_sessions/drover_handoff "
            "for continuation context"
        )
    elif (
        latest_activity_dt
        and last_activity_at
        and latest_activity_dt > last_activity_at
    ):
        stale = True
        warning = (
            "project brief activity marker is stale: newer session activity exists; "
            "prefer drover_recent_sessions/drover_handoff for continuation context"
        )

    row["latest_session_activity_at"] = _coerce(latest_activity)
    row["stale"] = stale
    row["freshness_status"] = "stale" if stale else "fresh"
    row["freshness_warning"] = warning
    return row


# --- drover_recent_sessions ---------------------------------------------------


@_canonical_read
def drover_recent_sessions(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    project_key: Optional[str] = None,
    limit: int = 5,
) -> dict:
    """Return the N most recent session summaries for a repository.

    Useful for "what was the last session about?" — strictly more
    fine-grained than ``drover_project_brief`` (which is a synthesis).
    """
    if not project_key:
        if not (repo_owner and repo_name):
            raise ValueError(
                "recent_sessions: need project_key or (repo_owner, repo_name)"
            )
    if project_key:
        owner, _, name = project_key.partition("/")
    else:
        owner, name = repo_owner, repo_name
    repo = _memory(duckdb_path)
    sessions: list[dict] = []
    if repo is not None:
        con = _history_connect(duckdb_path)
        try:
            # session_memory.project_key first, then the task link, then the
            # day-summary index so unlinked summaries still surface.
            #
            # That last fallback used to rank every event in the lakehouse by
            # dedup_key to learn which sessions belong to a repository, which
            # cost the whole analytical budget and returned an OOM instead of
            # five rows (drover#369). `agent_event_day_summary` already
            # records (date, session_id, repo) per summarised partition, so
            # the same question is a small table lookup.
            found = _repo_summaries(
                repo, con, owner=owner, name=name, limit=limit, include_day_summary=True
            )
        finally:
            con.close()
        sessions = [_summary_row(s, _RECENT_SESSION_KEYS) for s in found]
    return {
        "project_key": project_key or f"{owner}/{name}",
        "repo_owner": owner,
        "repo_name": name,
        "sessions": sessions,
    }


# --- context containers ------------------------------------------------------


_CONTEXT_CONTAINER_COLUMNS = """
    context_id, container_type, label, source_harness, confidence, evidence,
    last_touched_at, next_action, open_loop, session_ids, task_ids,
    repo_owner, repo_name, branch, summary_md, redaction_policy,
    created_at, updated_at
"""


def _selected_context(path, *, limit, **options):
    from drover.server.lake.serving import selected_config

    if selected_config(path).backend == "ducklake":
        from drover.server.lake.read_models import read_model

        result = read_model(path, "contexts", limit=limit, **options)
        if options.get("mode") == "brief" and result.get("status") != "unavailable":
            return result["context"]
        return result
    return None


@_canonical_read
def drover_recent_contexts(
    *,
    duckdb_path: Path,
    container_type: Optional[str] = None,
    source_harness: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Return recent confidence-aware context containers.

    Unlike repo-first tools, this includes personal/research/open-floor/general
    containers whose repo columns are intentionally null.
    """
    where: list[str] = []
    params: list[Any] = []
    if container_type:
        where.append("container_type = ?")
        params.append(normalize_context_type(container_type))
    if source_harness:
        where.append("source_harness = ?")
        params.append(source_harness)
    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    selected = _selected_context(
        duckdb_path,
        limit=limit,
        container_type=container_type,
        source_harness=source_harness,
    )
    if selected is not None:
        return selected
    con = _connect(duckdb_path)
    try:
        rows = _row_to_dict(
            con.execute(
                f"""SELECT {_CONTEXT_CONTAINER_COLUMNS}
                    FROM context_containers
                    {sql_where}
                    ORDER BY last_touched_at DESC NULLS LAST, updated_at DESC
                    LIMIT ?""",
                [*params, int(limit)],
            )
        )
    finally:
        con.close()
    return {"contexts": rows, "limit": int(limit)}


@_canonical_read
def drover_context_brief(
    *,
    duckdb_path: Path,
    context_id: Optional[str] = None,
    label: Optional[str] = None,
) -> Optional[dict]:
    """Return one context container by id or label."""
    if not (context_id or label):
        raise ValueError("context_brief: need context_id or label")
    predicate = "context_id = ?" if context_id else "label = ?"
    value = context_id or label
    from drover.server.lake.serving import selected_config

    if selected_config(duckdb_path).backend == "ducklake":
        return _selected_context(
            duckdb_path, limit=1, mode="brief", context_id=context_id, label=label
        )
    con = _connect(duckdb_path)
    try:
        rows = _row_to_dict(
            con.execute(
                f"""SELECT {_CONTEXT_CONTAINER_COLUMNS}
                    FROM context_containers
                    WHERE {predicate}
                    ORDER BY last_touched_at DESC NULLS LAST, updated_at DESC
                    LIMIT 1""",
                [value],
            )
        )
    finally:
        con.close()
    return rows[0] if rows else None


@_canonical_read
def drover_open_loops(
    *,
    duckdb_path: Path,
    container_type: Optional[str] = None,
    limit: int = 20,
    project_key: Optional[str] = None,
) -> dict:
    """Return context containers with a known next action or open loop.

    When supplied, ``project_key`` must be one exact ``<owner>/<name>`` pair.
    """
    where = [
        "(COALESCE(next_action, '') <> '' OR COALESCE(open_loop, '') <> '')",
    ]
    params: list[Any] = []
    if container_type:
        where.append("container_type = ?")
        params.append(normalize_context_type(container_type))
    if project_key is not None:
        owner, separator, name = project_key.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError("open_loops: project_key must be one <owner>/<name> pair")
        where.extend(["repo_owner = ?", "repo_name = ?"])
        params.extend([owner, name])
    selected = _selected_context(
        duckdb_path,
        limit=limit,
        mode="loops",
        container_type=container_type,
        project_key=project_key,
    )
    if selected is not None:
        return selected
    con = _connect(duckdb_path)
    try:
        rows = _row_to_dict(
            con.execute(
                f"""SELECT {_CONTEXT_CONTAINER_COLUMNS}
                    FROM context_containers
                    WHERE {' AND '.join(where)}
                    ORDER BY last_touched_at DESC NULLS LAST, updated_at DESC
                    LIMIT ?""",
                [*params, int(limit)],
            )
        )
    finally:
        con.close()
    return {"open_loops": rows, "limit": int(limit)}


@_canonical_read
def drover_resume_context(
    *,
    duckdb_path: Path,
    context_id: Optional[str] = None,
    label: Optional[str] = None,
    max_summaries: int = 5,
) -> Optional[dict]:
    """Return a resumable context container plus linked session summaries."""
    from drover.server.lake.coverage import read_fence

    with read_fence(duckdb_path):
        return _resume_context(
            duckdb_path=duckdb_path,
            context_id=context_id,
            label=label,
            max_summaries=max_summaries,
        )


def _resume_context(*, duckdb_path, context_id, label, max_summaries):
    from drover.server.lake.runtime import LakeError
    from drover.server.lake.serving import selected_config

    if selected_config(duckdb_path).backend == "ducklake" and (
        type(max_summaries) is not int or not 1 <= max_summaries <= 1000
    ):
        raise LakeError("analytics_row_limit_exceeded")
    if not (context_id or label):
        raise ValueError("context_brief: need context_id or label")
    container = drover_context_brief(
        duckdb_path=duckdb_path, context_id=context_id, label=label
    )
    if not container:
        return None
    if container.get("status") == "unavailable":
        return container
    session_ids = container.get("session_ids") or []
    summaries: list[dict] = []
    repo = _memory(duckdb_path) if session_ids else None
    if repo is not None:
        summaries = [
            _summary_row(s, _RESUME_SUMMARY_KEYS)
            for s in repo.recent_summaries(session_ids=session_ids, limit=max_summaries)
        ]
    result = {"context": container, "session_summaries": summaries}
    from drover.server.lake.serving import selected_config

    if selected_config(duckdb_path).backend == "ducklake":
        from drover.server.lake.coverage import bounded

        bounded(summaries)
        bounded(result)
    return result


# --- drover_project_activity --------------------------------------------------


def _activity_days(since: Optional[str], days: Optional[int], now: datetime) -> int:
    """Window length in whole days; ``since`` wins, both are capped at 30."""
    if since:
        lower = _parse_datetime(since)
        if lower is None:
            raise ValueError("project_activity: since must be an ISO-8601 timestamp")
        if lower.tzinfo is None:
            lower = lower.replace(tzinfo=timezone.utc)
        elapsed = (now - lower).total_seconds() / 86400.0
        return max(1, min(PROJECT_ACTIVITY_MAX_DAYS, int(elapsed) + 1))
    return max(1, min(PROJECT_ACTIVITY_MAX_DAYS, int(days or 7)))


@_canonical_read
def drover_project_activity(
    *,
    duckdb_path: Path,
    project_key: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = 20,
) -> dict:
    """Return what happened on a project recently and what is still open.

    Built from Drover's own records -- harness launches and state, agent-event
    day summaries, session summaries and ``session_usage`` -- never spans
    (#473). ``project_key`` filters to ``<owner>/<name>``; without it every
    project in the window is listed. ``since`` (ISO-8601) or ``days`` sets the
    window, default 7 days, capped at 30. ``limit`` caps the sessions in the
    timeline (max 200); projects and open items have their own hard caps.
    """
    now = datetime.now(timezone.utc)
    window_days = _activity_days(since, days, now)
    from drover.server.lake.serving import selected_config

    if selected_config(duckdb_path).backend == "ducklake":
        from drover.server.lake.read_models import read_model

        return read_model(
            duckdb_path,
            "project_activity",
            project_key=project_key,
            days=window_days,
            now=now.isoformat(),
            max_sessions=int(limit),
        )
    con = _connect(duckdb_path)
    try:
        with attached_control_plane_snapshot(con, duckdb_path):
            return project_activity(
                con,
                memory_store_path=duckdb_path,
                project_key=project_key,
                days=window_days,
                now=now,
                max_sessions=int(limit),
            )
    finally:
        con.close()


# --- drover_fleet_status ------------------------------------------------------


@_canonical_read
def drover_fleet_status(
    *,
    duckdb_path: Path,
) -> dict:
    """Return live harness sessions from authoritative control-plane state."""
    return _control_active_sessions(duckdb_path)


def _control_active_sessions(duckdb_path: Path, task_id: str | None = None) -> dict:
    from dataclasses import asdict

    from drover.server.control_store import is_postgres_control_store
    from drover.server.harness.registry import HarnessRegistry

    registry = HarnessRegistry(duckdb_path)
    # The registry default excludes retired hosts; retain that boundary for
    # active work while deriving liveness for every host it returns.
    host_liveness = {
        host.host_id: (host.liveness().state if hasattr(host, "liveness") else "online")
        for host in registry.list_hosts()
    }
    sessions = []
    for session in registry.list_sessions(archived_limit=0):
        liveness = host_liveness.get(session.host_id)
        # Retired hosts retain history but are never active fleet work.
        if liveness is None or liveness == "retired":
            continue
        tid = compute_task_id(
            None, session.repo_owner, session.repo_name, session.branch
        )
        if task_id and tid != task_id:
            continue
        row = _coerce(asdict(session))
        row.update(
            agent_id=session.host_id,
            task_id=tid,
            host_liveness=liveness,
        )
        sessions.append(row)
    return {
        "active_sessions": sessions,
        "count": len(sessions),
        "state_source": "control_plane.harness_sessions+harness_hosts",
        "control_store": (
            "postgres" if is_postgres_control_store(duckdb_path) else "duckdb"
        ),
        "authoritative": True,
    }


# --- drover_data_quality ------------------------------------------------------


def drover_data_quality(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    hours: int = 24,
    deep: bool = False,
    spans_enabled: bool = False,
) -> dict:
    """Return the structured read-only Drover lakehouse quality snapshot.

    MCP defaults to the same standard-depth snapshot as the CLI. Deep audits are
    useful for operator triage but can exceed short agent hook budgets.
    """
    return quality_snapshot(
        duckdb_path=duckdb_path,
        incoming_dir=incoming_dir,
        hours=int(hours),
        deep=deep,
        spans_enabled=spans_enabled,
    )


def drover_pipeline_observatory(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    max_artifacts: int = 10,
    max_projects: int = 10,
    spans_enabled: bool = False,
) -> dict:
    """Return saved artifact and project-readiness drilldown for Drover."""
    quality = quality_snapshot(
        duckdb_path=duckdb_path,
        incoming_dir=incoming_dir,
        deep=False,
        spans_enabled=spans_enabled,
    )
    return pipeline_observatory_snapshot(
        duckdb_path=duckdb_path,
        runtime_audit=quality.get("runtime_audit", {}),
        max_artifacts=max_artifacts,
        max_projects=max_projects,
    )


# --- drover_recall (semantic search) -----------------------------------------


def _recall_row(
    session_id: str, summary: Optional[SessionSummary], score: Optional[float]
) -> dict:
    """One recall hit. ``span_id``/``source_text`` stay for shape compatibility:
    span embeddings are out of the core path (#473), so they are always null."""
    return {
        "source_type": "session_summary",
        "session_id": session_id,
        "span_id": None,
        "agent_id": summary.agent_id if summary else None,
        "ended_at": _coerce(summary.ended_at) if summary else None,
        "generated_at": _coerce(summary.generated_at) if summary else None,
        "summary_md": summary.summary_md if summary else None,
        "next_steps_md": summary.next_steps_md if summary else None,
        "open_questions": list(summary.open_questions) if summary else [],
        "source_text": None,
        "score": score,
    }


@_canonical_read
def drover_recall(
    *,
    duckdb_path: Path,
    query_embedding: Optional[list[float]] = None,
    limit: int = 5,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    include_spans: bool = False,
    session_id: Optional[str] = None,
    query: Optional[str] = None,
    embedding_model: Optional[str] = None,
    query_embedding_model: Optional[str] = None,
) -> dict:
    """Return session summaries ranked by cosine similarity to ``query_embedding``.

    The embedding has to be supplied by the caller -- Drover's MCP layer
    doesn't call out to the embedder for ad-hoc query encoding. It must come
    from ``embedding_model`` (the hub's configured embedding model): search is
    exact cosine within that one embedding space in pgvector, and a vector of
    the wrong dimension is an error, not an empty result.

    ``query`` is the keyword fallback: when no semantic search is possible
    (no embedding given, no model configured, pgvector missing) it does a
    case-insensitive match over summaries instead. ``mode`` says which ran
    (``semantic``/``keyword``/``none``) and ``reason`` why semantic did not.

    Filters by ``(repo_owner, repo_name)`` if both are provided. Span
    embeddings have been removed from the core memory path (#480).
    """
    if query_embedding is not None:
        from drover.server.memory_store import EMBEDDING_DIM, EmbeddingMismatch

        if len(query_embedding) != EMBEDDING_DIM:
            raise EmbeddingMismatch(
                f"embedding has {len(query_embedding)} dimensions; expected {EMBEDDING_DIM}"
            )
        if (
            query_embedding_model is not None
            and query_embedding_model != embedding_model
        ):
            raise EmbeddingMismatch(
                f"query model {query_embedding_model!r} does not match hub model {embedding_model!r}"
            )
    resolution = _resolve(duckdb_path, session_id, history=True) if session_id else None
    if resolution and resolution["status"] != "ok":
        return resolution
    if not query_embedding and not query:
        raise ValueError(
            "recall: query_embedding (list[float]) or a keyword query is required"
        )
    limit = max(1, int(limit))
    out: dict[str, Any] = {
        "results": [],
        "limit": limit,
        "mode": "none",
        "memory_unavailable": False,
        "reason": None,
    }
    repo = _memory(duckdb_path)
    if repo is None:
        out.update(memory_unavailable=True, reason=MEMORY_UNAVAILABLE_REASON)
        return out

    scope: Optional[set[str]] = (
        {resolution.get("summary_session_id") or resolution["session_id"]}
        if resolution
        else None
    )
    if repo_owner and repo_name:
        con = _history_connect(duckdb_path)
        try:
            repo_scope = {
                s.session_id
                for s in _repo_summaries(
                    repo,
                    con,
                    owner=repo_owner,
                    name=repo_name,
                    limit=_RECALL_SCOPE_LIMIT,
                    include_day_summary=True,
                )
            }
        finally:
            con.close()
        scope = repo_scope if scope is None else scope & repo_scope
        if not scope:
            out["reason"] = f"no summarized sessions for {repo_owner}/{repo_name}"
            return out

    if query_embedding:
        if not embedding_model:
            out["reason"] = "no embedding model is configured for semantic recall"
        else:
            store = EmbeddingStore(duckdb_path, model=embedding_model)
            try:
                hits = store.search(query_embedding, limit=limit, session_ids=scope)
            except VectorStoreUnavailable as exc:
                out["reason"] = str(exc)
            else:
                summaries = repo.summaries(hit.session_id for hit in hits)
                out["results"] = [
                    _recall_row(
                        hit.session_id, summaries.get(hit.session_id), hit.similarity
                    )
                    for hit in hits
                ]
                out["mode"] = "semantic"
                return out

    if query:
        fetch = limit if scope is None else max(limit, _RECALL_SCOPE_LIMIT)
        matches = [
            s
            for s in repo.search_summaries(query, limit=fetch)
            if scope is None or s.session_id in scope
        ][:limit]
        out["results"] = [_recall_row(s.session_id, s, None) for s in matches]
        out["mode"] = "keyword"
    return out


def drover_active_handoff(
    *,
    duckdb_path: Path,
    session_id: str,
    backend_config: Any = None,
    backend: Any = None,
    max_age_seconds: float = 60,
) -> dict:
    """Rolling handoff brief for an OPEN session.

    Returns the cached ``active_session_briefs`` row when it's within the
    TTL, otherwise refreshes it from the most recent events for the
    session. See ``drover.server.briefs.active.generate_active_brief``.
    """
    from drover.server.briefs.active import generate_active_brief

    resolution = _resolve(duckdb_path, session_id)
    if resolution["status"] != "ok":
        return resolution
    session_id = resolution["session_id"]
    try:
        return generate_active_brief(
            duckdb_path,
            session_id,
            backend=backend,
            backend_config=backend_config,
            max_age_seconds=max_age_seconds,
        )
    except (RuntimeError, duckdb.Error):
        return _missing(resolution)


@_canonical_read
def drover_task_status(
    *,
    duckdb_path: Path,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    from drover.server.lake.serving import selected_config

    if selected_config(duckdb_path).backend == "ducklake":
        from drover.server.lake.task_projection import task_status

        out = task_status(duckdb_path, task_id=task_id, session_id=session_id)
        if out["status"] == "unknown":
            return out
        import psycopg

        from drover.server.lake.runtime import LakeError

        try:
            repo = _memory(duckdb_path)
            summaries = (
                repo.recent_summaries(task_id=out["task_id"], limit=1) if repo else []
            )
        except psycopg.Error:
            raise LakeError("analytics_task_summary_unavailable") from None
        out["latest_summary"] = (
            _summary_row(summaries[0], _TASK_SUMMARY_KEYS) if summaries else None
        )
        return out
    if session_id:
        resolution = _resolve(duckdb_path, session_id)
        if resolution["status"] != "ok":
            return resolution
        con = _connect(duckdb_path)
        try:
            row = con.execute(
                "SELECT task_id FROM agent_events WHERE session_id=? AND task_id IS NOT NULL LIMIT 1",
                [resolution["session_id"]],
            ).fetchone()
        finally:
            con.close()
        if not row:
            return _missing(resolution)
        task_id = row[0]
    con = _connect(duckdb_path)
    try:
        task_rows = _row_to_dict(
            con.execute(
                """SELECT task_id, repo_owner, repo_name, branch, principal_id,
                      status, created_at, last_activity_at, session_count, total_cost_usd
               FROM tasks WHERE task_id = ?""",
                [task_id],
            )
        )
        if not task_rows:
            return {"status": "unknown", "task_id": task_id}
        # Refresh aggregates from views (don't trust tasks.session_count)
        ev = con.execute(
            f"""WITH {canonical_agent_events_cte()}
               SELECT count(DISTINCT session_id), count(DISTINCT agent_id),
                      max(timestamp)
               FROM canonical_agent_events WHERE task_id = ?""",
            [task_id],
        ).fetchone()
    finally:
        con.close()
    repo = _memory(duckdb_path)
    latest_summary = (
        [
            _summary_row(s, _TASK_SUMMARY_KEYS)
            for s in repo.recent_summaries(task_id=task_id, limit=1)
        ]
        if repo is not None
        else []
    )

    out = task_rows[0]
    out["session_count"] = ev[0] or 0
    out["agent_count"] = ev[1] or 0
    out["last_activity_at"] = (
        ev[2].isoformat() if ev[2] else out.get("last_activity_at")
    )
    out["latest_summary"] = latest_summary[0] if latest_summary else None
    return out


# --- drover_provider_quota ----------------------------------------------------


def _format_routing_reset_time(dt: datetime, now: datetime | None = None) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if abs((dt - now).total_seconds()) < 24 * 3600 and dt.date() == now.date():
        return dt.strftime("%H:%MZ")
    return dt.strftime("%Y-%m-%d %H:%MZ")


def _mask_email(label: str | None) -> str | None:
    """Agents get a recognisable label, never a full mailbox address."""
    if not label or "@" not in label:
        return label
    local, _, domain = label.partition("@")
    return f"{local[:1]}***@{domain}"


def _account_identity_key(snapshot: Any) -> str:
    identity = (getattr(snapshot, "account_identity", None) or "").strip().lower()
    label = (getattr(snapshot, "account_label", None) or "").strip().lower()
    provider = getattr(snapshot, "provider", "")
    if identity:
        return f"{provider}|{identity}"
    if "@" in label:
        return f"{provider}|{label}"
    if label and label != "unknown account":
        return f"{provider}|{label}"
    return f"{provider}|unknown|{getattr(snapshot, 'host_id', '')}"


def _build_routing_hint(accounts: list[dict[str, Any]], now: datetime) -> str:
    """Build compact headroom routing hint across pools (gemini, codex, agy-claude, claude)."""
    from drover.server.mcp.freshness import _instant

    pools: dict[str, list[dict[str, Any]]] = {}

    for account in accounts:
        prov = account.get("provider", "")
        for w in account.get("windows", []):
            kind = str(w.get("kind") or "")
            if prov == "google":
                if "claude_gpt" in kind or "3p" in kind:
                    pool_name = "agy-claude"
                else:
                    pool_name = "gemini"
            elif prov == "openai":
                pool_name = "codex"
            elif prov == "anthropic":
                pool_name = "claude"
            else:
                pool_name = prov or "other"
            pools.setdefault(pool_name, []).append(w)

    if not pools:
        return "no provider accounts configured"

    priority = ["gemini", "codex", "agy-claude", "claude"]
    ordered_keys = sorted(
        pools.keys(),
        key=lambda k: (priority.index(k) if k in priority else 99, k),
    )

    hints: list[str] = []
    for pool in ordered_keys:
        windows = pools[pool]
        # 1. Observed exhaustion takes priority
        obs_resets = [
            _instant(w.get("resets_at"))
            for w in windows
            if w.get("status") == "observed_exhausted" and w.get("resets_at")
        ]
        if obs_resets:
            valid_resets = [r for r in obs_resets if r is not None]
            until_str = (
                _format_routing_reset_time(max(valid_resets), now=now)
                if valid_resets
                else ""
            )
            hints.append(
                f"{pool}: observed exhausted until {until_str}"
                if until_str
                else f"{pool}: observed exhausted"
            )
            continue

        # 2. Complete exhaustion
        exhausted_resets = [
            _instant(w.get("resets_at"))
            for w in windows
            if (
                w.get("status") == "exhausted"
                or (
                    w.get("used_percent") is not None and w.get("used_percent") >= 100.0
                )
            )
            and w.get("resets_at")
        ]
        all_exhausted = windows and all(
            w.get("status") == "exhausted"
            or (w.get("used_percent") is not None and w.get("used_percent") >= 100.0)
            for w in windows
        )
        if exhausted_resets and all_exhausted:
            valid_resets = [r for r in exhausted_resets if r is not None]
            until_str = (
                _format_routing_reset_time(min(valid_resets), now=now)
                if valid_resets
                else ""
            )
            hints.append(
                f"{pool}: out until {until_str}" if until_str else f"{pool}: out"
            )
            continue

        # 3. Available headroom: show used % of the tightest window
        used_percents = [
            w.get("used_percent")
            for w in windows
            if isinstance(w.get("used_percent"), (int, float))
        ]
        if used_percents:
            tightest = max(used_percents)
            hints.append(f"{pool}: {int(round(tightest))}%")
            continue

        hints.append(f"{pool}: unavailable")

    return "; ".join(hints)


@_canonical_read
def drover_provider_quota(
    *,
    duckdb_path: Path,
    provider: str | None = None,
    fresh: bool = False,
    timeout_s: float = 3.0,
    service: Any | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only provider quota for every account across hosts with routing hint."""
    import concurrent.futures

    from drover.server.harness.registry import HarnessRegistry
    from drover.server.providers.service import ProviderUsageService

    current_now = now or datetime.now(timezone.utc)
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=timezone.utc)

    db_path = Path(duckdb_path)
    usage_service = service or ProviderUsageService(
        duckdb_path=db_path,
        parquet_dir=db_path.parent / "parquet",
        clock=lambda: current_now,
    )

    if fresh:
        try:
            registry = HarnessRegistry(db_path)
            online_hosts = [
                h
                for h in registry.list_hosts()
                if getattr(h, "liveness", None) and h.liveness().state == "online"
            ]
            if online_hosts:
                # Not a `with` block: its exit joins every worker, so a slow
                # host would hold the call past `timeout_s`. Probes still
                # running at the deadline finish in the background and land
                # in the store for the next read.
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(online_hosts))
                )
                try:
                    futures = [
                        executor.submit(usage_service.refresh_host, host)
                        for host in online_hosts
                    ]
                    concurrent.futures.wait(futures, timeout=timeout_s)
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logging.getLogger(__name__).debug(
                "fresh provider quota refresh encountered an error", exc_info=True
            )

    try:
        raw_snapshots = usage_service.latest_accounts()
    except (duckdb.Error, OSError) as exc:
        logging.getLogger(__name__).warning("failed to read provider accounts: %s", exc)
        return {
            "accounts": [],
            "routing_hint": "no provider accounts configured",
            "updated_at": None,
        }

    if provider:
        provider_norm = provider.strip().lower()
        raw_snapshots = [
            s for s in raw_snapshots if s.provider.lower() == provider_norm
        ]

    # Group snapshots by subscription identity
    buckets: dict[str, list[Any]] = {}
    for snapshot in raw_snapshots:
        key = _account_identity_key(snapshot)
        buckets.setdefault(key, []).append(snapshot)

    account_records: list[dict[str, Any]] = []
    latest_timestamps: list[datetime] = []

    for key, members in buckets.items():

        def member_sort_key(s: Any):
            status = getattr(s, "status", "")
            status_priority = {
                "observed_exhausted": 4,
                "ok": 3,
                "usage_unavailable": 2,
                "stale": 1,
                "error": 0,
            }.get(status, 0)
            obs = getattr(s, "observed_at", current_now)
            return (status_priority, obs)

        representative = max(members, key=member_sort_key)
        rep_provider = representative.provider
        rep_plan = representative.plan_label or next(
            (s.plan_label for s in members if s.plan_label), None
        )
        rep_label = (
            representative.account_label
            if representative.account_label != "Unknown account"
            else next(
                (
                    s.account_label
                    for s in members
                    if s.account_label != "Unknown account"
                ),
                representative.account_label,
            )
        )
        hosts = sorted({s.host_id for s in members if s.host_id})
        rep_obs = max(s.observed_at for s in members)
        latest_timestamps.append(rep_obs)

        # Build windows
        windows_list: list[dict[str, Any]] = []
        for w in representative.windows:
            w_status: str
            if (
                representative.status == "observed_exhausted"
                or representative.source == "observed_429"
            ) and (w.used_percent == 100.0 or "claude_gpt" in w.kind):
                w_status = "observed_exhausted"
                w_source = "observed_429"
            elif w.used_percent is not None and w.used_percent >= 100.0:
                w_status = "exhausted"
                w_source = "provider_api"
            elif w.used_percent is not None:
                w_status = "ok"
                w_source = "provider_api"
            else:
                w_status = "usage_unavailable"
                w_source = "provider_api"

            windows_list.append(
                {
                    "kind": w.kind,
                    "used_percent": w.used_percent,
                    "resets_at": w.resets_at.isoformat() if w.resets_at else None,
                    "status": w_status,
                    "source": w_source,
                    "updated_at": representative.observed_at.isoformat(),
                }
            )

        account_records.append(
            {
                "provider": rep_provider,
                "account_label": _mask_email(rep_label),
                "plan": rep_plan,
                "hosts": hosts,
                "status": representative.status,
                "windows": windows_list,
                "updated_at": rep_obs.isoformat(),
            }
        )

    account_records.sort(key=lambda a: (a["provider"], a["account_label"]))
    overall_updated_at = (
        max(latest_timestamps).isoformat() if latest_timestamps else None
    )
    routing_hint = _build_routing_hint(account_records, now=current_now)

    return {
        "accounts": account_records,
        "routing_hint": routing_hint,
        "updated_at": overall_updated_at,
    }


# Apply the same response caps to direct users and the MCP transport.
for _tool_name in READ_CAPS:
    if _tool_name in globals():
        globals()[_tool_name] = bounded_read(globals()[_tool_name])


# Transition aliases (nexus_* → drover_*), kept for one release so existing
# callers keep working; see docs/porting-and-cutover.md §7.6.
nexus_handoff = drover_handoff
nexus_session_replay = drover_session_replay
nexus_session_summary = drover_session_summary
nexus_active_sessions = drover_active_sessions
nexus_search = drover_search
nexus_files_touched = drover_files_touched
nexus_session_close = drover_session_close
nexus_project_brief = drover_project_brief
nexus_recent_sessions = drover_recent_sessions
nexus_recent_contexts = drover_recent_contexts
nexus_context_brief = drover_context_brief
nexus_open_loops = drover_open_loops
nexus_resume_context = drover_resume_context
nexus_recall = drover_recall
nexus_task_status = drover_task_status
nexus_project_activity = drover_project_activity
nexus_active_handoff = drover_active_handoff
nexus_fleet_status = drover_fleet_status
nexus_data_quality = drover_data_quality
nexus_pipeline_observatory = drover_pipeline_observatory
nexus_provider_quota = drover_provider_quota
