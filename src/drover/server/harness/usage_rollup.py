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

log = logging.getLogger("drover.harness.usage_rollup")

SOURCE_HARNESS_EVENTS = "harness_events"
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
LEFT JOIN session_usage u USING (session_id)
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

_UPSERT_SQL = """
INSERT INTO session_usage
  (session_id, host_id, harness, input_tokens, output_tokens,
   cache_read_tokens, cache_write_tokens, reasoning_tokens, turn_count,
   exact, source, source_seq, source_event_count, observed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (session_id) DO UPDATE SET
  host_id = excluded.host_id,
  harness = excluded.harness,
  input_tokens = excluded.input_tokens,
  output_tokens = excluded.output_tokens,
  cache_read_tokens = excluded.cache_read_tokens,
  cache_write_tokens = excluded.cache_write_tokens,
  reasoning_tokens = excluded.reasoning_tokens,
  turn_count = excluded.turn_count,
  exact = excluded.exact,
  source = excluded.source,
  source_seq = excluded.source_seq,
  source_event_count = excluded.source_event_count,
  observed_at = excluded.observed_at
"""


def _load_events(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Return ``{"seq", "payload"}`` records and the malformed-payload count."""
    events: list[dict[str, Any]] = []
    malformed = 0
    for seq, payload_json in con.execute(_EVENTS_SQL, [session_id]).fetchall():
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        events.append({"seq": seq, "payload": payload})
    return events, malformed


def _rollup_one(
    con: duckdb.DuckDBPyConnection, candidate: _Candidate, now: datetime
) -> int:
    events, malformed = _load_events(con, candidate.session_id)
    totals = session_totals(candidate.harness, events)
    observed = totals.observed
    con.execute(
        _UPSERT_SQL,
        [
            candidate.session_id,
            candidate.host_id,
            candidate.harness,
            totals.input_tokens,
            totals.output_tokens,
            totals.cache_read_tokens,
            totals.cache_write_tokens,
            totals.reasoning_tokens,
            usage_turn_count(events),
            bool(totals.exact) and malformed == 0,
            SOURCE_HARNESS_EVENTS if observed else SOURCE_UNOBSERVED,
            candidate.max_seq,
            candidate.event_count,
            now,
        ],
    )
    if malformed:
        log.warning(
            "usage rollup skipped %d malformed payload(s) in session %s",
            malformed,
            candidate.session_id,
        )
    return malformed


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
        with control_plane_connection(registry_path) as con:
            report = rollup_pending_sessions(con, limit=self.batch_size)
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
