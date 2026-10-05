"""Tests for src/drover/server/ingest.py."""

import json
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server import ingest as ingest_mod
from drover.server.ingest import IngestStats
from drover.server.ingest import ingest_file as record_file

FIXTURE = Path(__file__).parent / "fixtures" / "incoming" / "sample_agent_events.jsonl"


def ingest_file(path, **kwargs):
    """Integration helper: explicitly drain the asynchronous legacy sink."""
    from datetime import datetime, timedelta, timezone

    from drover.server.control_exporter import ControlOutboxExporter

    stats = record_file(path, **kwargs)
    exporter = ControlOutboxExporter(
        control_path=kwargs["duckdb_path"],
        analytical_path=kwargs["duckdb_path"],
        parquet_dir=kwargs["parquet_dir"],
        batch_size=1000,
    )
    while True:
        result = exporter.run_once(
            now=datetime.now(timezone.utc) + timedelta(seconds=10)
        )
        if not result["pending"]:
            break
    return stats


@pytest.fixture
def tmp_lh(tmp_path, pg_control_path):
    parquet_dir = tmp_path / "parquet"
    db_path = pg_control_path
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    return parquet_dir, db_path


def test_ingest_writes_three_rows(tmp_lh):
    parquet_dir, db_path = tmp_lh
    stats = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.read == 3
    assert stats.inserted == 3
    assert stats.skipped_dupes == 0

    con = duckdb.connect(str(db_path))
    n = con.execute(
        "SELECT count(*) FROM agent_events WHERE id LIKE 'evt-%'"
    ).fetchone()[0]
    assert n == 3


