"""Regenerate derived memory from canonical sessions (#480 bootstrap).

Phase 3 rebuilds derived memory instead of migrating it: the new PostgreSQL
tables start empty, and this walks the canonical session record in the
analytical store and opens the jobs that refill them.

* A session with no current summary gets a ``summarize_session`` job. Its
  embedding (and its project's brief) follow automatically when the summary
  commits -- the summarizer enqueues them in the same transaction.
* A session whose summary is current but which has no embedding in the
  configured model gets an ``embed_session`` job directly. That covers a hub
  whose summaries survived but whose pgvector table was just created.

Requeued jobs run at a lower priority than live work, so a backfill never
starves the session that ended a minute ago, and enqueueing is rate-limited so
a large ``--since`` window does not land as one burst of row locks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from drover.event_identity import canonical_agent_events_cte
from drover.server.db import open_duckdb_connection
from drover.server.ledger import EMBED_SESSION, SUMMARIZE_SESSION, JobLedger
from drover.server.memory_store import EmbeddingStore, MemoryRepository
from drover.server.summarizer.jobs import source_version_for_session

log = logging.getLogger("drover.memory_requeue")

#: Below live work (priority 0), so a backfill yields to new sessions.
REQUEUE_PRIORITY = -10

#: Parser placeholders, not sessions (see the active_sessions view).
_PLACEHOLDER_SESSIONS = ("unknown_openclaw", "unknown_session")


@dataclass
class RequeueReport:
    since: str
    dry_run: bool
    substantive_only: bool
    sessions_scanned: int = 0
    skipped_not_substantive: int = 0
    summarize: dict[str, int] = field(default_factory=dict)
    embed: dict[str, int] = field(default_factory=dict)
    already_current: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "since": self.since,
            "dry_run": self.dry_run,
            "substantive_only": self.substantive_only,
            "sessions_scanned": self.sessions_scanned,
            "skipped_not_substantive": self.skipped_not_substantive,
            "summarize": dict(sorted(self.summarize.items())),
            "embed": dict(sorted(self.embed.items())),
            "already_current": self.already_current,
        }


@dataclass(frozen=True)
class _CanonicalSession:
    session_id: str
    user_messages: int
    assistant_messages: int


def canonical_sessions(
    duckdb_path: Path,
    *,
    since: date,
    session_ids: Optional[Iterable[str]] = None,
) -> list[_CanonicalSession]:
    """Sessions with canonical activity on or after ``since``, oldest first."""
    params: list[object] = [
        # `date` is the hive key from the event's own clock: a day of slack
        # keeps a session that straddles midnight (or a skewed host).
        (since - timedelta(days=1)).isoformat(),
        datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc),
    ]
    scoped = ""
    if session_ids is not None:
        ids = sorted({str(s) for s in session_ids if s})
        if not ids:
            return []
        scoped = f" AND session_id IN ({', '.join('?' for _ in ids)})"
        params.extend(ids)
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        rows = con.execute(
            f"""WITH window_events AS (
                  SELECT * FROM agent_events
                   WHERE date >= ?
                     AND TRY_CAST(timestamp AS TIMESTAMPTZ) >= ?{scoped}
                ),
                {canonical_agent_events_cte(source="window_events")}
                SELECT session_id,
                       count(*) FILTER (WHERE role = 'user'
                         AND NULLIF(trim(COALESCE(content, '')), '') IS NOT NULL),
                       count(*) FILTER (WHERE role = 'assistant'
                         AND NULLIF(trim(COALESCE(content, '')), '') IS NOT NULL),
                       min(TRY_CAST(timestamp AS TIMESTAMPTZ)) AS first_seen
                  FROM canonical_agent_events
                 WHERE session_id IS NOT NULL
                   AND session_id NOT IN ({', '.join('?' for _ in _PLACEHOLDER_SESSIONS)})
                 GROUP BY session_id
                 ORDER BY first_seen, session_id""",
            [*params, *_PLACEHOLDER_SESSIONS],
        ).fetchall()
    finally:
        con.close()
    return [_CanonicalSession(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in rows]


def _source_versions(duckdb_path: Path, session_ids: list[str]) -> dict[str, str]:
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        return {sid: source_version_for_session(con, sid) for sid in session_ids}
    finally:
        con.close()


def requeue_memory(
    duckdb_path: Path,
    *,
    since: date,
    substantive_only: bool = False,
    dry_run: bool = False,
    embedding_model: Optional[str] = None,
    session_ids: Optional[Iterable[str]] = None,
    rate_per_second: float = 20.0,
    batch_size: int = 200,
    sleep: Callable[[float], None] = time.sleep,
) -> RequeueReport:
    """Open summarize/embed jobs for canonical sessions since ``since``.

    ``substantive_only`` keeps sessions with at least one non-empty user
    message and one non-empty assistant message: a session made of status
    rows has nothing to summarize (#468). ``embedding_model`` enables the
    embed-only path; without it (embeddings disabled) only summaries are
    requeued. ``session_ids`` targets specific sessions and forces a fresh
    generation even when the same source version already succeeded.
    """
    targeted = session_ids is not None
    report = RequeueReport(
        since=since.isoformat(), dry_run=dry_run, substantive_only=substantive_only
    )
    ledger = JobLedger(duckdb_path)
    repo = MemoryRepository(duckdb_path)
    embeddings = (
        EmbeddingStore(duckdb_path, model=embedding_model) if embedding_model else None
    )
    sessions = canonical_sessions(duckdb_path, since=since, session_ids=session_ids)
    report.sessions_scanned = len(sessions)
    if substantive_only:
        kept = [s for s in sessions if s.user_messages > 0 and s.assistant_messages > 0]
        report.skipped_not_substantive = len(sessions) - len(kept)
        sessions = kept

    interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
    for start in range(0, len(sessions), max(1, batch_size)):
        batch = [s.session_id for s in sessions[start : start + batch_size]]
        versions = _source_versions(duckdb_path, batch)
        summaries = repo.summaries(batch)
        embedded = (
            embeddings.embedded_session_ids(batch) if embeddings is not None else set()
        )
        for session_id in batch:
            version = versions[session_id]
            summary = summaries.get(session_id)
            if summary is None or summary.source_version != version or targeted:
                kind, bucket = SUMMARIZE_SESSION, report.summarize
            elif embeddings is not None and session_id not in embedded:
                kind, bucket = EMBED_SESSION, report.embed
            else:
                report.already_current += 1
                continue
            if dry_run:
                bucket["would_enqueue"] = bucket.get("would_enqueue", 0) + 1
                continue
            outcome = ledger.enqueue(
                kind,
                session_id,
                source_version=version,
                priority=REQUEUE_PRIORITY,
                force=targeted,
            )
            bucket[outcome] = bucket.get(outcome, 0) + 1
            if interval:
                sleep(interval)
    log.info("memory requeue: %s", report.as_dict())
    return report
