"""Durable state transitions for source-versioned session summaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable, Literal

import duckdb

from drover.event_identity import canonical_agent_events_cte

SUMMARY_MAX_ATTEMPTS = 5


def _duckdb_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def source_version_for_session(con: duckdb.DuckDBPyConnection, session_id: str) -> str:
    """Hash stable facts describing one immutable session-event generation."""
    row = con.execute(
        f"""WITH session_agent_events AS (
               SELECT * FROM agent_events WHERE session_id = ?
             ),
             {canonical_agent_events_cte(source="session_agent_events")}
             SELECT count(*),
                    max(TRY_CAST(timestamp AS TIMESTAMPTZ)),
                    max(dedup_key)
             FROM canonical_agent_events""",
        [session_id],
    ).fetchone()
    event_count, max_timestamp, max_dedup_key = row or (0, None, None)
    stable_facts = json.dumps(
        [
            int(event_count or 0),
            max_timestamp.isoformat() if max_timestamp is not None else None,
            max_dedup_key,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(stable_facts.encode("utf-8")).hexdigest()


def enqueue_summary_generation(
    con: duckdb.DuckDBPyConnection, session_id: str, source_version: str
) -> bool:
    """Open a runnable generation only when the immutable source changed."""
    try:
        con.execute("BEGIN TRANSACTION")
        legacy = con.execute(
            """UPDATE summarize_jobs
                  SET source_version = ?, updated_at = now()
                WHERE session_id = ? AND source_version IS NULL
                RETURNING session_id""",
            [source_version, session_id],
        ).fetchone()
        if legacy is not None:
            # A null legacy version carries no evidence that its source changed.
            # Backfill its identity without resetting or republishing the generation.
            con.execute("COMMIT")
            return False

        row = con.execute(
            """INSERT INTO summarize_jobs
                 (session_id, status, attempts, source_version, max_attempts,
                  last_error, next_run_at, dead_lettered_at, updated_at,
                  stream_publish_needed)
                 VALUES (?, 'pending', 0, ?, ?, NULL, NULL, NULL, now(), TRUE)
                 ON CONFLICT (session_id) DO UPDATE SET
                   source_version = excluded.source_version,
                   status = 'pending',
                   attempts = 0,
                   max_attempts = excluded.max_attempts,
                   last_error = NULL,
                   next_run_at = NULL,
                   dead_lettered_at = NULL,
                   updated_at = now(),
                   stream_publish_needed = TRUE
                 WHERE summarize_jobs.source_version IS DISTINCT FROM excluded.source_version
                 RETURNING session_id""",
            [session_id, source_version, SUMMARY_MAX_ATTEMPTS],
        ).fetchone()
        if row is not None:
            con.execute(
                """UPDATE embed_jobs SET status='superseded', updated_at=now()
                     WHERE session_id=?
                       AND source_version IS NOT NULL
                       AND source_version IS DISTINCT FROM ?
                       AND status <> 'superseded'""",
                [session_id, source_version],
            )
            con.execute(
                """UPDATE brief_jobs SET status='superseded', updated_at=now()
                     WHERE source_session_id=?
                       AND source_version IS NOT NULL
                       AND source_version IS DISTINCT FROM ?
                       AND status <> 'superseded'""",
                [session_id, source_version],
            )
        con.execute("COMMIT")
        return row is not None
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise


def publish_summary_generation(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    source_version: str,
    stream: object | None,
) -> bool:
    """Publish a durable pending generation with at-least-once semantics."""
    if stream is None:
        return False
    pending = con.execute(
        """SELECT 1 FROM summarize_jobs
             WHERE session_id=?
               AND source_version IS NOT DISTINCT FROM ?
               AND COALESCE(stream_publish_needed, FALSE)""",
        [session_id, source_version],
    ).fetchone()
    if pending is None:
        return False
    stream.add({"session_id": session_id, "source_version": source_version})
    con.execute(
        """UPDATE summarize_jobs SET stream_publish_needed=FALSE
             WHERE session_id=?
               AND source_version IS NOT DISTINCT FROM ?""",
        [session_id, source_version],
    )
    return True


def flush_summary_publications(
    con: duckdb.DuckDBPyConnection, stream: object | None, *, limit: int = 100
) -> int:
    """Retry durable summary-generation publications from the worker poll path."""
    if stream is None:
        return 0
    rows = con.execute(
        """SELECT session_id, source_version FROM summarize_jobs
             WHERE COALESCE(stream_publish_needed, FALSE)
             ORDER BY enqueued_at ASC
             LIMIT ?""",
        [max(1, int(limit))],
    ).fetchall()
    published = 0
    for session_id, source_version in rows:
        if publish_summary_generation(con, session_id, source_version, stream):
            published += 1
    return published


def finish_summary_failure(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    source_version: str,
    error: str,
    *,
    now: datetime,
    jitter: Callable[[float, float], float],
) -> Literal["retry_wait", "dead_lettered", "stale"]:
    """Spend one failure from the matching source generation's retry budget."""
    stored_now = _duckdb_timestamp(now)
    jitter_fraction = jitter(0, 0.2)
    updated = con.execute(
        """UPDATE summarize_jobs
              SET status = CASE
                    WHEN COALESCE(attempts, 0) + 1 >= COALESCE(max_attempts, ?)
                    THEN 'dead_lettered' ELSE 'retry_wait' END,
                  attempts = COALESCE(attempts, 0) + 1,
                  last_error = ?,
                  next_run_at = CASE
                    WHEN COALESCE(attempts, 0) + 1 >= COALESCE(max_attempts, ?)
                    THEN NULL
                    ELSE ? + (
                      LEAST(60 * POWER(2, COALESCE(attempts, 0)), 3600)
                      * (1 + ?)
                    ) * INTERVAL '1 second'
                  END,
                  dead_lettered_at = CASE
                    WHEN COALESCE(attempts, 0) + 1 >= COALESCE(max_attempts, ?)
                    THEN ? ELSE NULL END,
                  updated_at = ?
            WHERE session_id = ? AND source_version IS NOT DISTINCT FROM ?
            RETURNING status, next_run_at""",
        [
            SUMMARY_MAX_ATTEMPTS,
            error,
            SUMMARY_MAX_ATTEMPTS,
            stored_now,
            jitter_fraction,
            SUMMARY_MAX_ATTEMPTS,
            stored_now,
            stored_now,
            session_id,
            source_version,
        ],
    ).fetchone()
    if updated is None:
        return "stale"
    return updated[0]
