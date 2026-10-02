"""Rebuildable memory projections; control session identity remains authoritative.

Phase 3 can replace these analytical tables without changing the resolution
contract. No inferred cwd/time matching: only explicit native IDs are links.
"""

from __future__ import annotations

import json
from typing import Any

from drover.models import AgentEvent, Message
from drover.server.ingest import _row_from_event, _upsert_tasks
from drover.task_id import compute_task_id


def ensure_memory_schema(con: Any) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS memory_session_identity (
        harness_session_id VARCHAR PRIMARY KEY, native_session_id VARCHAR,
        summary_session_id VARCHAR, harness VARCHAR, task_id VARCHAR
    )""")
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory_projection_pending (session_id VARCHAR PRIMARY KEY)"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS control_memory_events (
        id VARCHAR PRIMARY KEY, session_id VARCHAR, timestamp TIMESTAMPTZ,
        date VARCHAR, agent_id VARCHAR, event_type VARCHAR, role VARCHAR,
        content VARCHAR, repo_owner VARCHAR, repo_name VARCHAR, branch VARCHAR,
        task_id VARCHAR, principal_id VARCHAR, input_tokens BIGINT,
        output_tokens BIGINT, cache_read_tokens BIGINT, cache_write_tokens BIGINT,
        reasoning_tokens BIGINT, dedup_key VARCHAR, raw_data VARCHAR, source VARCHAR
    )""")


def project_control_event(event: dict, session: dict) -> dict:
    """Project the full envelope, never the truncated UI preview."""
    payload = json.loads(event.get("payload_json") or "{}")
    kind = event.get("normalized_type") or event["event_type"]
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    item = inner.get("item") or payload.get("item")
    # Older structured streams wrapped explicit file changes in status events.
    # Their concrete change list is substantive evidence, not lifecycle metadata.
    if (
        kind == "status"
        and isinstance(item, dict)
        and item.get("type") == "file_change"
        and item.get("changes")
    ):
        kind = "file_change"
    role = {
        "user_input": "user",
        "assistant_output": "assistant",
        "tool_action": "tool",
        "tool_result": "tool",
        "command": "tool",
        "file_change": "tool",
    }.get(kind)
    content = payload.get("text") or payload.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content)
    raw = {
        **payload,
        **inner,
        "source": "control",
        "normalized_type": kind,
        "normalized_source": event.get("normalized_source"),
        "control_event_id": event["event_id"],
        "native_session_id": session.get("native_session_id"),
    }
    for key in ("cwd", "_repo_owner", "_repo_name", "gitBranch"):
        if session.get(key):
            raw.setdefault(key, session[key])
    for key, target in [
        ("repo_owner", "_repo_owner"),
        ("repo_name", "_repo_name"),
        ("branch", "gitBranch"),
    ]:
        if session.get(key):
            raw.setdefault(target, session[key])
    usage = inner.get("usage") or payload.get("usage")
    ev = AgentEvent(
        id=event["event_id"],
        session_id=event["session_id"],
        timestamp=event["created_at"],
        agent_id=session.get("harness") or "unknown",
        event_type=kind,
        message=Message(role=role, content=content) if role else None,
        raw_data=raw,
        token_usage=usage if isinstance(usage, dict) else None,
    )
    row = _row_from_event(ev, session.get("task_id"))
    # Outbox IDs are globally stable and preserve even identical adjacent turns.
    row["dedup_key"] = "control:" + event["event_id"]
    row["source"] = "control"
    return row


def read_memory_sessions(control: Any) -> dict[str, dict]:
    """Detach the small authoritative identity snapshot before analytical work."""
    cur = control.execute(
        "SELECT session_id, native_session_id, summary_session_id, harness, repo_owner, repo_name, branch, cwd FROM harness_sessions"
    )
    cols = [d[0] for d in cur.description]
    sessions = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    for session in sessions.values():
        session["task_id"] = compute_task_id(
            None,
            session.get("repo_owner"),
            session.get("repo_name"),
            session.get("branch"),
        )
    return sessions


def apply_memory_links(control: Any, links: list[dict]) -> None:
    """Retry-safe link effects; never overwrite a native ID learned concurrently."""
    for link in links:
        if link.get("native_session_id"):
            control.execute(
                "UPDATE harness_sessions SET native_session_id=? WHERE session_id=? AND native_session_id IS NULL",
                [link["native_session_id"], link["session_id"]],
            )
        if link.get("summary_session_id"):
            control.execute(
                "UPDATE harness_sessions SET summary_session_id=? WHERE session_id=? AND summary_session_id IS DISTINCT FROM ?",
                [
                    link["summary_session_id"],
                    link["session_id"],
                    link["summary_session_id"],
                ],
            )


