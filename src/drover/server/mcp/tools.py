"""MCP tool implementations.

These are pure functions over a DuckDB lakehouse path: each opens its
own connection, executes a query, and returns a JSON-serializable dict.
The MCP transport layer (server.py) thinly wraps them; tests exercise
them directly so we can validate behavior without a transport.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import duckdb

from drover.context_containers import normalize_context_type
from drover.event_identity import canonical_agent_events_cte
from drover.server.db import open_duckdb_connection
from drover.server.observatory import pipeline_observatory_snapshot
from drover.server.quality import quality_snapshot
from drover.task_id import compute_task_id

log = logging.getLogger("drover.mcp.tools")


def _connect(duckdb_path: Path) -> duckdb.DuckDBPyConnection:
    # Read-write open even for read paths: a read_only connection has a
    # different config and DuckDB rejects it beside live writers (issue #2).
    return open_duckdb_connection(duckdb_path, role="diagnostic")


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


# --- drover_handoff -----------------------------------------------------------


def drover_handoff(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None,
    task_id: Optional[str] = None,
    max_summaries: int = 3,
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
    by_repo = repo_owner is not None and repo_name is not None and task_id is None

    con = _connect(duckdb_path)
    try:
        if by_repo:
            summaries = _row_to_dict(
                con.execute(
                    """SELECT ss.session_id, ss.agent_id, ss.ended_at,
                              ss.summary_md, ss.next_steps_md, ss.open_questions,
                              ss.files_touched, ss.status, ss.generator_model
                         FROM session_summaries ss
                         JOIN tasks t ON ss.task_id = t.task_id
                        WHERE t.repo_owner = ? AND t.repo_name = ?
                          AND (? IS NULL OR t.branch = ?)
                          AND ss.session_id <> 'unknown_openclaw'
                        ORDER BY ss.ended_at DESC
                        LIMIT ?""",
                    [repo_owner, repo_name, branch, branch, max_summaries],
                )
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
            summaries = _row_to_dict(
                con.execute(
                    """SELECT session_id, agent_id, ended_at, summary_md, next_steps_md,
                              open_questions, files_touched, status, generator_model
                         FROM session_summaries
                        WHERE task_id = ?
                          AND session_id <> 'unknown_openclaw'
                        ORDER BY ended_at DESC
                        LIMIT ?""",
                    [tid, max_summaries],
                )
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

    return {
        "task_id": tid,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "summaries": summaries,
        "active_sessions": active,
    }


# --- drover_session_replay ----------------------------------------------------


def drover_session_replay(
    *,
    duckdb_path: Path,
    session_id: str,
    last_n_turns: int = 30,
    include_empty: bool = False,
) -> dict:
    con = _connect(duckdb_path)
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
               SELECT id, timestamp, agent_id, event_type, role, content
               FROM canonical_agent_events
               ORDER BY timestamp DESC
               LIMIT ?""",
            [*params, last_n_turns],
        )
        events = _row_to_dict(cur)
    finally:
        con.close()
    return {"session_id": session_id, "include_empty": include_empty, "events": events}


# --- drover_session_summary ---------------------------------------------------


def drover_session_summary(
    *,
    duckdb_path: Path,
    session_id: str,
) -> Optional[dict]:
    con = _connect(duckdb_path)
    try:
        cur = con.execute(
            """SELECT session_id, task_id, agent_id, ended_at, summary_md,
                      files_touched, tools_used, last_user_prompt, last_assistant,
                      next_steps_md, open_questions, status, generator_model, generated_at
               FROM session_summaries
               WHERE session_id = ?""",
            [session_id],
        )
        rows = _row_to_dict(cur)
    finally:
        con.close()
    return rows[0] if rows else None


# --- drover_active_sessions ---------------------------------------------------


def drover_active_sessions(
    *,
    duckdb_path: Path,
    task_id: Optional[str] = None,
) -> dict:
    con = _connect(duckdb_path)
    try:
        if task_id:
            cur = con.execute(
                """SELECT session_id, agent_id, task_id, started_at, last_event_at,
                          event_count, repo_owner, repo_name, branch
                   FROM active_sessions WHERE task_id = ?
                   ORDER BY last_event_at DESC""",
                [task_id],
            )
        else:
            cur = con.execute(
                """SELECT session_id, agent_id, task_id, started_at, last_event_at,
                          event_count, repo_owner, repo_name, branch
                   FROM active_sessions
                   ORDER BY last_event_at DESC"""
            )
        active = _row_to_dict(cur)
    finally:
        con.close()
    return {"active_sessions": active}


# --- drover_search ------------------------------------------------------------


