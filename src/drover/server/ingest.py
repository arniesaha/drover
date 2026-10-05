"""Collector normalization and atomic control-outbox ingest.

Full native payloads, dedup and summary generation intent commit together.
Only the backend-selected exporter writes analytical storage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Set

from drover.attribution import enrich_raw_repo_attribution
from drover.dedup import make_dedup_key
from drover.models import AgentEvent
from drover.server.harness.usage import session_totals
from drover.server.redis_shadow import ShadowPublisher
from drover.task_id import compute_task_id

log = logging.getLogger("drover.ingest")


@dataclass
class IngestStats:
    read: int = 0
    inserted: int = 0
    skipped_dupes: int = 0
    errors: int = 0
    shadow_published: int = 0
    new_session_ids: Set[str] = field(default_factory=set)


def _extract_content(ev: AgentEvent) -> str:
    if ev.message and isinstance(ev.message.content, str):
        return ev.message.content
    if ev.message and isinstance(ev.message.content, list):
        # Concatenate text blocks for fingerprinting
        parts = []
        for block in ev.message.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _row_from_event(ev: AgentEvent, env_task_id: Optional[str]) -> dict:
    timestamp = (
        ev.timestamp
        if ev.timestamp.tzinfo
        else ev.timestamp.replace(tzinfo=timezone.utc)
    ).astimezone(timezone.utc)
    rd = enrich_raw_repo_attribution(ev.raw_data)
    rd.setdefault("source", "native")
    if ev.tool_calls:
        rd.setdefault(
            "tool_use_blocks",
            [{"name": t.tool_name, "input": t.input} for t in ev.tool_calls],
        )
    repo_owner = rd.get("_repo_owner")
    repo_name = rd.get("_repo_name")
    branch = rd.get("gitBranch") or rd.get("git_branch")
    content = _extract_content(ev)
    usage = (
        session_totals(None, [{"usage": ev.token_usage}])
        if ev.token_usage is not None
        else None
    )

    return {
        "id": ev.id,
        "session_id": ev.session_id,
        "timestamp": timestamp,
        "date": timestamp.strftime("%Y-%m-%d"),
        "agent_id": ev.agent_id,
        "event_type": ev.event_type,
        "role": ev.message.role if ev.message else None,
        "content": content,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "task_id": compute_task_id(env_task_id, repo_owner, repo_name, branch),
        "principal_id": rd.get("_principal_id"),
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "cache_read_tokens": usage.cache_read_tokens if usage else None,
        "cache_write_tokens": usage.cache_write_tokens if usage else None,
        "reasoning_tokens": usage.reasoning_tokens if usage else None,
        "dedup_key": make_dedup_key(
            timestamp.isoformat(),
            ev.agent_id,
            ev.session_id,
            ev.event_type,
            content,
        ),
        "raw_data": json.dumps(rd, default=str),
    }


def _propagate_unique_session_repo(
    rows: list[dict], env_task_id: Optional[str]
) -> None:
    """Fill unattributed rows from the only repo observed in the same session.

    Claude Code sessions often emit early hook/observer rows before a cwd is
    present, then later rows in the same session carry definitive repo metadata.
    If a session has exactly one repo in the incoming batch, propagating it is
    deterministic. Sessions with multiple repos are intentionally left alone.
    """
    repos_by_session: dict[tuple[str | None, str], set[tuple[str, str, str | None]]] = (
        {}
    )
    for row in rows:
        owner = row.get("repo_owner")
        name = row.get("repo_name")
        if owner and name:
            key = (row.get("agent_id"), row["session_id"])
            repos_by_session.setdefault(key, set()).add(
                (owner, name, row.get("branch"))
            )

    unique_repos = {
        key: next(iter(repos))
        for key, repos in repos_by_session.items()
        if len({(owner, name) for owner, name, _branch in repos}) == 1
    }

    for row in rows:
        if row.get("repo_owner") and row.get("repo_name"):
            continue
        repo = unique_repos.get((row.get("agent_id"), row["session_id"]))
        if not repo:
            continue
        owner, name, branch = repo
        row["repo_owner"] = owner
        row["repo_name"] = name
        row["branch"] = row.get("branch") or branch
        row["task_id"] = compute_task_id(env_task_id, owner, name, row.get("branch"))
        try:
            raw = json.loads(row.get("raw_data") or "{}")
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            raw.setdefault("_repo_owner", owner)
            raw.setdefault("_repo_name", name)
            if row.get("branch"):
                raw.setdefault("gitBranch", row["branch"])
            row["raw_data"] = json.dumps(raw, default=str)


def _iter_events(
    path: Path, env_task_id: Optional[str]
) -> Iterator[tuple[Optional[dict], Optional[str]]]:
    """Yield (row_dict, error_msg) — exactly one of the two is None per yield."""
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                ev = AgentEvent.model_validate(obj)
                yield _row_from_event(ev, env_task_id), None
            except Exception as e:
                yield None, f"line {lineno}: {e!r}"


def _is_valid_title(content: str) -> bool:
    """Return True only if *content* is suitable for use as a task title.

    Filters out:
    - XML/HTML fragments (content that starts with ``<``)
    - Suspiciously short strings (fewer than 10 characters after stripping)
    """
    stripped = content.strip()
    if stripped.startswith("<"):
        return False
    if len(stripped) < 10:
        return False
    return True


def _upsert_tasks(con, rows: list[dict]) -> None:
    """Insert any new (task_id) into tasks; update last_activity_at + session_count for existing."""
    seen: dict[str, dict] = {}
    for r in rows:
        tid = r["task_id"]
        if tid not in seen:
            seen[tid] = {
                "task_id": tid,
                "repo_owner": r.get("repo_owner"),
                "repo_name": r.get("repo_name"),
                "branch": r.get("branch"),
                "principal_id": r.get("principal_id"),
                "last_activity_at": r["timestamp"],
                "title": None,
            }
        else:
            if r["timestamp"] > seen[tid]["last_activity_at"]:
                seen[tid]["last_activity_at"] = r["timestamp"]
        # Capture the first user-role message as task title if not yet set.
        if not seen[tid]["title"] and r.get("role") == "user":
            raw_content = (r.get("content") or "").strip()
            if _is_valid_title(raw_content):
                seen[tid]["title"] = raw_content[:120].replace("\n", " ")

    for tid, t in seen.items():
        con.execute(
            """
            INSERT INTO tasks (task_id, repo_owner, repo_name, branch, principal_id,
                               status, created_at, last_activity_at, session_count, total_cost_usd,
                               title)
            VALUES (?, ?, ?, ?, ?, 'open', now(), ?, 0, 0.0, ?)
            ON CONFLICT (task_id) DO UPDATE SET
              last_activity_at = greatest(tasks.last_activity_at, EXCLUDED.last_activity_at),
              title = COALESCE(tasks.title, EXCLUDED.title)
            """,
            [
                t["task_id"],
                t["repo_owner"],
                t["repo_name"],
                t["branch"],
                t["principal_id"],
                t["last_activity_at"],
                t["title"],
            ],
        )


def ingest_file(
    path: Path,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    env_task_id: Optional[str] = None,
    shadow_publisher: Optional[ShadowPublisher] = None,
) -> IngestStats:
    """Ingest one JSONL file.  Returns IngestStats.

    When ``shadow_publisher`` is provided, newly-inserted rows are also mirrored
    to a Redis Stream (best-effort; the lakehouse stays the source of truth).
    """
    from drover.server.control_outbox import (
        canonical_payload,
        record_event_side_effects,
    )
    from drover.server.control_store import is_postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
    from drover.server.summarizer.jobs import source_version_from_facts

    postgres = is_postgres_control_store(duckdb_path)
    stats = IngestStats()
    parsed_rows = []
    for row, error in _iter_events(Path(path), env_task_id):
        stats.read += 1
        if error:
            stats.errors += 1
            log.warning("ingest %s: %s", path, error)
        else:
            parsed_rows.append(row)
    _propagate_unique_session_repo(parsed_rows, env_task_id)
    new_rows = []
    with control_plane_connection(duckdb_path) as con:
        con.execute("BEGIN")
        try:
            if postgres:
                # Summary completion locks its job before linking the session.
                # Follow that order; session -> job would deadlock with a worker.
                for sid in sorted({r["session_id"] for r in parsed_rows}):
                    con.execute(
                        """SELECT job_id FROM pipeline_jobs
                        WHERE job_kind='summarize_session' AND subject_key=?
                          AND status IN ('pending','running','retry_wait') FOR UPDATE""",
                        [sid],
                    )
            for agent in sorted({r["agent_id"] for r in parsed_rows}):
                con.execute(
                    """INSERT INTO harness_hosts
                    (host_id, display_name, kind, status, capabilities_json)
                    VALUES (?, ?, 'collector', 'offline', '{}')
                    ON CONFLICT (host_id) DO UPDATE SET status='offline'
                    WHERE harness_hosts.kind='collector'""",
                    [agent, agent],
                )
            sessions = {}
            for row in sorted(
                parsed_rows, key=lambda r: (r["agent_id"], r["session_id"])
            ):
                sid = row["session_id"]
                if sid not in sessions:
                    sessions[sid] = {
                        **row,
                        "first_at": row["timestamp"],
                        "last_at": row["timestamp"],
                    }
                else:
                    sessions[sid]["first_at"] = min(
                        sessions[sid]["first_at"], row["timestamp"]
                    )
                    sessions[sid]["last_at"] = max(
                        sessions[sid]["last_at"], row["timestamp"]
                    )
            for sid in sorted(sessions):
                row = sessions[sid]
                con.execute(
                    """INSERT INTO harness_sessions
                    (session_id, host_id, harness, command, status, started_at,
                     native_session_id, repo_owner, repo_name, branch, ended_at, last_activity)
                    VALUES (?, ?, ?, 'collector', 'completed', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                      native_session_id=COALESCE(harness_sessions.native_session_id, EXCLUDED.native_session_id),
                      status=CASE WHEN harness_sessions.command='collector' THEN 'completed' ELSE harness_sessions.status END,
                      started_at=CASE WHEN harness_sessions.command='collector' THEN least(harness_sessions.started_at, EXCLUDED.started_at) ELSE harness_sessions.started_at END,
                      ended_at=CASE WHEN harness_sessions.command='collector' THEN greatest(harness_sessions.ended_at, EXCLUDED.ended_at) ELSE harness_sessions.ended_at END,
                      last_activity=CASE WHEN harness_sessions.command='collector' THEN greatest(harness_sessions.last_activity, EXCLUDED.last_activity) ELSE harness_sessions.last_activity END
                    """,
                    [
                        row["session_id"],
                        row["agent_id"],
                        row["agent_id"],
                        row["first_at"],
                        row["session_id"],
                        row["repo_owner"],
                        row["repo_name"],
                        row["branch"],
                        row["last_at"],
                        row["last_at"],
                    ],
                )
            for sid in sorted({r["session_id"] for r in parsed_rows}):
                con.execute(
                    "SELECT session_id FROM harness_sessions WHERE session_id=?"
                    + (" FOR UPDATE" if postgres else ""),
                    [sid],
                )
            for row in parsed_rows:
                payload_json = canonical_payload(
                    {
                        "collector_row": {
                            **row,
                            "timestamp": row["timestamp"].isoformat(),
                        }
                    }
                )
                inserted = con.execute(
                    """INSERT INTO harness_events
                    (event_id, session_id, event_type, normalized_type, normalized_source,
                     content_preview, created_at, dedup_key, payload_json)
                    VALUES (?, ?, ?, ?, 'collector', ?, ?, ?, ?) ON CONFLICT DO NOTHING
                    RETURNING event_id""",
                    [
                        row["id"],
                        row["session_id"],
                        row["event_type"],
                        row["event_type"],
                        row["content"][:500],
                        row["timestamp"],
                        row["dedup_key"],
                        None if postgres else payload_json,
                    ],
                ).fetchone()
                if inserted is None:
                    stats.skipped_dupes += 1
                    continue
                # Keep the canonical normalized row, including full native usage,
                # tool payloads and explicit attribution; both sinks project it.
                record_event_side_effects(
                    con,
                    event_id=row["id"],
                    session_id=row["session_id"],
                    event_type=row["event_type"],
                    content_preview=row["content"][:500],
                    payload_json=payload_json,
                    created_at=row["timestamp"],
                    seq=None,
                )
                new_rows.append(row)
                stats.new_session_ids.add(row["session_id"])
            for sid in sorted(stats.new_session_ids) if postgres else []:
                facts = con.execute(
                    """SELECT count(*), max(created_at), max(dedup_key)
                    FROM harness_events WHERE session_id=?""",
                    [sid],
                ).fetchone()
                JobLedger(duckdb_path).enqueue(
                    SUMMARIZE_SESSION,
                    sid,
                    source_version=source_version_from_facts(*facts),
                    con=con,
                )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    stats.inserted = len(new_rows)
    if shadow_publisher is not None and new_rows:
        stats.shadow_published = shadow_publisher.publish_rows(new_rows)
    return stats