def refresh_memory_projection(
    analytics: Any, sessions: dict[str, dict], *, store_path=None
) -> list[dict]:
    """Project published events without borrowing a control connection or lock."""
    from drover.server.ledger import memory_store_available
    from drover.server.memory_store import MemoryRepository
    from drover.server.summarizer.jobs import (
        enqueue_summary_generation,
        source_version_for_session,
    )

    repo = (
        MemoryRepository(store_path)
        if store_path and memory_store_available(store_path)
        else None
    )
    ensure_memory_schema(analytics)
    links = []
    for sid, native in analytics.execute(
        "SELECT harness_session_id, native_session_id FROM memory_session_identity WHERE native_session_id IS NOT NULL"
    ).fetchall():
        if sid in sessions and not sessions[sid].get("native_session_id"):
            sessions[sid]["native_session_id"] = native
            links.append({"session_id": sid, "native_session_id": native})
    # Backfill explicit IDs carried by older published event envelopes. No heuristic identity guesses.
    exported_columns = analytics.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='harness_exported_events'"
    ).fetchall()
    if ("payload_json",) in exported_columns:
        cur = analytics.execute(
            """SELECT e.session_id, e.payload_json FROM harness_exported_events e
            WHERE NOT EXISTS (SELECT 1 FROM control_memory_events c WHERE c.id=e.event_id)
            ORDER BY e.created_at, e.event_id LIMIT 1000"""
        )
        for sid, raw in cur.fetchall():
            payload = json.loads(raw or "{}")
            native = payload.get("native_session_id")
            if not native and isinstance(payload.get("payload"), dict):
                native = payload["payload"].get("native_session_id")
            if (
                sid in sessions
                and not sessions[sid].get("native_session_id")
                and isinstance(native, str)
                and native.strip()
            ):
                sessions[sid]["native_session_id"] = native.strip()
                links.append({"session_id": sid, "native_session_id": native.strip()})
    for session in sessions.values():
        analytics.execute(
            """INSERT INTO memory_session_identity VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (harness_session_id) DO UPDATE SET
            native_session_id=excluded.native_session_id,
            summary_session_id=excluded.summary_session_id,
            harness=excluded.harness, task_id=excluded.task_id""",
            [
                session["session_id"],
                session["native_session_id"],
                session["summary_session_id"],
                session["harness"],
                session["task_id"],
            ],
        )
    available = analytics.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_name='harness_exported_events' AND column_name='session_id'"
    ).fetchone()[0]
    if available:
        cur = analytics.execute("""SELECT e.* FROM harness_exported_events e
            WHERE NOT EXISTS (SELECT 1 FROM control_memory_events c WHERE c.id=e.event_id)
            ORDER BY created_at, event_id LIMIT 1000""")
        cols = [d[0] for d in cur.description]
        events = [dict(zip(cols, r)) for r in cur.fetchall()]
        for event in events:
            session = sessions.get(event["session_id"])
            if session is None:
                continue
            row = project_control_event(event, session)
            analytics.execute("BEGIN")
            try:
                analytics.execute(
                    "INSERT INTO control_memory_events ("
                    + ",".join(row)
                    + ") VALUES ("
                    + ",".join("?" for _ in row)
                    + ") ON CONFLICT DO NOTHING",
                    list(row.values()),
                )
                analytics.execute(
                    "INSERT INTO memory_projection_pending VALUES (?) ON CONFLICT DO NOTHING",
                    [row["session_id"]],
                )
                _upsert_tasks(analytics, [row])
                analytics.execute("COMMIT")
            except BaseException:
                analytics.execute("ROLLBACK")
                raise

    for (sid,) in analytics.execute(
        "SELECT session_id FROM memory_projection_pending"
    ).fetchall():
        if repo is None:
            continue  # Keep the durable intent until a memory store is available.
        enqueue_summary_generation(
            store_path, sid, source_version_for_session(analytics, sid)
        )
        analytics.execute(
            "DELETE FROM memory_projection_pending WHERE session_id=?", [sid]
        )
    # Summary publication is a separate idempotent control-plane link effect.
    # A crash after summary completion is repaired by the next export pass.
    for sid, session in sessions.items():
        if repo is None:
            continue
        # A harness artifact wins over its explicit native alias.
        artifact = repo.summary(sid) or (
            repo.summary(session["native_session_id"])
            if session.get("native_session_id")
            else None
        )
        if artifact and session.get("summary_session_id") != artifact.session_id:
            links.append({"session_id": sid, "summary_session_id": artifact.session_id})
            analytics.execute(
                "UPDATE memory_session_identity SET summary_session_id=? WHERE harness_session_id=?",
                [artifact.session_id, sid],
            )

    return links


def resolve_session(con: Any, session_id: str, *, store_path=None) -> dict:
    """Distinguish unknown identity, missing native link, and missing artifact."""
    from drover.server.ledger import memory_store_available
    from drover.server.memory_store import MemoryRepository

    repo = (
        MemoryRepository(store_path)
        if store_path and memory_store_available(store_path)
        else None
    )
    rows = con.execute(
        """SELECT harness_session_id, native_session_id, summary_session_id
        FROM memory_session_identity WHERE ? IN (harness_session_id, native_session_id, summary_session_id)""",
        [session_id],
    ).fetchall()
    if len(rows) > 1:
        return {
            "status": "unmapped",
            "session_id": session_id,
            "reason": "ambiguous_identity",
        }
    if rows:
        harness, native, summary = rows[0]
        exists = con.execute(
            "SELECT 1 FROM control_memory_events WHERE session_id=? LIMIT 1", [harness]
        ).fetchone()
        artifact = repo.summary(harness) if repo is not None else None
        if artifact is None and repo is not None and native:
            artifact = repo.summary(native)
        summary = artifact.session_id if artifact else summary
        status = (
            "ok" if exists or artifact else ("unavailable" if native else "unmapped")
        )
        return {
            "status": status,
            "session_id": harness,
            "requested_session_id": session_id,
            "source": "control",
            "harness_session_id": harness,
            "native_session_id": native,
            "summary_session_id": summary,
            "mapping_status": "mapped" if native else "unmapped",
        }
    exists = con.execute(
        "SELECT 1 FROM agent_events WHERE session_id=? LIMIT 1", [session_id]
    ).fetchone()
    if not exists:
        exists = repo.summary(session_id) if repo is not None else None
    return {
        "status": "ok" if exists else "unknown",
        "session_id": session_id,
        "requested_session_id": session_id,
        "source": "native" if exists else None,
    }
