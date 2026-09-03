"""Rolling harness-stream usage up to one row per session."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.db import control_plane_path
from drover.server.harness.usage_rollup import (
    SOURCE_HARNESS_EVENTS,
    SOURCE_UNOBSERVED,
    malformed_payload_count,
    reset_counters_for_tests,
    rolled_session_count,
    rollup_pending_sessions,
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
