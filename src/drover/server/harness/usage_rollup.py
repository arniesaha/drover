"""Roll harness-stream token usage up to one row per session.

Track 3, slice 1 of the insights and usage telemetry design. ``harness_events``
carries per-message (claude-code) or cumulative (codex) usage payloads;
``session_totals`` already knows each harness's arithmetic. This module finds
sessions whose events have grown past the watermark stored on their
``session_usage`` row and re-rolls them.

The watermark is ``(count(*), COALESCE(max(seq), 0))`` over the session's
events. ``seq`` alone is not enough: legacy rows carry ``seq IS NULL``
(``harness/schema.py``), and ``seq`` is only monotonic within a session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from drover.server.db import control_plane_connection, control_plane_path
from drover.server.harness.usage import session_totals, usage_turn_count
from drover.server.harness.usage_sources import (
    SOURCE_HARNESS_EVENTS,
    upsert_source_usage,
)

log = logging.getLogger("drover.harness.usage_rollup")

SOURCE_UNOBSERVED = "unobserved"
DEFAULT_BATCH_SIZE = 200

_counter_lock = threading.Lock()
_malformed_payloads_total = 0
_rolled_sessions_total = 0


def malformed_payload_count() -> int:
    with _counter_lock:
        return _malformed_payloads_total


def rolled_session_count() -> int:
    with _counter_lock:
        return _rolled_sessions_total


def reset_counters_for_tests() -> None:
    global _malformed_payloads_total, _rolled_sessions_total
    with _counter_lock:
        _malformed_payloads_total = 0
        _rolled_sessions_total = 0


def _bump(rolled: int, malformed: int) -> None:
    global _malformed_payloads_total, _rolled_sessions_total
    with _counter_lock:
        _rolled_sessions_total += rolled
        _malformed_payloads_total += malformed


@dataclass(frozen=True)
class RollupReport:
    candidates: int
    rolled: int
    malformed_events: int


@dataclass(frozen=True)
class _Candidate:
    session_id: str
    host_id: str
    harness: str
    event_count: int
    max_seq: int


_CANDIDATES_SQL = """
WITH per_session AS (
  SELECT session_id,
         count(*) AS event_count,
         COALESCE(max(seq), 0) AS max_seq
  FROM harness_events
  GROUP BY session_id
)
SELECT p.session_id, s.host_id, s.harness, p.event_count, p.max_seq
FROM per_session p
JOIN harness_sessions s USING (session_id)
LEFT JOIN session_usage_sources u
  ON u.session_id = p.session_id AND u.source = 'harness_events'
WHERE u.session_id IS NULL
   OR p.event_count <> u.source_event_count
   OR p.max_seq <> u.source_seq
ORDER BY p.session_id
LIMIT ?
"""

_EVENTS_SQL = """
SELECT seq, payload_json
FROM harness_events
WHERE session_id = ?
ORDER BY COALESCE(seq, 0), created_at, event_id
"""


def _load_events(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Return registry event envelopes (column ``seq`` wins) and the malformed count.

    ``harness_events.payload_json`` already stores the whole event envelope
    (``{"payload": {...usage...}, "seq": ..., "type": ..., ...}``), so the
    parsed JSON is passed through as-is rather than re-nested under another
    ``payload`` key -- that used to shadow the real ``$.payload.usage`` path
    that ``_usage_records`` (``usage.py``) looks for. The ``seq`` column is
    spread in last because many rows carry a column value but no envelope
    ``seq`` at all.
    """
    return _parse_event_rows(con.execute(_EVENTS_SQL, [session_id]).fetchall())