def test_ingest_partitions_by_date_and_agent(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    macmini = (
        parquet_dir / "agent_events" / "date=2026-05-08" / "agent_id=macmini-claude"
    )
    nas = parquet_dir / "agent_events" / "date=2026-05-08" / "agent_id=nas-openclaw"
    assert macmini.is_dir()
    assert nas.is_dir()
    assert any(p.suffix == ".parquet" for p in macmini.iterdir())
    assert any(p.suffix == ".parquet" for p in nas.iterdir())


def test_ingest_is_idempotent(tmp_lh):
    parquet_dir, db_path = tmp_lh
    s1 = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    s2 = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert s1.inserted == 3
    assert s2.inserted == 0
    assert s2.skipped_dupes == 3

    con = duckdb.connect(str(db_path))
    n = con.execute(
        "SELECT count(*) FROM agent_events WHERE id LIKE 'evt-%'"
    ).fetchone()[0]
    assert n == 3


def test_ingest_computes_task_id(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    rows = con.execute(
        "SELECT DISTINCT task_id FROM agent_events WHERE id LIKE 'evt-%'"
    ).fetchall()
    # All three events are arniesaha/nexus@main → same task_id
    assert len(rows) == 1
    task_id = rows[0][0]
    assert isinstance(task_id, str)
    assert len(task_id) == 16


def test_ingest_upserts_tasks_row(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    rows = con.execute("SELECT repo_owner, repo_name, branch FROM tasks").fetchall()
    assert ("arniesaha", "nexus", "main") in rows


def test_ingest_skips_malformed_lines(tmp_lh, tmp_path):
    parquet_dir, db_path = tmp_lh
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id":"good","session_id":"s","timestamp":"2026-05-08T10:00:00Z",'
        '"agent_id":"a","event_type":"user_message","raw_data":{}}\n'
        "NOT JSON\n"
        '{"missing":"required-fields"}\n'
    )
    stats = ingest_file(bad, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.read == 3
    assert stats.inserted == 1
    assert stats.errors == 2


def test_ingest_returns_new_session_ids(tmp_lh):
    """ingest_file() must populate stats.new_session_ids for every newly-seen session."""
    parquet_dir, db_path = tmp_lh
    # sample_agent_events.jsonl has sess-A (2 events) and sess-B (1 event)
    stats = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.new_session_ids == {"sess-A", "sess-B"}


def test_ingest_persists_normalized_native_usage_columns(tmp_lh, tmp_path):
    """Imported Claude usage must be queryable without decoding raw JSON."""
    parquet_dir, db_path = tmp_lh
    incoming = tmp_path / "claude_usage.jsonl"
    incoming.write_text(
        json.dumps(
            {
                "id": "claude-usage-1",
                "session_id": "claude-native-session",
                "timestamp": "2026-09-04T12:00:00Z",
                "agent_id": "claude-code",
                "event_type": "assistant_message",
                "message": {"role": "assistant", "content": "Done."},
                "token_usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 10,
                    "reasoning_output_tokens": 5,
                },
                "raw_data": {},
            }
        )
        + "\n"
    )

    stats = ingest_file(incoming, parquet_dir=parquet_dir, duckdb_path=db_path)

    assert stats.inserted == 1
    with duckdb.connect(str(db_path)) as con:
        row = con.execute("""
            SELECT input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, reasoning_tokens
            FROM agent_events
            WHERE id = 'claude-usage-1'
            """).fetchone()
    assert row == (120, 30, 80, 10, 5)


def test_ingest_new_session_ids_empty_on_reingest(tmp_lh):
    """Re-ingesting the same file must produce no new session ids (all dupes)."""
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    stats2 = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats2.new_session_ids == set()


def test_ingest_propagates_unique_session_repo_to_unattributed_events(
    tmp_lh, tmp_path, monkeypatch
):
    """Observer/hook events in a session should inherit the session's only repo."""
    monkeypatch.setenv(
        "DROVER_REPO_ROOTS_JSON",
        '{"/Users/arnabmac/jenny/nexus": "arniesaha/nexus"}',
    )
    parquet_dir, db_path = tmp_lh
    incoming = tmp_path / "session_attribution.jsonl"
    rows = [
        {
            "id": "hook-before-cwd",
            "session_id": "sess-prop",
            "timestamp": "2026-05-27T01:00:00Z",
            "agent_id": "macmini-claude",
            "event_type": "attachment",
            "raw_data": {"type": "attachment"},
        },
        {
            "id": "project-user",
            "session_id": "sess-prop",
            "timestamp": "2026-05-27T01:01:00Z",
            "agent_id": "macmini-claude",
            "event_type": "user",
            "message": {"role": "user", "content": "work on nexus"},
            "raw_data": {"cwd": "/Users/arnabmac/jenny/nexus", "type": "user"},
        },
    ]
    incoming.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    stats = ingest_file(incoming, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.inserted == 2

    con = duckdb.connect(str(db_path))
    try:
        repo_rows = con.execute("""
            SELECT id, repo_owner, repo_name, task_id
            FROM agent_events
            WHERE session_id='sess-prop'
            ORDER BY id
            """).fetchall()
    finally:
        con.close()

    assert repo_rows[0][:3] == ("hook-before-cwd", "arniesaha", "nexus")
    assert repo_rows[1][:3] == ("project-user", "arniesaha", "nexus")
    assert repo_rows[0][3] == repo_rows[1][3]


def test_ingest_dedup_is_transactional_without_analytical_lookup(tmp_lh, monkeypatch):
    from drover.server import db
    from drover.server.db import control_plane_connection

    parquet_dir, db_path = tmp_lh

    def forbidden(*args, **kwargs):
        raise AssertionError("ingest must not open analytical storage")

    monkeypatch.setattr(db, "open_duckdb_connection", forbidden)
    first = record_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    second = record_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert first.inserted == 3 and second.skipped_dupes == 3
    with control_plane_connection(db_path) as con:
        assert con.execute("SELECT count(*) FROM control_outbox_events").fetchone() == (
            3,
        )
    assert not [p for p in parquet_dir.rglob("*.parquet") if "_seed" not in str(p)]


def test_ingest_rollup_is_limited_to_touched_tasks(tmp_lh, monkeypatch):
    parquet_dir, db_path = tmp_lh
    calls = []

    def fake_rollup(con, task_ids=None, dates=None):
        calls.append((task_ids, dates))
        return 0

    from drover.server import rollup

    monkeypatch.setattr(rollup, "rollup_tasks", fake_rollup)

    stats = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    assert stats.inserted == 3
    assert len(calls) == 1
    task_ids, dates = calls[0]
    assert task_ids
    assert all(isinstance(task_id, str) for task_id in task_ids)
    assert dates == ["2026-05-08"]
