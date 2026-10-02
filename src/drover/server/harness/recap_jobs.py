"""Live recaps as the live phase of session memory (#480).

A live recap is one ``recap_session`` job in the PostgreSQL job ledger
(``pipeline_jobs``) and, once generated, the live phase of the session's
``session_memory`` row. There is no recap-specific queue table and no Redis
delivery stream any more: the ledger is the one authoritative queue.

The registry enqueues inside the transaction that appends the turn-completion
event, on the same PostgreSQL control-plane connection, so the event and the
recap intent commit (or roll back) together. On a DuckDB control plane --
a local install without PostgreSQL -- derived memory, live recaps included,
is unavailable: enqueue is a no-op and reads return nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from drover.server.control_outbox import is_postgres_connection
from drover.server.ledger import (
    LIVE_STATUSES,
    RECAP_SESSION,
    JobLedger,
    memory_store_available,
)
from drover.server.memory_store import LiveRecap, MemoryRepository

__all__ = ["LiveRecap", "enqueue_live_recap", "latest_live_recaps"]

_ADVANCED = ("queued", "requeued")


def _live_job_seq(con: object, session_id: str) -> int | None:
    """The source sequence of the session's live recap job, locked for this txn.

    ``FOR UPDATE`` serializes two completions racing for one session: the
    second waits for the first to commit and then compares against its seq,
    so the forward-only check below cannot be interleaved.
    """
    row = con.execute(  # type: ignore[attr-defined]
        f"""SELECT source_version FROM pipeline_jobs
             WHERE job_kind = ? AND subject_key = ?
               AND status IN ({", ".join("?" for _ in LIVE_STATUSES)})
             FOR UPDATE""",
        [RECAP_SESSION, session_id, *LIVE_STATUSES],
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # A live job whose version is not a sequence was not written here;
        # treat it as older than anything so a real sequence replaces it.
        return -1


def _stored_recap_seq(con: object, session_id: str) -> int | None:
    row = con.execute(  # type: ignore[attr-defined]
        "SELECT recap_source_seq FROM session_memory WHERE session_id = ?",
        [session_id],
    ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def enqueue_live_recap(
    con: object, session_id: str, source_seq: int, *, store_path: str | Path
) -> bool:
    """Queue a recap at ``source_seq`` in the caller's transaction, forward only.

    ``con`` is the caller's open control-plane connection; on PostgreSQL the
    ledger enqueue joins its transaction. Returns True when a job now targets
    ``source_seq``.

    The ledger retargets a waiting job to whatever version is enqueued, so
    the forward-only rule lives here: a session whose live job, or whose
    stored recap, already covers ``source_seq`` or later is left alone. An
    out-of-order or replayed completion can therefore never move a session's
    recap backwards.
    """
    if not is_postgres_connection(con) or not memory_store_available(store_path):
        return False
    seq = int(source_seq)
    # Serialize the sequence comparison as well as the subsequent enqueue.
    JobLedger.lock_subject(con, RECAP_SESSION, session_id)
    live_seq = _live_job_seq(con, session_id)
    if live_seq is not None and live_seq >= seq:
        return False
    stored_seq = _stored_recap_seq(con, session_id)
    if stored_seq is not None and stored_seq >= seq:
        return False
    ledger = JobLedger(store_path)
    outcome = ledger.enqueue(
        RECAP_SESSION,
        session_id,
        source_version=str(seq),
        payload={"source_seq": seq},
        con=con,
    )
    if outcome == "already_queued":
        # On a caller connection the ledger reports a lost insert race as
        # "already_queued". The winner may hold an older sequence; now that
        # its row is committed and visible, advance it once more.
        live_seq = _live_job_seq(con, session_id)
        if live_seq is not None and live_seq < seq:
            outcome = ledger.enqueue(
                RECAP_SESSION,
                session_id,
                source_version=str(seq),
                payload={"source_seq": seq},
                con=con,
            )
    return outcome in _ADVANCED


def latest_live_recaps(
    store_path: str | Path, session_ids: Iterable[str]
) -> dict[str, LiveRecap]:
    """The latest generated recap for each requested session.

    Empty when memory is unavailable (DuckDB control plane): nothing that
    renders a fleet may fail because live recaps are off.
    """
    ids = [str(s) for s in session_ids if s]
    if not ids or not memory_store_available(store_path):
        return {}
    return MemoryRepository(store_path).live_recaps(ids)
