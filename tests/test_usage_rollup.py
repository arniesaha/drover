"""Rolling harness-stream usage up to one row per session."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server import harness as _harness_pkg  # noqa: F401
from drover.server.db import control_plane_path
from drover.server.harness import usage_rollup as usage_rollup_module
from drover.server.harness.usage import TokenTotals
from drover.server.harness.usage_rollup import (
    SOURCE_HARNESS_EVENTS,
    SOURCE_UNOBSERVED,
    malformed_payload_count,
    reset_counters_for_tests,
    rolled_session_count,
    rollup_pending_sessions,
)
from drover.server.harness.usage_sources import (
    SOURCE_NATIVE_AGENT_EVENTS,
    upsert_source_usage,
)


@pytest.fixture(autouse=True)
def _fresh_counters():
    reset_counters_for_tests()
    yield
    reset_counters_for_tests()


def registry(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    return duckdb.connect(str(control_plane_path(db)))


def add_session(con, session_id: str, harness: str, host_id: str = "mac-mini") -> None:
    con.execute(
        """INSERT INTO harness_sessions
           (session_id, host_id, harness, command, status, started_at)
           VALUES (?, ?, ?, 'cmd', 'running', now())""",
        [session_id, host_id, harness],
    )


def add_event(
    con, session_id: str, seq: int | None, payload, event_type="assistant_output"
) -> None:
    payload_json = payload if isinstance(payload, str) else json.dumps(payload)
    con.execute(
        """INSERT INTO harness_events
           (event_id, session_id, event_type, payload_json, seq)
           VALUES (?, ?, ?, ?, ?)""",
        [
            f"{session_id}-{seq}-{abs(hash(payload_json))}",
            session_id,
            event_type,
            payload_json,
            seq,
        ],
    )


def claude_usage(
    native_event_id: str,
    *,
    inp: int,
    out: int,
    cache_read: int = 0,
    cache_write: int = 0,
):
    return {
        "native_event_id": native_event_id,
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }


def codex_usage(*, inp: int, out: int, cached: int = 0):
    return {
        "turn_complete": True,
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cached_input_tokens": cached,
            "reasoning_output_tokens": 0,
        },
    }


def usage_row(con, session_id: str):
    return con.execute(
        """SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                  turn_count, exact, source, source_seq, source_event_count, harness, host_id
           FROM session_usage WHERE session_id = ?""",
        [session_id],
    ).fetchone()


def test_claude_sessions_sum_per_message_deltas_once(tmp_path):
    con = registry(tmp_path)
    add_session(con, "c1", "claude-code")
    add_event(con, "c1", 1, claude_usage("m1", inp=100, out=10, cache_read=40))
    add_event(
        con, "c1", 2, claude_usage("m1", inp=100, out=10, cache_read=40)
    )  # duplicate delivery
    add_event(con, "c1", 3, claude_usage("m2", inp=50, out=5, cache_write=7))
    add_event(con, "c1", 4, {"text": "no usage here"}, event_type="user_input")

    report = rollup_pending_sessions(con)

    assert (report.candidates, report.rolled, report.malformed_events) == (1, 1, 0)
    assert usage_row(con, "c1") == (
        150,
        15,
        40,
        7,
        2,
        True,
        SOURCE_HARNESS_EVENTS,
        4,
        4,
        "claude-code",
        "mac-mini",
    )
    assert rolled_session_count() == 1


def test_codex_sessions_take_the_last_running_total(tmp_path):
    con = registry(tmp_path)
    add_session(con, "x1", "codex")
    add_event(con, "x1", 1, codex_usage(inp=1000, out=20, cached=300))
    add_event(con, "x1", 2, codex_usage(inp=1800, out=45, cached=900))

    rollup_pending_sessions(con)

    row = usage_row(con, "x1")
    assert row[:4] == (1800, 45, 900, None)
    assert row[4] == 2  # two usage-bearing turns


def test_unreporting_harnesses_get_an_unobserved_row_not_zeros(tmp_path):
    con = registry(tmp_path)
    add_session(con, "a1", "agy")
    add_event(con, "a1", 1, {"text": "hello"}, event_type="user_input")

    rollup_pending_sessions(con)

    row = usage_row(con, "a1")
    assert row[:4] == (None, None, None, None)
    assert row[6] == SOURCE_UNOBSERVED
    assert row[7:9] == (1, 1)
    # Second pass: nothing changed, so nothing is rescanned.
    assert rollup_pending_sessions(con).candidates == 0


def test_source_ledger_preserves_native_usage_until_harness_observes_it(tmp_path):
    """A no-usage harness update must not erase another source's measured usage."""
    con = registry(tmp_path)
    upsert_source_usage(
        con,
        session_id="shared-session",
        source=SOURCE_NATIVE_AGENT_EVENTS,
        usage=TokenTotals(input_tokens=10, output_tokens=2, cache_read_tokens=4),
        turn_count=1,
        exact=True,
        source_seq=2,
        source_event_count=2,
    )
    assert usage_row(con, "shared-session")[:9] == (
        10,
        2,
        4,
        None,
        1,
        True,
        SOURCE_NATIVE_AGENT_EVENTS,
        2,
        2,
    )

    upsert_source_usage(
        con,
        session_id="shared-session",
        source=SOURCE_HARNESS_EVENTS,
        usage=TokenTotals(),
        turn_count=0,
        exact=True,
        source_seq=3,
        source_event_count=3,
        host_id="mac-mini",
        harness="claude-code",
    )
    assert usage_row(con, "shared-session")[6:9] == (
        SOURCE_NATIVE_AGENT_EVENTS,
        2,
        2,
    )

    upsert_source_usage(
        con,
        session_id="shared-session",
        source=SOURCE_HARNESS_EVENTS,
        usage=TokenTotals(input_tokens=40, output_tokens=8, cache_read_tokens=16),
        turn_count=2,
        exact=True,
        source_seq=5,
        source_event_count=5,
        host_id="mac-mini",
        harness="claude-code",
    )
    assert usage_row(con, "shared-session")[:11] == (
        40,
        8,
        16,
        None,
        2,
        True,
        SOURCE_HARNESS_EVENTS,
        5,
        5,
        "claude-code",
        "mac-mini",
    )
    assert (
        con.execute("""
        SELECT source, input_tokens, usage_observed, source_seq, source_event_count
        FROM session_usage_sources
        WHERE session_id = 'shared-session'
        ORDER BY source
        """).fetchall()
        == [
            (SOURCE_HARNESS_EVENTS, 40, True, 5, 5),
            (SOURCE_NATIVE_AGENT_EVENTS, 10, True, 2, 2),
        ]
    )


