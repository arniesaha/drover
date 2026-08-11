"""Durable queue operations for incremental live session recaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb


@dataclass(frozen=True)
class LiveRecap:
    """The latest durable recap projection for one live session."""

    session_id: str
    text: str
    source_seq: int
    generated_at: datetime
    generator_model: str | None


def enqueue_live_recap(
    con: duckdb.DuckDBPyConnection, session_id: str, source_seq: int
) -> bool:
    """Queue a newer recap generation without letting stale events reset it."""
    row = con.execute(
        """INSERT INTO live_recap_jobs
          (session_id, desired_source_seq, status, attempts, last_error,
           enqueued_at, updated_at, next_run_at, stream_publish_needed)
        VALUES (?, ?, 'pending', 0, NULL, now(), now(), NULL, TRUE)
        ON CONFLICT (session_id) DO UPDATE SET
          desired_source_seq=excluded.desired_source_seq,
          status='pending', attempts=0, last_error=NULL,
          updated_at=now(), next_run_at=NULL, stream_publish_needed=TRUE
        WHERE live_recap_jobs.desired_source_seq < excluded.desired_source_seq
        RETURNING session_id""",
        [session_id, source_seq],
    ).fetchone()
    return row is not None


def publish_live_recap_generation(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    source_seq: int,
    stream: object | None,
) -> bool:
    """Publish one durable generation with at-least-once semantics."""
    if stream is None:
        return False
    pending = con.execute(
        """SELECT 1 FROM live_recap_jobs
             WHERE session_id=?
               AND desired_source_seq=?
               AND stream_publish_needed""",
        [session_id, source_seq],
    ).fetchone()
    if pending is None:
        return False
    stream.add({"session_id": session_id, "source_seq": source_seq})  # type: ignore[attr-defined]
    con.execute(
        """UPDATE live_recap_jobs SET stream_publish_needed=FALSE
             WHERE session_id=?
               AND desired_source_seq=?
               AND stream_publish_needed""",
        [session_id, source_seq],
    )
    return True


def flush_live_recap_publications(
    con: duckdb.DuckDBPyConnection, stream: object | None, *, limit: int = 100
) -> int:
    """Retry stream publications left pending by a failed prior process."""
    if stream is None:
        return 0
    rows = con.execute(
        """SELECT session_id, desired_source_seq FROM live_recap_jobs
             WHERE stream_publish_needed
             ORDER BY enqueued_at ASC
             LIMIT ?""",
        [max(1, int(limit))],
    ).fetchall()
    return sum(
        publish_live_recap_generation(con, session_id, source_seq, stream)
        for session_id, source_seq in rows
    )


def latest_live_recaps(
    con: duckdb.DuckDBPyConnection, session_ids: list[str]
) -> dict[str, LiveRecap]:
    """Return the latest persisted recap for each requested session."""
    if not session_ids:
        return {}
    placeholders = ", ".join("?" for _ in session_ids)
    rows = con.execute(
        "SELECT session_id, recap_text, source_seq, generated_at, generator_model "
        f"FROM live_session_recaps WHERE session_id IN ({placeholders})",
        session_ids,
    ).fetchall()
    return {
        session_id: LiveRecap(
            session_id=session_id,
            text=recap_text,
            source_seq=source_seq,
            generated_at=generated_at,
            generator_model=generator_model,
        )
        for session_id, recap_text, source_seq, generated_at, generator_model in rows
    }
