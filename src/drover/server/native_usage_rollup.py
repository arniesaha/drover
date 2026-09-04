"""Roll typed native-history usage into the source-aware session projection."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from drover.event_identity import canonical_agent_events_cte
from drover.server.db import control_plane_connection, open_duckdb_connection
from drover.server.harness.usage import TokenTotals
from drover.server.harness.usage_sources import (
    SOURCE_NATIVE_AGENT_EVENTS,
    upsert_source_usage,
)

log = logging.getLogger("drover.native_usage_rollup")

DEFAULT_PARTITION_LIMIT = 1


@dataclass(frozen=True)
class NativeUsageRollupReport:
    partitions: int
    sessions: int


@dataclass(frozen=True)
class _PartitionTotals:
    session_id: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    turn_count: int
    event_count: int


def _pending_partitions(duckdb_path: Path, *, limit: int) -> list[tuple[str, datetime]]:
    with open_duckdb_connection(duckdb_path, role="worker") as con:
        activity = con.execute("""
            SELECT date, latest_ingested_at
            FROM agent_event_partition_activity
            ORDER BY latest_ingested_at, date
            """).fetchall()
    with control_plane_connection(duckdb_path) as con:
        watermarks = {str(row[0]): row[1] for row in con.execute("""
                SELECT partition_date, source_activity_at
                FROM native_usage_partition_watermarks
                """).fetchall()}
    return [
        (str(date), observed_at)
        for date, observed_at in activity
        if watermarks.get(str(date)) is None or watermarks[str(date)] < observed_at
    ][:limit]


def _load_partition_totals(
    con: duckdb.DuckDBPyConnection, partition_date: str
) -> list[_PartitionTotals]:
    rows = con.execute(
        f"""
        WITH date_agent_events AS (
          SELECT session_id, dedup_key, timestamp, id, repo_owner, repo_name,
                 input_tokens, output_tokens, cache_read_tokens,
                 cache_write_tokens, reasoning_tokens
          FROM agent_events_for_date(?)
          WHERE input_tokens IS NOT NULL
             OR output_tokens IS NOT NULL
             OR cache_read_tokens IS NOT NULL
             OR cache_write_tokens IS NOT NULL
             OR reasoning_tokens IS NOT NULL
        ),
        {canonical_agent_events_cte(source="date_agent_events")}
        SELECT session_id,
               sum(input_tokens)::BIGINT,
               sum(output_tokens)::BIGINT,
               sum(cache_read_tokens)::BIGINT,
               sum(cache_write_tokens)::BIGINT,
               sum(reasoning_tokens)::BIGINT,
               count(*)::INTEGER AS turn_count,
               count(*)::INTEGER AS event_count
        FROM canonical_agent_events
        WHERE session_id IS NOT NULL
        GROUP BY session_id
        ORDER BY session_id
        """,
        [partition_date],
    ).fetchall()
    return [
        _PartitionTotals(
            session_id=str(row[0]),
            input_tokens=row[1],
            output_tokens=row[2],
            cache_read_tokens=row[3],
            cache_write_tokens=row[4],
            reasoning_tokens=row[5],
            turn_count=int(row[6]),
            event_count=int(row[7]),
        )
        for row in rows
    ]


def _rebuild_partition(
    con: duckdb.DuckDBPyConnection,
    *,
    partition_date: str,
    source_activity_at: datetime,
    totals: list[_PartitionTotals],
) -> int:
    prior_sessions = {
        str(row[0])
        for row in con.execute(
            """
            SELECT session_id FROM native_usage_partition_totals
            WHERE partition_date = ?
            """,
            [partition_date],
        ).fetchall()
    }
    con.execute(
        "DELETE FROM native_usage_partition_totals WHERE partition_date = ?",
        [partition_date],
    )
    rolled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for total in totals:
        con.execute(
            """
            INSERT INTO native_usage_partition_totals
              (native_usage_partition_id, session_id, partition_date,
               input_tokens, output_tokens, cache_read_tokens,
               cache_write_tokens, reasoning_tokens, turn_count, event_count,
               exact, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?)
            """,
            [
                f"{SOURCE_NATIVE_AGENT_EVENTS}:{partition_date}:{total.session_id}",
                total.session_id,
                partition_date,
                total.input_tokens,
                total.output_tokens,
                total.cache_read_tokens,
                total.cache_write_tokens,
                total.reasoning_tokens,
                total.turn_count,
                total.event_count,
                rolled_at,
            ],
        )
    con.execute(
        """
        INSERT INTO native_usage_partition_watermarks
          (partition_date, source_activity_at, rolled_at)
        VALUES (?, ?, ?)
        ON CONFLICT (partition_date) DO UPDATE SET
          source_activity_at = excluded.source_activity_at,
          rolled_at = excluded.rolled_at
        """,
        [partition_date, source_activity_at, rolled_at],
    )
    for session_id in prior_sessions | {total.session_id for total in totals}:
        row = con.execute(
            """
            SELECT sum(input_tokens)::BIGINT, sum(output_tokens)::BIGINT,
                   sum(cache_read_tokens)::BIGINT, sum(cache_write_tokens)::BIGINT,
                   sum(reasoning_tokens)::BIGINT, sum(turn_count)::INTEGER,
                   sum(event_count)::INTEGER, bool_and(exact)
            FROM native_usage_partition_totals
            WHERE session_id = ?
            """,
            [session_id],
        ).fetchone()
        if row is None or row[6] is None:
            continue
        upsert_source_usage(
            con,
            session_id=session_id,
            source=SOURCE_NATIVE_AGENT_EVENTS,
            usage=TokenTotals(
                input_tokens=row[0],
                output_tokens=row[1],
                cache_read_tokens=row[2],
                cache_write_tokens=row[3],
                reasoning_tokens=row[4],
                exact=bool(row[7]),
            ),
            turn_count=int(row[5]),
            exact=bool(row[7]),
            source_seq=int(row[6]),
            source_event_count=int(row[6]),
            observed_at=rolled_at,
        )
    return len(totals)


def rollup_pending_native_usage(
    duckdb_path: Path, *, limit: int = DEFAULT_PARTITION_LIMIT
) -> NativeUsageRollupReport:
    """Apply changed native-event partitions, one bounded partition at a time."""
    duckdb_path = Path(duckdb_path)
    pending = _pending_partitions(duckdb_path, limit=limit)
    if not pending:
        return NativeUsageRollupReport(partitions=0, sessions=0)
    with open_duckdb_connection(duckdb_path, role="worker") as analytical:
        loaded = [
            (
                partition_date,
                activity_at,
                _load_partition_totals(analytical, partition_date),
            )
            for partition_date, activity_at in pending
        ]
    sessions = 0
    with control_plane_connection(duckdb_path) as control:
        for partition_date, activity_at, totals in loaded:
            # The partition totals, source projection, and watermark are one
            # unit. Advancing the watermark first would turn an interrupted
            # materialization into a permanently missed partition.
            control.execute("BEGIN TRANSACTION")
            try:
                sessions += _rebuild_partition(
                    control,
                    partition_date=partition_date,
                    source_activity_at=activity_at,
                    totals=totals,
                )
            except BaseException:
                control.execute("ROLLBACK")
                raise
            else:
                control.execute("COMMIT")
    return NativeUsageRollupReport(partitions=len(loaded), sessions=sessions)


class NativeUsageRollupWorker:
    """Periodically repair native usage after an import or an interrupted write."""

    def __init__(self, *, duckdb_path: Path, poll_interval_s: float = 60.0) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="drover-native-usage-rollup", daemon=True
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
            except Exception:  # noqa: BLE001 - a later pass can repair an import.
                log.exception("native usage rollup pass crashed")
            self._stop.wait(self.poll_interval_s)

    def drain_once(self) -> NativeUsageRollupReport:
        return rollup_pending_native_usage(self.duckdb_path)