def test_rollup_is_idempotent_and_reacts_only_to_new_events(tmp_path):
    con = registry(tmp_path)
    add_session(con, "c1", "claude-code")
    add_event(con, "c1", 1, claude_usage("m1", inp=10, out=1))
    rollup_pending_sessions(con)

    assert rollup_pending_sessions(con).candidates == 0
    add_event(con, "c1", 2, claude_usage("m2", inp=20, out=2))
    report = rollup_pending_sessions(con)

    assert report.candidates == 1
    assert usage_row(con, "c1")[:2] == (30, 3)
    assert usage_row(con, "c1")[7:9] == (2, 2)


def test_legacy_null_seq_rows_are_tracked_by_count(tmp_path):
    con = registry(tmp_path)
    add_session(con, "l1", "claude-code")
    add_event(con, "l1", None, claude_usage("m1", inp=5, out=1))
    rollup_pending_sessions(con)
    assert usage_row(con, "l1")[7:9] == (0, 1)

    add_event(con, "l1", None, claude_usage("m2", inp=5, out=1))
    assert rollup_pending_sessions(con).candidates == 1
    assert usage_row(con, "l1")[:2] == (10, 2)


def test_malformed_payloads_are_skipped_counted_and_mark_the_session_inexact(tmp_path):
    con = registry(tmp_path)
    add_session(con, "c1", "claude-code")
    add_event(con, "c1", 1, claude_usage("m1", inp=10, out=1))
    add_event(con, "c1", 2, "{not json")
    add_event(con, "c1", 3, "[1, 2, 3]")

    report = rollup_pending_sessions(con)

    assert report.malformed_events == 2
    assert malformed_payload_count() == 2
    row = usage_row(con, "c1")
    assert row[:2] == (10, 1)
    assert row[5] is False


def test_batch_limit_bounds_one_pass(tmp_path):
    con = registry(tmp_path)
    for index in range(3):
        add_session(con, f"s{index}", "claude-code")
        add_event(con, f"s{index}", 1, claude_usage("m", inp=1, out=1))

    first = rollup_pending_sessions(con, limit=2)
    second = rollup_pending_sessions(con, limit=2)

    assert (first.rolled, second.rolled) == (2, 1)


