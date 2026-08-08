"""Tests for src/drover/server/ingest.py."""

from pathlib import Path
import json
import duckdb
import pytest
from drover.schema import bootstrap
from drover.server import ingest as ingest_mod
from drover.server.ingest import ingest_file, IngestStats

FIXTURE = Path(__file__).parent / "fixtures" / "incoming" / "sample_agent_events.jsonl"


@pytest.fixture
def tmp_lh(tmp_path):
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
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


def test_ingest_dedup_lookup_uses_bounded_date_macro(tmp_lh, monkeypatch):
    """Dedup should scan only incoming date partitions, not all historical agent_events."""
    parquet_dir, db_path = tmp_lh
    captured = []
    real_connect = duckdb.connect

    class _TrackingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if "dedup_key" in sql and "agent_events" in sql:
                captured.append(sql)
            if params is None:
                return self._inner.execute(sql)
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def tracking_connect(*args, **kwargs):
        return _TrackingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(ingest_mod.duckdb, "connect", tracking_connect)

    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    assert captured
    assert "agent_events_for_date" in captured[0]
    assert "FROM agent_events WHERE dedup_key IS NOT NULL" not in captured[0]


def test_ingest_rollup_is_limited_to_touched_tasks(tmp_lh, monkeypatch):
    parquet_dir, db_path = tmp_lh
    calls = []

    def fake_rollup(con, task_ids=None, dates=None):
        calls.append((task_ids, dates))
        return 0

    monkeypatch.setattr(ingest_mod, "rollup_tasks", fake_rollup)

    stats = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)

    assert stats.inserted == 3
    assert len(calls) == 1
    task_ids, dates = calls[0]
    assert task_ids
    assert all(isinstance(task_id, str) for task_id in task_ids)
    assert dates == ["2026-05-08"]