def _parse_event_rows(
    rows: list[tuple[Any, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Parse fetched rows. Pure, so a caller can run it off the lock.

    This is the expensive half: one ``json.loads`` per event, and a busy
    session has tens of thousands. Running it inside a control-plane window
    is what let a single rollup pass hold the lock for 238 seconds while
    ``/harness`` waited behind it (#334).
    """
    events: list[dict[str, Any]] = []
    malformed = 0
    for seq, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        events.append({**payload, "seq": seq})
    return events, malformed


def _rollup_one(
    con: duckdb.DuckDBPyConnection, candidate: _Candidate, now: datetime
) -> int:
    events, malformed = _load_events(con, candidate.session_id)
    totals = session_totals(candidate.harness, events)
    upsert_source_usage(
        con,
        session_id=candidate.session_id,
        source=SOURCE_HARNESS_EVENTS,
        usage=totals,
        turn_count=usage_turn_count(events),
        exact=bool(totals.exact) and malformed == 0,
        source_seq=candidate.max_seq,
        source_event_count=candidate.event_count,
        host_id=candidate.host_id,
        harness=candidate.harness,
        observed_at=now,
    )
    if malformed:
        log.warning(
            "usage rollup skipped %d malformed payload(s) in session %s",
            malformed,
            candidate.session_id,
        )
    return malformed


def load_pending_candidates(
    con: duckdb.DuckDBPyConnection, *, limit: int = DEFAULT_BATCH_SIZE
) -> list["_Candidate"]:
    """Candidates only. One short read, so the lock is released before work."""
    rows = con.execute(_CANDIDATES_SQL, [limit]).fetchall()
    return [
        _Candidate(str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in rows
    ]


def fetch_event_rows(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> list[tuple[Any, Any]]:
    """The session's raw event rows, unparsed."""
    return con.execute(_EVENTS_SQL, [session_id]).fetchall()


def store_rolled_usage(
    con: duckdb.DuckDBPyConnection,
    candidate: "_Candidate",
    events: list[dict[str, Any]],
    malformed: int,
    now: datetime,
) -> None:
    """Write one session's totals. One short write, off the parsing path."""
    totals = session_totals(candidate.harness, events)
    upsert_source_usage(
        con,
        session_id=candidate.session_id,
        source=SOURCE_HARNESS_EVENTS,
        usage=totals,
        turn_count=usage_turn_count(events),
        exact=bool(totals.exact) and malformed == 0,
        source_seq=candidate.max_seq,
        source_event_count=candidate.event_count,
        host_id=candidate.host_id,
        harness=candidate.harness,
        observed_at=now,
    )
    if malformed:
        log.warning(
            "usage rollup skipped %d malformed payload(s) in session %s",
            malformed,
            candidate.session_id,
        )


def rollup_pending_sessions(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> RollupReport:
    """Re-roll every session whose events moved past its watermark.

    ``con`` is an open control-plane connection. Each upsert autocommits, so a
    crash mid-pass leaves earlier sessions rolled and later ones still
    candidates for the next pass.
    """
    rollup_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rows = con.execute(_CANDIDATES_SQL, [limit]).fetchall()
    candidates = [
        _Candidate(str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in rows
    ]
    malformed_total = 0
    for candidate in candidates:
        malformed_total += _rollup_one(con, candidate, rollup_at)
    _bump(len(candidates), malformed_total)
    return RollupReport(
        candidates=len(candidates),
        rolled=len(candidates),
        malformed_events=malformed_total,
    )


_last_pass_lock = threading.Lock()
_last_pass_seconds: float | None = None


def last_pass_seconds() -> float | None:
    with _last_pass_lock:
        return _last_pass_seconds


class UsageRollupWorker:
    """Periodically roll new harness usage into ``session_usage``.

    Same shape as ``LiveRecapWorker``: one daemon thread, one short
    control-plane window per pass, a pass that raises never kills the loop.
    """

    def __init__(
        self,
        *,
        duckdb_path: Path,
        poll_interval_s: float = 60.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.poll_interval_s = poll_interval_s
        self.batch_size = batch_size
        self.last_pass_seconds: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-usage-rollup", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - the next pass must still run.
                log.exception("usage rollup pass crashed")
            self._stop.wait(self.poll_interval_s)

    def drain_once(self) -> RollupReport:
        global _last_pass_seconds
        started = time.monotonic()
        registry_path = control_plane_path(self.duckdb_path)
        rollup_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # One short window per step instead of one window for the whole pass.
        # `/harness` needs this same lock, so the old shape made the fleet
        # endpoint wait for however long it took to parse every event of every
        # candidate: measured at 47s and 238s on this hub while a session was
        # busy, which is exactly when someone is watching it (#334).
        with control_plane_connection(registry_path) as con:
            candidates = load_pending_candidates(con, limit=self.batch_size)

        malformed_total = 0
        for candidate in candidates:
            with control_plane_connection(registry_path) as con:
                rows = fetch_event_rows(con, candidate.session_id)
            # Off the lock: one json.loads per event, and a busy session has
            # tens of thousands of them.
            events, malformed = _parse_event_rows(rows)
            malformed_total += malformed
            with control_plane_connection(registry_path) as con:
                store_rolled_usage(con, candidate, events, malformed, rollup_at)

        _bump(len(candidates), malformed_total)
        report = RollupReport(
            candidates=len(candidates),
            rolled=len(candidates),
            malformed_events=malformed_total,
        )
        elapsed = time.monotonic() - started
        self.last_pass_seconds = elapsed
        with _last_pass_lock:
            _last_pass_seconds = elapsed
        if report.rolled:
            log.info(
                "usage rollup: %d session(s) in %.2fs (%d malformed payload(s))",
                report.rolled,
                elapsed,
                report.malformed_events,
            )
        return report
