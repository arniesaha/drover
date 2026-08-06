"""Durable state transitions for source-versioned session summaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
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
        return False

    row = con.execute(
        """INSERT INTO summarize_jobs
             (session_id, status, attempts, source_version, max_attempts,
              last_error, next_run_at, dead_lettered_at, updated_at)
             VALUES (?, 'pending', 0, ?, ?, NULL, NULL, NULL, now())
             ON CONFLICT (session_id) DO UPDATE SET
               source_version = excluded.source_version,
               status = 'pending',
               attempts = 0,
               max_attempts = excluded.max_attempts,
               last_error = NULL,
               next_run_at = NULL,
               dead_lettered_at = NULL,
               updated_at = now()
             WHERE summarize_jobs.source_version IS DISTINCT FROM excluded.source_version
             RETURNING session_id""",
        [session_id, source_version, SUMMARY_MAX_ATTEMPTS],
    ).fetchone()
    return row is not None


def finish_summary_failure(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    source_version: str,
    error: str,
    *,
    now: datetime,
    jitter: Callable[[float, float], float],
) -> Literal["retry_wait", "dead_lettered"]:
    """Spend one failure from the matching source generation's retry budget."""
    row = con.execute(
        """SELECT attempts, COALESCE(max_attempts, ?)
             FROM summarize_jobs
            WHERE session_id = ? AND source_version IS NOT DISTINCT FROM ?""",
        [SUMMARY_MAX_ATTEMPTS, session_id, source_version],
    ).fetchone()
    if row is None:
        # Claim paths reject stale deliveries before backend execution. Retain a
        # no-op guard here so a late failure can never mutate a newer generation.
        return "retry_wait"

    attempts = int(row[0] or 0) + 1
    max_attempts = int(row[1] or SUMMARY_MAX_ATTEMPTS)
    terminal = attempts >= max_attempts
    status = "dead_lettered" if terminal else "retry_wait"
    base_seconds = min(60 * (2 ** max(0, attempts - 1)), 3600)
    next_run_at = (
        None
        if terminal
        else _duckdb_timestamp(now)
        + timedelta(seconds=base_seconds + jitter(0, base_seconds * 0.2))
    )
    stored_now = _duckdb_timestamp(now)
    dead_lettered_at = stored_now if terminal else None
    con.execute(
        """UPDATE summarize_jobs
              SET status = ?, attempts = ?, last_error = ?, next_run_at = ?,
                  dead_lettered_at = ?, updated_at = ?
            WHERE session_id = ? AND source_version IS NOT DISTINCT FROM ?""",
        [
            status,
            attempts,
            error,
            next_run_at,
            dead_lettered_at,
            stored_now,
            session_id,
            source_version,
        ],
    )
    return status