def drover_search(
    *,
    duckdb_path: Path,
    query: str,
    task_id: Optional[str] = None,
    repo: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    default_since_days: int = 30,
) -> dict:
    """Content LIKE search across agent_events.

    Until DuckDB FTS is wired up, we use case-insensitive LIKE on `content`.
    `repo` matches `<owner>/<name>` against `repo_owner || '/' || repo_name`.

    Unscoped searches default to a recent bounded window. This keeps MCP
    dogfood queries responsive on large live lakehouses and avoids broad view
    scans that can exhaust file descriptors. Pass `since` or a `repo`/`task_id`
    scope for explicit historical searches.
    """
    scoped = bool(task_id or repo or since)
    where = ["content IS NOT NULL", "lower(content) LIKE ?"]
    params: list[Any] = [f"%{query.lower()}%"]
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if repo:
        where.append("(repo_owner || '/' || repo_name) = ?")
        params.append(repo)
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    elif not scoped and default_since_days > 0:
        where.append(
            f"TRY_CAST(timestamp AS TIMESTAMPTZ) >= now() - INTERVAL {int(default_since_days)} DAY"
        )

    sql = f"""
      WITH candidate_agent_events AS (
        SELECT * FROM agent_events
        WHERE {" AND ".join(where)}
      ),
      {canonical_agent_events_cte(source="candidate_agent_events")}
      SELECT id, session_id, agent_id, timestamp, event_type, content
      FROM canonical_agent_events
      ORDER BY timestamp DESC
      LIMIT {int(limit)}
    """
    con = _connect(duckdb_path)
    try:
        results = _row_to_dict(con.execute(sql, params))
    finally:
        con.close()
    return {
        "query": query,
        "scoped": scoped,
        "since": since,
        "default_since_days": int(default_since_days) if not scoped else None,
        "results": results,
    }


# --- drover_files_touched -----------------------------------------------------


def drover_files_touched(
    *,
    duckdb_path: Path,
    task_id: str,
    since: Optional[str] = None,
) -> dict:
    """Return distinct file paths touched by Edit/Write/Bash tool_use blocks.

    Reads ``raw_data`` (JSON-encoded) from agent_events, walks any
    ``tool_use_blocks`` entries, and pulls ``input.file_path`` /
    ``input.path``.
    """
    where = ["task_id = ?"]
    params: list[Any] = [task_id]
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    sql = f"""
      WITH {canonical_agent_events_cte()}
      SELECT DISTINCT raw_data
      FROM canonical_agent_events
      WHERE {" AND ".join(where)} AND raw_data IS NOT NULL AND raw_data <> '{{}}'
    """
    con = _connect(duckdb_path)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    files: set[str] = set()
    for (raw,) in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for block in data.get("tool_use_blocks", []) or []:
            if not isinstance(block, dict):
                continue
            inp = block.get("input") or {}
            for key in ("file_path", "path"):
                v = inp.get(key)
                if isinstance(v, str) and v:
                    files.add(v)
    return {"task_id": task_id, "files": sorted(files)}


# --- drover_task_status -------------------------------------------------------


def drover_session_close(
    *,
    duckdb_path: Path,
    session_id: str,
) -> dict:
    """Enqueue a summarize_jobs row for ``session_id``.

    Idempotent: if a row already exists in any non-terminal state, no-op.
    Returns ``{"session_id": ..., "status": "queued"|"already_queued"|"already_done"}``.
    """
    con = open_duckdb_connection(duckdb_path)
    try:
        existing = con.execute(
            "SELECT status FROM summarize_jobs WHERE session_id=?",
            [session_id],
        ).fetchone()
        if existing:
            status = existing[0]
            if status == "done":
                return {"session_id": session_id, "status": "already_done"}
            if status in ("pending", "running"):
                return {"session_id": session_id, "status": "already_queued"}
            # status == 'errored' — re-queue
            con.execute(
                "UPDATE summarize_jobs SET status='pending', last_error=NULL, updated_at=now() WHERE session_id=?",
                [session_id],
            )
            return {"session_id": session_id, "status": "requeued"}
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) VALUES (?, 'pending', 0)",
            [session_id],
        )
    finally:
        con.close()
    return {"session_id": session_id, "status": "queued"}


