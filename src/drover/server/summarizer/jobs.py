"""Source-versioned summary generations on the PostgreSQL job ledger (#480).

A session's *source version* is a hash of stable facts about its canonical
events (read from analytical DuckDB or projected PostgreSQL control events).
Each distinct version is one
summary generation, and enqueueing it is a ``summarize_session`` job in the
one authoritative ledger (:mod:`drover.server.ledger`).

The DuckDB ``summarize_jobs`` state machine that used to live here is gone,
and with it the in-process writer lock that serialized its transitions
(#308, #460): PostgreSQL row locks on ``pipeline_jobs`` make concurrent
enqueue/complete safe across threads and processes. The retry budget, the
backoff and the dead-letter streak cap (``SUMMARY_MAX_DEAD_LETTERS`` here,
once) are owned by ``ledger.POLICIES`` now.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.ledger import SUMMARIZE_SESSION, JobLedger, memory_store_available
from drover.server.summarizer.derive import SUBSTANTIVE_SQL

log = logging.getLogger("drover.summarizer.jobs")


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
             FROM canonical_agent_events WHERE {SUBSTANTIVE_SQL}""",
        [session_id],
    ).fetchone()
    event_count, max_timestamp, max_dedup_key = row or (0, None, None)
    return source_version_from_facts(event_count, max_timestamp, max_dedup_key)


def source_version_from_facts(
    event_count: int, max_timestamp: datetime | None, max_dedup_key: str | None
) -> str:
    """Shared generation hash for analytical and control-plane event readers."""
    stable_facts = json.dumps(
        [
            int(event_count or 0),
            (
                max_timestamp.astimezone(timezone.utc).isoformat()
                if max_timestamp is not None
                else None
            ),
            max_dedup_key,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(stable_facts.encode("utf-8")).hexdigest()


def enqueue_summary_generation(
    store_path: str | Path, session_id: str, source_version: str
) -> str:
    """Open the summary job for one source generation; return the ledger outcome.

    The ledger decides what a generation earns: a new ``source_version``
    replaces a waiting job (fresh budget) or supersedes a running one; the
    same version that already succeeded or dead-lettered earns nothing
    (``already_done``/``already_failed``); and once a session has failed
    ``max_failed_streak`` generations in a row no further one opens
    (``suppressed``) until an operator requeues it.

    Without a PostgreSQL control store derived memory is unavailable, and
    this is a logged no-op returning ``"unavailable"`` rather than an error:
    ingest must keep working on a hub that has not moved to PostgreSQL.
    """
    if not memory_store_available(store_path):
        log.debug(
            "summary for session %s not enqueued: derived memory requires the "
            "PostgreSQL control store",
            session_id,
        )
        return "unavailable"
    return JobLedger(store_path).enqueue(
        SUMMARIZE_SESSION, session_id, source_version=source_version
    )