def test_worker_drains_through_its_own_control_plane_window(tmp_path):
    from drover.server.harness.usage_rollup import UsageRollupWorker

    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    with duckdb.connect(str(control_plane_path(db))) as con:
        add_session(con, "c1", "claude-code")
        add_event(con, "c1", 1, claude_usage("m1", inp=7, out=3))

    worker = UsageRollupWorker(duckdb_path=db, poll_interval_s=0.01)
    report = worker.drain_once()

    assert report.rolled == 1
    with duckdb.connect(str(control_plane_path(db))) as con:
        assert usage_row(con, "c1")[:2] == (7, 3)
    assert worker.last_pass_seconds is not None


def envelope(seq: int, inner: dict, event_type: str = "assistant") -> dict:
    return {
        "event_id": f"evt-{seq}",
        "type": event_type,
        "seq": seq,
        "session_id": "ignored-by-rollup",
        "role": "assistant",
        "text": "",
        "ts": "2026-09-02T00:00:00Z",
        "turn_id": f"turn-{seq}",
        "payload": inner,
    }


def test_registry_envelopes_are_read_as_stored(tmp_path):
    con = registry(tmp_path)
    add_session(con, "c1", "claude-code")
    add_event(
        con, "c1", 1, envelope(1, claude_usage("m1", inp=100, out=10, cache_read=40))
    )
    add_event(con, "c1", 2, envelope(2, claude_usage("m2", inp=50, out=5)))
    rollup_pending_sessions(con)
    row = usage_row(con, "c1")
    assert row[:3] == (150, 15, 40)
    assert row[6] == SOURCE_HARNESS_EVENTS


def test_codex_envelopes_take_the_last_running_total(tmp_path):
    con = registry(tmp_path)
    add_session(con, "x1", "codex")
    add_event(con, "x1", 1, envelope(1, codex_usage(inp=1000, out=20, cached=300)))
    add_event(con, "x1", 2, envelope(2, codex_usage(inp=1800, out=45, cached=900)))
    rollup_pending_sessions(con)
    row = usage_row(con, "x1")
    assert row[:3] == (1800, 45, 900)
    assert row[6] == SOURCE_HARNESS_EVENTS


def test_envelope_without_seq_dedups_on_column_seq(tmp_path):
    con = registry(tmp_path)
    add_session(con, "c2", "claude-code")
    inner_1 = claude_usage("m1", inp=100, out=10, cache_read=40)
    inner_2 = claude_usage("m1", inp=100, out=10, cache_read=40)  # duplicate id
    env_1 = envelope(1, inner_1)
    env_2 = envelope(2, inner_2)
    del env_1["seq"]
    del env_2["seq"]
    add_event(con, "c2", 1, env_1)
    add_event(con, "c2", 2, env_2)
    rollup_pending_sessions(con)
    row = usage_row(con, "c2")
    assert row[:3] == (100, 10, 40)
    assert row[6] == SOURCE_HARNESS_EVENTS


def test_worker_thread_starts_stops_and_survives_a_bad_pass(tmp_path, monkeypatch):
    from drover.server.harness import usage_rollup

    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    calls = []

    def explode(con, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    # The first call of a pass, so a failure here is a failed pass.
    monkeypatch.setattr(usage_rollup, "load_pending_candidates", explode)
    worker = usage_rollup.UsageRollupWorker(duckdb_path=db, poll_interval_s=0.01)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert len(calls) >= 2


def test_a_pass_does_not_hold_the_control_plane_across_its_parsing(tmp_path):
    """/harness needs this same lock.

    Holding it for the whole pass made the fleet endpoint wait for however
    long it took to parse every event of every candidate: 47s and 238s
    measured on the hub while a session was busy, which is precisely when
    someone is looking at it (#334).
    """
    import drover.server.db as dbmod

    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    with duckdb.connect(str(control_plane_path(db))) as con:
        add_session(con, "c1", "claude-code")
        for seq in range(1, 12):
            add_event(con, "c1", seq, claude_usage(f"m{seq}", inp=10, out=1))

    depth = 0
    peak = 0
    real = dbmod.control_plane_connection

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        nonlocal depth, peak
        with real(*args, **kwargs) as con:
            depth += 1
            peak = max(peak, depth)
            try:
                yield con
            finally:
                depth -= 1

    worker = usage_rollup_module.UsageRollupWorker(duckdb_path=db)
    with mock.patch.object(usage_rollup_module, "control_plane_connection", counting):
        report = worker.drain_once()

    assert report.rolled == 1
    # Three short windows: candidates, this session's rows, its upsert. Never
    # nested, and never spanning the parse between them.
    assert peak == 1
    with duckdb.connect(str(control_plane_path(db))) as con:
        assert usage_row(con, "c1")[:2] == (110, 11)