# --- drover_project_brief -----------------------------------------------------


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
    con = _connect(duckdb_path)
    try:
        rows = _row_to_dict(
            con.execute(
                """SELECT project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                      key_files, open_questions, next_steps_md,
                      session_count, last_activity_at, generator_model, generated_at
               FROM project_briefs WHERE project_key=?""",
                [project_key],
            )
        )
        if not rows:
            return None
        row = rows[0]
        latest = con.execute(
            """SELECT MAX(activity_at)
                 FROM (
                   SELECT TRY_CAST(ss.ended_at AS TIMESTAMP) AS activity_at
                     FROM session_summaries ss
                     JOIN tasks t USING (task_id)
                    WHERE t.repo_owner = ? AND t.repo_name = ?
                      AND ss.session_id <> 'unknown_openclaw'
                   UNION ALL
                   SELECT TRY_CAST(ss.generated_at AS TIMESTAMP) AS activity_at
                     FROM session_summaries ss
                     JOIN tasks t USING (task_id)
                    WHERE t.repo_owner = ? AND t.repo_name = ?
                      AND ss.session_id <> 'unknown_openclaw'
                   UNION ALL
                   SELECT TRY_CAST(last_activity_at AS TIMESTAMP) AS activity_at
                     FROM tasks
                    WHERE repo_owner = ? AND repo_name = ?
                 )""",
            [owner, name, owner, name, owner, name],
        ).fetchone()
        latest_activity = latest[0] if latest else None
    finally:
        con.close()

    generated_at = _parse_datetime(row.get("generated_at"))
    last_activity_at = _parse_datetime(row.get("last_activity_at"))
    latest_activity_dt = _parse_datetime(latest_activity)
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


def drover_recent_sessions(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    project_key: Optional[str] = None,
    limit: int = 5,
) -> dict:
    """Return the N most recent session_summaries for a repository.

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
    con = _connect(duckdb_path)
    try:
        # Prefer the task-keyed join (covers tasks linked via task_id);
        # union with the repo-direct path so unlinked summaries still surface.
        cur = con.execute(
            f"""WITH {canonical_agent_events_cte()}
               SELECT DISTINCT ss.session_id, ss.agent_id, ss.ended_at,
                      ss.summary_md, ss.next_steps_md, ss.open_questions,
                      ss.files_touched, ss.generator_model
               FROM session_summaries ss
               LEFT JOIN tasks t USING (task_id)
               WHERE ss.session_id <> 'unknown_openclaw'
                 AND (
                   (t.repo_owner = ? AND t.repo_name = ?)
                   OR ss.session_id IN (
                       SELECT DISTINCT session_id FROM canonical_agent_events
                       WHERE repo_owner=? AND repo_name=?
                     )
                 )
               ORDER BY ss.ended_at DESC
               LIMIT ?""",
            [owner, name, owner, name, int(limit)],
        )
        sessions = _row_to_dict(cur)
    finally:
        con.close()
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


def drover_open_loops(
    *,
    duckdb_path: Path,
    container_type: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Return context containers with a known next action or open loop."""
    where = [
        "(COALESCE(next_action, '') <> '' OR COALESCE(open_loop, '') <> '')",
    ]
    params: list[Any] = []
    if container_type:
        where.append("container_type = ?")
        params.append(normalize_context_type(container_type))
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


def drover_resume_context(
    *,
    duckdb_path: Path,
    context_id: Optional[str] = None,
    label: Optional[str] = None,
    max_summaries: int = 5,
) -> Optional[dict]:
    """Return a resumable context container plus linked session summaries."""
    container = drover_context_brief(
        duckdb_path=duckdb_path, context_id=context_id, label=label
    )
    if not container:
        return None
    session_ids = container.get("session_ids") or []
    summaries: list[dict] = []
    if session_ids:
        con = _connect(duckdb_path)
        try:
            summaries = _row_to_dict(
                con.execute(
                    """SELECT session_id, agent_id, ended_at, summary_md,
                              next_steps_md, open_questions, status, generator_model
                         FROM session_summaries
                        WHERE session_id = ANY(?::VARCHAR[])
                        ORDER BY ended_at DESC NULLS LAST
                        LIMIT ?""",
                    [session_ids, int(max_summaries)],
                )
            )
        finally:
            con.close()
    return {"context": container, "session_summaries": summaries}


# --- drover_project_activity --------------------------------------------------


