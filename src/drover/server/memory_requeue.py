"""Regenerate derived memory from canonical sessions (#480 bootstrap).

Phase 3 rebuilds derived memory instead of migrating it: the new PostgreSQL
tables start empty, and this walks the canonical session record in the
PostgreSQL control store and opens the jobs that refill them. Native sessions
collected only from files are excluded; no analytical connection is opened.

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

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection
from drover.server.ledger import EMBED_SESSION, SUMMARIZE_SESSION, JobLedger
from drover.server.memory_identity import project_control_event, read_memory_sessions
from drover.server.memory_store import EmbeddingStore, MemoryRepository
from drover.server.summarizer.jobs import source_version_from_facts

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
    through: str
    sessions_scanned: int = 0
    skipped_not_substantive: int = 0
    summarize: dict[str, int] = field(default_factory=dict)
    embed: dict[str, int] = field(default_factory=dict)
    already_current: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "since": self.since,
            "through": self.through,
            "source": "postgres_control",
            "native_only_sessions": "excluded",
            "counts_by_reason": {
                "summarize": sum(self.summarize.values()),
                "embed_only": sum(self.embed.values()),
                "skipped_non_substantive": self.skipped_not_substantive,
                "already_current": self.already_current,
            },
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
    source_version: str


def canonical_sessions(
    store_path: Path,
    *,
    since: date,
    session_ids: Optional[Iterable[str]] = None,
    through: Optional[datetime] = None,
) -> list[_CanonicalSession]:
    """Read control sessions and full envelopes from one PostgreSQL snapshot.

    Explicit native/summary aliases resolve to the harness identity, which is
    the subject consumed by the control-event summary worker. File-only native
    sessions have no control record and are deliberately excluded.
    """
    if not is_postgres_control_store(store_path):
        raise ValueError("memory requeue requires a PostgreSQL control store")
    ids = set(session_ids) if session_ids is not None else None
    if ids == set():
        return []
    cutoff = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    through = through or datetime.now(timezone.utc)
    with control_plane_connection(store_path) as con:
        # Identity and events must agree even while the hub is ingesting.
        con.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            identities = read_memory_sessions(con)
            selected = [
                sid
                for sid, session in identities.items()
                if sid not in _PLACEHOLDER_SESSIONS
                and (
                    ids is None
                    or ids.intersection(
                        {
                            sid,
                            session.get("native_session_id"),
                            session.get("summary_session_id"),
                        }
                    )
                )
            ]
            if not selected:
                return []
            cur = con.execute(
                """SELECT e.* FROM harness_events e
                   WHERE e.session_id = ANY(?)
                     AND e.created_at <= ?
                     AND EXISTS (SELECT 1 FROM harness_events recent
                         WHERE recent.session_id=e.session_id
                           AND recent.created_at >= ? AND recent.created_at <= ?)
                   ORDER BY e.created_at, e.event_id""",
                [selected, through, cutoff, through],
            )
            columns = [d[0] for d in cur.description]
            events = [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            con.execute("ROLLBACK")
    grouped: dict[str, list[dict]] = {}
    for event in events:
        sid = event["session_id"]
        grouped.setdefault(sid, []).append(
            project_control_event(event, identities[sid])
        )
    result = []
    for sid, rows in grouped.items():
        substantive = [r for r in rows if _substantive(r)]
        version = source_version_from_facts(
            len(substantive),
            max((r["timestamp"] for r in substantive), default=None),
            max((r["dedup_key"] for r in substantive), default=None),
        )
        result.append(
            _CanonicalSession(
                sid,
                sum(r["role"] == "user" and bool(r["content"].strip()) for r in rows),
                sum(
                    r["role"] == "assistant" and bool(r["content"].strip())
                    for r in rows
                ),
                version,
            )
        )
    return result


def _substantive(row: dict) -> bool:
    """Match summarizer.derive.SUBSTANTIVE_SQL on projected control rows."""
    kind = row["event_type"]
    if kind in {"status", "system_event", "metadata"}:
        return False
    raw = json.loads(row["raw_data"] or "{}")
    return bool(
        (row["role"] in {"user", "assistant", "tool"} and row["content"].strip(" "))
        or (
            kind
            in {"tool_call", "tool_action", "tool_result", "file_change", "command"}
            and raw
        )
        or raw.get("tool_use_blocks")
    )


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
    through = datetime.now(timezone.utc)
    report = RequeueReport(
        since=since.isoformat(),
        dry_run=dry_run,
        substantive_only=substantive_only,
        through=through.isoformat(),
    )
    ledger = JobLedger(duckdb_path)
    repo = MemoryRepository(duckdb_path)
    embeddings = (
        EmbeddingStore(duckdb_path, model=embedding_model) if embedding_model else None
    )
    sessions = canonical_sessions(
        duckdb_path, since=since, session_ids=session_ids, through=through
    )
    report.sessions_scanned = len(sessions)
    if substantive_only:
        kept = [s for s in sessions if s.user_messages > 0 and s.assistant_messages > 0]
        report.skipped_not_substantive = len(sessions) - len(kept)
        sessions = kept

    interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
    batch_size = max(1, batch_size)
    for start in range(0, len(sessions), batch_size):
        batch = [s.session_id for s in sessions[start : start + batch_size]]
        versions = {
            s.session_id: s.source_version for s in sessions[start : start + batch_size]
        }
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
