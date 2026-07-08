"""Read-only Drover Pipeline Observatory snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from drover.server.adoption import adoption_snapshot
from drover.server.db import open_duckdb_connection


def _coerce(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {key: _coerce(item) for key, item in value.items()}
    return value


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    cols = [desc[0] for desc in cursor.description]
    return [
        {col: _coerce(value) for col, value in zip(cols, row)}
        for row in cursor.fetchall()
    ]


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT count(*)
          FROM information_schema.tables
         WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


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


def _summary_artifacts(con: duckdb.DuckDBPyConnection, *, limit: int) -> dict[str, Any]:
    if not _table_exists(con, "session_summaries"):
        return {"total": 0, "bundle_ready": 0, "latest": []}
    rows = _rows(
        con.execute(
            """
            SELECT ss.session_id, ss.task_id, ss.agent_id, ss.ended_at, ss.generated_at,
                   ss.generator_model, ss.status, ss.summary_md, ss.next_steps_md,
                   ss.last_user_prompt, ss.last_assistant, ss.files_touched,
                   ss.open_questions,
                   t.repo_owner, t.repo_name, t.branch
              FROM session_summaries ss
              LEFT JOIN tasks t ON ss.task_id = t.task_id
             ORDER BY COALESCE(
                      TRY_CAST(ss.generated_at AS TIMESTAMPTZ),
                      TRY_CAST(ss.ended_at AS TIMESTAMPTZ)
                    ) DESC NULLS LAST
             LIMIT ?
            """,
            [int(limit)],
        )
    )
    total, ready = con.execute("""
        SELECT count(*),
               count(*) FILTER (
                 WHERE NULLIF(trim(COALESCE(summary_md, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(next_steps_md, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(last_user_prompt, '')), '') IS NOT NULL
                   AND NULLIF(trim(COALESCE(last_assistant, '')), '') IS NOT NULL
                   AND (
                     (files_touched IS NOT NULL AND array_length(files_touched) > 0)
                     OR (open_questions IS NOT NULL AND array_length(open_questions) > 0)
                   )
               )
          FROM session_summaries
        """).fetchone()
    latest = []
    for row in rows:
        missing = _missing_summary_fields(row)
        latest.append(
            {
                "session_id": row.get("session_id"),
                "task_id": row.get("task_id"),
                "agent_id": row.get("agent_id"),
                "repo_owner": row.get("repo_owner"),
                "repo_name": row.get("repo_name"),
                "branch": row.get("branch"),
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


def _brief_artifacts(con: duckdb.DuckDBPyConnection, *, limit: int) -> dict[str, Any]:
    if not _table_exists(con, "project_briefs"):
        return {"total": 0, "latest": []}
    total = con.execute("SELECT count(*) FROM project_briefs").fetchone()[0]
    rows = _rows(
        con.execute(
            """
            SELECT project_key, repo_owner, repo_name, session_count, last_activity_at,
                   generated_at, generator_model, key_files, open_questions,
                   brief_md, recent_themes_md, next_steps_md
              FROM project_briefs
             ORDER BY COALESCE(
                      TRY_CAST(generated_at AS TIMESTAMPTZ),
                      TRY_CAST(last_activity_at AS TIMESTAMPTZ)
                    ) DESC NULLS LAST
             LIMIT ?
            """,
            [int(limit)],
        )
    )
    latest = []
    for row in rows:
        latest.append(
            {
                "project_key": row.get("project_key"),
                "repo_owner": row.get("repo_owner"),
                "repo_name": row.get("repo_name"),
                "session_count": row.get("session_count"),
                "last_activity_at": row.get("last_activity_at"),
                "generated_at": row.get("generated_at"),
                "generator_model": row.get("generator_model"),
                "key_files_count": len(row.get("key_files") or []),
                "open_questions_count": len(row.get("open_questions") or []),
                "brief_preview": _preview(row.get("brief_md")),
                "recent_themes_preview": _preview(row.get("recent_themes_md")),
                "next_steps_preview": _preview(row.get("next_steps_md")),
            }
        )
    return {"total": int(total or 0), "latest": latest}


def _project_readiness(
    con: duckdb.DuckDBPyConnection, *, limit: int
) -> list[dict[str, Any]]:
    rows = _rows(
        con.execute(
            """
            WITH task_projects AS (
              SELECT repo_owner, repo_name,
                     count(*) AS task_count,
                     sum(COALESCE(session_count, 0)) AS task_session_count,
                     max(last_activity_at) AS latest_task_activity_at
                FROM tasks
               WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL
               GROUP BY repo_owner, repo_name
            ),
            summary_projects AS (
              SELECT t.repo_owner AS repo_owner,
                     t.repo_name AS repo_name,
                     count(DISTINCT ss.session_id) AS summary_count,
                     count(DISTINCT se.session_id) AS session_embedding_count,
                     max(COALESCE(
                       TRY_CAST(ss.generated_at AS TIMESTAMPTZ),
                       TRY_CAST(ss.ended_at AS TIMESTAMPTZ)
                     )) AS latest_summary_at
                FROM session_summaries ss
                JOIN tasks t ON ss.task_id = t.task_id
                LEFT JOIN session_embeddings se ON ss.session_id = se.session_id
               WHERE t.repo_owner IS NOT NULL
                 AND t.repo_name IS NOT NULL
               GROUP BY 1, 2
            ),
            span_projects AS (
              SELECT repo_owner, repo_name,
                     count(*) AS span_count,
                     count(*) AS span_embedding_count,
                     max(embedded_at) AS latest_span_at
                FROM span_embeddings
               WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL
               GROUP BY repo_owner, repo_name
            ),
            projects AS (
              SELECT repo_owner, repo_name FROM task_projects
              UNION
              SELECT repo_owner, repo_name FROM summary_projects
              UNION
              SELECT repo_owner, repo_name FROM span_projects
              UNION
              SELECT repo_owner, repo_name FROM project_briefs
            )
            SELECT p.repo_owner, p.repo_name,
                   p.repo_owner || '/' || p.repo_name AS project_key,
                   COALESCE(tp.task_count, 0) AS task_count,
                   COALESCE(tp.task_session_count, 0) AS task_session_count,
                   tp.latest_task_activity_at,
                   COALESCE(sp.summary_count, 0) AS summary_count,
                   COALESCE(sp.session_embedding_count, 0) AS session_embedding_count,
                   sp.latest_summary_at,
                   COALESCE(spanp.span_count, 0) AS span_count,
                   COALESCE(spanp.span_embedding_count, 0) AS span_embedding_count,
                   spanp.latest_span_at,
                   pb.generated_at AS project_brief_generated_at,
                   pb.generator_model AS project_brief_model
              FROM projects p
              LEFT JOIN task_projects tp USING (repo_owner, repo_name)
              LEFT JOIN summary_projects sp USING (repo_owner, repo_name)
              LEFT JOIN span_projects spanp USING (repo_owner, repo_name)
              LEFT JOIN project_briefs pb USING (repo_owner, repo_name)
             ORDER BY COALESCE(
                      TRY_CAST(tp.latest_task_activity_at AS TIMESTAMPTZ),
                      TRY_CAST(sp.latest_summary_at AS TIMESTAMPTZ),
                      TRY_CAST(spanp.latest_span_at AS TIMESTAMPTZ),
                      TRY_CAST(pb.generated_at AS TIMESTAMPTZ)
                    ) DESC NULLS LAST
             LIMIT ?
            """,
            [int(limit)],
        )
    )
    projects = []
    for row in rows:
        summary_count = int(row.get("summary_count") or 0)
        session_embedding_count = int(row.get("session_embedding_count") or 0)
        span_count = int(row.get("span_count") or 0)
        span_embedding_count = int(row.get("span_embedding_count") or 0)
        project_brief_ready = bool(row.get("project_brief_generated_at"))
        projects.append(
            {
                **row,
                "project_brief_ready": project_brief_ready,
                "summary_embedding_ready": (
                    summary_count > 0 and session_embedding_count >= summary_count
                ),
                "span_embedding_ready": (
                    span_count == 0 or span_embedding_count >= span_count
                ),
                "ready": bool(
                    summary_count > 0
                    and project_brief_ready
                    and session_embedding_count >= summary_count
                    and (span_count == 0 or span_embedding_count >= span_count)
                ),
            }
        )
    return projects


def pipeline_observatory_snapshot(
    *,
    duckdb_path: Path,
    runtime_audit: dict[str, Any] | None = None,
    max_artifacts: int = 10,
    max_projects: int = 10,
) -> dict[str, Any]:
    """Return artifact and project drilldown for the Drover pipeline."""

    if not Path(duckdb_path).exists():
        return {
            "snapshot_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duckdb_path": str(duckdb_path),
            "artifacts": {
                "session_summaries": {"total": 0, "bundle_ready": 0, "latest": []},
                "project_briefs": {"total": 0, "latest": []},
            },
            "projects": [],
            "agent_adoption": adoption_snapshot(runtime_audit or {}),
        }

    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        summaries = _summary_artifacts(con, limit=max_artifacts)
        briefs = _brief_artifacts(con, limit=max_artifacts)
        projects = _project_readiness(con, limit=max_projects)
    finally:
        con.close()

    return {
        "snapshot_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duckdb_path": str(duckdb_path),
        "artifacts": {
            "session_summaries": summaries,
            "project_briefs": briefs,
        },
        "projects": projects,
        "agent_adoption": adoption_snapshot(runtime_audit or {}),
    }
