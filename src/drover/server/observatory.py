"""Read-only Drover Pipeline Observatory snapshots.

Saved artifacts -- session summaries, project briefs and session embeddings --
are derived memory in the PostgreSQL control store (#480); tasks still come
from the analytical DuckDB. Without a PostgreSQL control store there are no
artifacts, and the snapshot says so instead of failing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from drover.server.adoption import adoption_snapshot
from drover.server.db import open_duckdb_connection
from drover.server.ledger import memory_store_available
from drover.server.memory_store import MemoryRepository, vector_status

MEMORY_UNAVAILABLE_DETAIL = (
    "derived memory requires the PostgreSQL control store; this hub runs a "
    "DuckDB control store, so no summaries, briefs or embeddings exist"
)


def _coerce(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {key: _coerce(item) for key, item in value.items()}
    return value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    cols = [desc[0] for desc in cursor.description]
    return [
        {col: _coerce(value) for col, value in zip(cols, row)}
        for row in cursor.fetchall()
    ]


def _instant(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _preview(text: str | None, limit: int = 360) -> str | None:
    if text is None:
        return None
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _missing_summary_fields(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in ("summary_md", "next_steps_md", "last_user_prompt", "last_assistant"):
        if not str(row.get(field) or "").strip():
            missing.append(field)
    files = row.get("files_touched") or []
    questions = row.get("open_questions") or []
    if not files and not questions:
        missing.append("files_touched_or_open_questions")
    return missing


def _task_repos(
    con: duckdb.DuckDBPyConnection, task_ids: list[str]
) -> dict[str, tuple[Any, Any, Any]]:
    if not task_ids:
        return {}
    placeholders = ", ".join("?" for _ in task_ids)
    rows = con.execute(
        f"""SELECT task_id, repo_owner, repo_name, branch
              FROM tasks WHERE task_id IN ({placeholders})""",
        task_ids,
    ).fetchall()
    return {str(row[0]): (row[1], row[2], row[3]) for row in rows}


def _summary_artifacts(
    pg: Any, con: duckdb.DuckDBPyConnection, *, limit: int
) -> dict[str, Any]:
    rows = _rows(
        pg.execute(
            """
            SELECT session_id, task_id, agent_id, project_key, ended_at,
                   summary_generated_at AS generated_at,
                   summary_model AS generator_model, summary_status AS status,
                   summary_md, next_steps_md, last_user_prompt, last_assistant,
                   files_touched, open_questions
              FROM session_memory
             WHERE phase = 'final'
             ORDER BY COALESCE(summary_generated_at, ended_at) DESC NULLS LAST
             LIMIT ?
            """,
            [int(limit)],
        )
    )
    total, ready = pg.execute("""
        SELECT count(*),
               count(*) FILTER (
                 WHERE NULLIF(trim(COALESCE(summary_md, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(next_steps_md, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(last_user_prompt, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(last_assistant, '')), '') IS NOT NULL
                   AND (cardinality(files_touched) > 0 OR cardinality(open_questions) > 0)
               )
          FROM session_memory
         WHERE phase = 'final'
        """).fetchone()
    repos = _task_repos(
        con, sorted({str(r["task_id"]) for r in rows if r.get("task_id")})
    )
    latest = []
    for row in rows:
        missing = _missing_summary_fields(row)
        owner, name, branch = repos.get(str(row.get("task_id")), (None, None, None))
        if owner is None and row.get("project_key"):
            owner, _, name = str(row["project_key"]).partition("/")
        latest.append(
            {
                "session_id": row.get("session_id"),
                "task_id": row.get("task_id"),
                "agent_id": row.get("agent_id"),
                "repo_owner": owner,
                "repo_name": name,
                "branch": branch,
                "ended_at": row.get("ended_at"),
                "generated_at": row.get("generated_at"),
                "generator_model": row.get("generator_model"),
                "status": row.get("status"),
                "bundle_ready": not missing,
                "missing_bundle_fields": missing,
                "files_touched_count": len(row.get("files_touched") or []),
                "open_questions_count": len(row.get("open_questions") or []),
                "summary_preview": _preview(row.get("summary_md")),
                "next_steps_preview": _preview(row.get("next_steps_md")),
            }
        )
    return {"total": int(total or 0), "bundle_ready": int(ready or 0), "latest": latest}


def _brief_artifacts(repo: MemoryRepository, pg: Any, *, limit: int) -> dict[str, Any]:
    total = repo.counts(con=pg)["project_briefs"]
    latest = []
    for brief in repo.briefs(limit=limit):
        latest.append(
            {
                "project_key": brief.project_key,
                "repo_owner": brief.repo_owner,
                "repo_name": brief.repo_name,
                "session_count": brief.session_count,
                "last_activity_at": _coerce(brief.last_activity_at),
                "generated_at": _coerce(brief.generated_at),
                "generator_model": brief.generator_model,
                "key_files_count": len(brief.key_files or ()),
                "open_questions_count": len(brief.open_questions or ()),
                "brief_preview": _preview(brief.brief_md),
                "recent_themes_preview": _preview(brief.recent_themes_md),
                "next_steps_preview": _preview(brief.next_steps_md),
            }
        )
    return {"total": int(total or 0), "latest": latest}


def _project_readiness(
    pg: Any | None, con: duckdb.DuckDBPyConnection, *, limit: int
) -> list[dict[str, Any]]:
    task_projects = {
        (row["repo_owner"], row["repo_name"]): row for row in _rows(con.execute("""
                SELECT repo_owner, repo_name,
                       count(*) AS task_count,
                       sum(COALESCE(session_count, 0)) AS task_session_count,
                       max(last_activity_at) AS latest_task_activity_at
                  FROM tasks
                 WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL
                 GROUP BY repo_owner, repo_name
                """))
    }
    summary_projects: dict[tuple[Any, Any], dict[str, Any]] = {}
    briefs: dict[tuple[Any, Any], dict[str, Any]] = {}
    if pg is not None:
        # Summaries attribute to a project through session_memory.project_key,
        # which the summarizer stamps from the session's own events.
        embeddings_ready = vector_status(pg)[0]
        embedding_join = (
            "LEFT JOIN session_embeddings se ON se.session_id = sm.session_id"
            if embeddings_ready
            else ""
        )
        embedding_count = "count(DISTINCT se.session_id)" if embeddings_ready else "0"
        for row in _rows(pg.execute(f"""
                SELECT split_part(sm.project_key, '/', 1) AS repo_owner,
                       substr(sm.project_key, strpos(sm.project_key, '/') + 1) AS repo_name,
                       count(DISTINCT sm.session_id) AS summary_count,
                       {embedding_count} AS session_embedding_count,
                       max(COALESCE(sm.summary_generated_at, sm.ended_at)) AS latest_summary_at
                  FROM session_memory sm
                  {embedding_join}
                 WHERE sm.phase = 'final' AND strpos(COALESCE(sm.project_key, ''), '/') > 0
                 GROUP BY 1, 2
                """)):
            summary_projects[(row["repo_owner"], row["repo_name"])] = row
        for row in _rows(
            pg.execute("""SELECT repo_owner, repo_name, generated_at, generator_model
                     FROM project_briefs""")
        ):
            briefs[(row["repo_owner"], row["repo_name"])] = row

    floor = datetime.min.replace(tzinfo=timezone.utc)
    keys = set(task_projects) | set(summary_projects) | set(briefs)
    rows = []
    for key in keys:
        tp = task_projects.get(key, {})
        sp = summary_projects.get(key, {})
        pb = briefs.get(key, {})
        rows.append(
            {
                "repo_owner": key[0],
                "repo_name": key[1],
                "project_key": f"{key[0]}/{key[1]}",
                "task_count": int(tp.get("task_count") or 0),
                "task_session_count": int(tp.get("task_session_count") or 0),
                "latest_task_activity_at": tp.get("latest_task_activity_at"),
                "summary_count": int(sp.get("summary_count") or 0),
                "session_embedding_count": int(sp.get("session_embedding_count") or 0),
                "latest_summary_at": sp.get("latest_summary_at"),
                # Span embeddings are out of the core path (#473); the keys
                # stay so the payload shape does not change.
                "span_count": 0,
                "span_embedding_count": 0,
                "latest_span_at": None,
                "project_brief_generated_at": pb.get("generated_at"),
                "project_brief_model": pb.get("generator_model"),
            }
        )
    rows.sort(
        key=lambda r: (
            _instant(r["latest_task_activity_at"])
            or _instant(r["latest_summary_at"])
            or _instant(r["project_brief_generated_at"])
            or floor
        ),
        reverse=True,
    )
    projects = []
    for row in rows[: int(limit)]:
        summary_count = row["summary_count"]
        session_embedding_count = row["session_embedding_count"]
        project_brief_ready = bool(row.get("project_brief_generated_at"))
        projects.append(
            {
                **row,
                "project_brief_ready": project_brief_ready,
                "summary_embedding_ready": (
                    summary_count > 0 and session_embedding_count >= summary_count
                ),
                "span_embedding_ready": True,
                "ready": bool(
                    summary_count > 0
                    and project_brief_ready
                    and session_embedding_count >= summary_count
                ),
            }
        )
    return projects


def _empty_artifacts() -> dict[str, Any]:
    return {
        "session_summaries": {"total": 0, "bundle_ready": 0, "latest": []},
        "project_briefs": {"total": 0, "latest": []},
    }


def pipeline_observatory_snapshot(
    *,
    duckdb_path: Path,
    runtime_audit: dict[str, Any] | None = None,
    max_artifacts: int = 10,
    max_projects: int = 10,
    role: str = "diagnostic",
    memory_store_path: Path | None = None,
) -> dict[str, Any]:
    """Return artifact and project drilldown for the Drover pipeline.

    ``role`` is the DuckDB connection profile; pass ``role="snapshot"`` only
    when ``duckdb_path`` is a private copy (see ``drover.server.db``). A
    private copy has no control store registered for it, so such callers
    pass the live path as ``memory_store_path`` (default: ``duckdb_path``).
    """
    store_path = Path(memory_store_path or duckdb_path)
    memory_available = memory_store_available(store_path)
    memory = {
        "available": memory_available,
        "detail": (
            "PostgreSQL control store"
            if memory_available
            else MEMORY_UNAVAILABLE_DETAIL
        ),
    }

    if not Path(duckdb_path).exists():
        return {
            "snapshot_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duckdb_path": str(duckdb_path),
            "artifacts": _empty_artifacts(),
            "projects": [],
            "memory": memory,
            "agent_adoption": adoption_snapshot(runtime_audit or {}),
        }

    con = open_duckdb_connection(duckdb_path, read_only=True, role=role)
    try:
        if memory_available:
            repo = MemoryRepository(store_path)
            with repo.connection() as pg:
                summaries = _summary_artifacts(pg, con, limit=max_artifacts)
                briefs = _brief_artifacts(repo, pg, limit=max_artifacts)
                projects = _project_readiness(pg, con, limit=max_projects)
            artifacts = {"session_summaries": summaries, "project_briefs": briefs}
        else:
            artifacts = _empty_artifacts()
            projects = _project_readiness(None, con, limit=max_projects)
    finally:
        con.close()

    return {
        "snapshot_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duckdb_path": str(duckdb_path),
        "artifacts": artifacts,
        "projects": projects,
        "memory": memory,
        "agent_adoption": adoption_snapshot(runtime_audit or {}),
    }