def _project_activity_span_dates(
    con: duckdb.DuckDBPyConnection, *, since: Optional[str]
) -> list[str]:
    if since:
        rows = con.execute(
            """
            SELECT DISTINCT date
            FROM spans
            WHERE date <> '_seed'
              AND date >= strftime(TRY_CAST(? AS TIMESTAMPTZ), '%Y-%m-%d')
              AND start_time >= ?
            ORDER BY date
            """,
            [since, since],
        ).fetchall()
    else:
        rows = con.execute("""
            SELECT DISTINCT date
            FROM spans
            WHERE date <> '_seed'
              AND date >= strftime(current_date - INTERVAL 9 DAY, '%Y-%m-%d')
              AND start_time >= now() - INTERVAL 7 DAY
            ORDER BY date
            """).fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def drover_project_activity(
    *,
    duckdb_path: Path,
    project_key: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Return span-level activity grouped by repo/project for the given window.

    ``project_key`` filters to ``<owner>/<name>`` (e.g. ``arniesaha/drover``).
    ``since`` is an ISO-8601 lower bound (default: last 7 days).
    Results are sorted by cost descending so the most expensive repos appear first.
    """
    con = _connect(duckdb_path)
    try:
        span_dates = _project_activity_span_dates(con, since=since)
        if not span_dates:
            return {"window_since": since or "last 7 days", "rows": []}

        bounded_spans = "\nUNION ALL\n".join(
            "SELECT * FROM spans_enriched_for_date(?)" for _ in span_dates
        )
        where_parts = []
        params: list = list(span_dates)
        if since:
            where_parts.append("start_time >= ?")
            params.append(since)
        else:
            where_parts.append("start_time >= now() - INTERVAL 7 DAY")
        if project_key:
            owner, _, name = project_key.partition("/")
            where_parts.append("repo_owner = ? AND repo_name = ?")
            params.extend([owner, name])

        where = " AND ".join(where_parts)
        rows = _row_to_dict(
            con.execute(
                f"""WITH bounded_spans AS (
                  {bounded_spans}
                )
                SELECT
                  COALESCE(repo_owner || '/' || repo_name, project, 'unknown') AS project_key,
                  repo_owner,
                  repo_name,
                  project          AS agentweave_project,
                  agent_id,
                  COUNT(*)         AS span_count,
                  SUM(cost_usd)    AS cost_usd,
                  MIN(start_time)  AS first_span,
                  MAX(start_time)  AS last_span
                FROM bounded_spans
               WHERE {where}
               GROUP BY 1, 2, 3, 4, 5
               ORDER BY cost_usd DESC NULLS LAST
               LIMIT ?""",
                params + [int(limit)],
            )
        )
    finally:
        con.close()
    return {"window_since": since or "last 7 days", "rows": rows}


# --- drover_fleet_status ------------------------------------------------------


def drover_fleet_status(
    *,
    duckdb_path: Path,
) -> dict:
    """Return a snapshot of every currently-active session with repo context.

    "Active" means: an agent_event within the last 30 minutes and no session
    summary (open session). Combines active_sessions with tasks for repo info.
    """
    con = _connect(duckdb_path)
    try:
        sessions = _row_to_dict(con.execute("""SELECT
                 a.session_id,
                 a.agent_id,
                 a.task_id,
                 COALESCE(a.repo_owner, t.repo_owner) AS repo_owner,
                 COALESCE(a.repo_name,  t.repo_name)  AS repo_name,
                 COALESCE(a.branch,     t.branch)     AS branch,
                 a.started_at,
                 a.last_event_at,
                 a.event_count
               FROM active_sessions a
               LEFT JOIN tasks t ON a.task_id = t.task_id
               ORDER BY a.last_event_at DESC"""))
        # Pull the latest event content snippet for each session.
        for s in sessions:
            sid = s["session_id"]
            snippet = con.execute(
                f"""WITH {canonical_agent_events_cte()}
                    SELECT content FROM canonical_agent_events
                    WHERE session_id = ? AND role = 'user' AND content IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1""",
                [sid],
            ).fetchone()
            s["latest_user_message"] = (snippet[0] or "")[:300] if snippet else None
    finally:
        con.close()
    return {"active_sessions": sessions, "count": len(sessions)}


# --- drover_data_quality ------------------------------------------------------


def drover_data_quality(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    hours: int = 24,
    deep: bool = False,
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
    )


def drover_pipeline_observatory(
    *,
    duckdb_path: Path,
    incoming_dir: Optional[Path] = None,
    max_artifacts: int = 10,
    max_projects: int = 10,
) -> dict:
    """Return saved artifact and project-readiness drilldown for Drover."""
    quality = quality_snapshot(
        duckdb_path=duckdb_path,
        incoming_dir=incoming_dir,
        deep=False,
    )
    return pipeline_observatory_snapshot(
        duckdb_path=duckdb_path,
        runtime_audit=quality.get("runtime_audit", {}),
        max_artifacts=max_artifacts,
        max_projects=max_projects,
    )


# --- drover_recall (semantic search) -----------------------------------------


def drover_recall(
    *,
    duckdb_path: Path,
    query_embedding: Optional[list[float]] = None,
    limit: int = 5,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> dict:
    """Return session summaries ranked by cosine similarity to ``query_embedding``.

    The embedding has to be supplied by the caller — Drover's MCP layer
    doesn't (yet) call out to the embedder for ad-hoc query encoding;
    that's a one-line addition once we settle on always-on availability.
    Until then, the typical caller is the brief worker or a CLI script
    that owns the embedder.

    Filters by ``(repo_owner, repo_name)`` if both are provided.
    """
    if not query_embedding:
        raise ValueError("recall: query_embedding is required (list[float])")
    where = ["se.embedding IS NOT NULL", "se.dim = ?"]
    params: list[Any] = [len(query_embedding)]
    if repo_owner and repo_name:
        where.append(
            "ss.session_id IN (SELECT DISTINCT session_id FROM canonical_agent_events "
            "WHERE repo_owner=? AND repo_name=?)"
        )
        params.extend([repo_owner, repo_name])
    # list_cosine_similarity (vs array_cosine_similarity) accepts
    # variable-length lists, which is what we store. Span and summary hits are
    # unioned with an explicit source_type so callers never mistake raw-span
    # recall for synthesized session-summary recall.
    span_where = ["spe.embedding IS NOT NULL", "spe.dim = ?"]
    span_params: list[Any] = [len(query_embedding)]
    if repo_owner and repo_name:
        span_where.append("spe.repo_owner = ? AND spe.repo_name = ?")
        span_params.extend([repo_owner, repo_name])
    sql = f"""
        WITH {canonical_agent_events_cte()}, hits AS (
            SELECT 'session_summary' AS source_type,
                   ss.session_id AS session_id,
                   NULL::VARCHAR AS span_id,
                   ss.agent_id AS agent_id,
                   ss.ended_at AS ended_at,
                   ss.summary_md AS summary_md,
                   ss.next_steps_md AS next_steps_md,
                   ss.open_questions AS open_questions,
                   NULL::VARCHAR AS source_text,
                   list_cosine_similarity(se.embedding::DOUBLE[], ?::DOUBLE[]) AS score
            FROM session_embeddings se
            JOIN session_summaries ss USING (session_id)
            WHERE {' AND '.join(where)}
            UNION ALL
            SELECT 'span' AS source_type,
                   spe.session_id AS session_id,
                   spe.span_id AS span_id,
                   spe.agent_id AS agent_id,
                   NULL::TIMESTAMP AS ended_at,
                   NULL::VARCHAR AS summary_md,
                   NULL::VARCHAR AS next_steps_md,
                   []::VARCHAR[] AS open_questions,
                   spe.source_text AS source_text,
                   list_cosine_similarity(spe.embedding::DOUBLE[], ?::DOUBLE[]) AS score
            FROM span_embeddings spe
            WHERE {' AND '.join(span_where)}
        )
        SELECT source_type, session_id, span_id, agent_id, ended_at, summary_md,
               next_steps_md, open_questions, source_text, score
        FROM hits
        ORDER BY score DESC
        LIMIT {int(limit)}
    """
    con = _connect(duckdb_path)
    try:
        results = _row_to_dict(
            con.execute(sql, [query_embedding, *params, query_embedding, *span_params])
        )
    finally:
        con.close()
    return {"results": results, "limit": int(limit)}


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

    return generate_active_brief(
        duckdb_path,
        session_id,
        backend=backend,
        backend_config=backend_config,
        max_age_seconds=max_age_seconds,
    )


def drover_task_status(
    *,
    duckdb_path: Path,
    task_id: str,
) -> Optional[dict]:
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
            return None
        # Refresh aggregates from views (don't trust tasks.session_count)
        ev = con.execute(
            f"""WITH {canonical_agent_events_cte()}
               SELECT count(DISTINCT session_id), count(DISTINCT agent_id),
                      max(timestamp)
               FROM canonical_agent_events WHERE task_id = ?""",
            [task_id],
        ).fetchone()
        latest_summary = _row_to_dict(
            con.execute(
                """SELECT session_id, agent_id, summary_md, ended_at
               FROM session_summaries
               WHERE task_id = ?
               ORDER BY ended_at DESC LIMIT 1""",
                [task_id],
            )
        )
    finally:
        con.close()

    out = task_rows[0]
    out["session_count"] = ev[0] or 0
    out["agent_count"] = ev[1] or 0
    out["last_activity_at"] = (
        ev[2].isoformat() if ev[2] else out.get("last_activity_at")
    )
    out["latest_summary"] = latest_summary[0] if latest_summary else None
    return out


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
