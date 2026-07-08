"""Tests for the rolling active-session handoff brief.

Covers the three relevant paths through ``generate_active_brief``:
  - cached row inside the TTL → backend NOT called
  - cached row outside the TTL → backend called, row refreshed
  - no cached row → backend called, row inserted
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import bootstrap
from drover.server.briefs.active import generate_active_brief


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _write_events(parquet_dir: Path, *, session_id: str) -> None:
    now = datetime.now(timezone.utc)
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("session_id", pa.string()),
            ("agent_id", pa.string()),
            ("task_id", pa.string()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),
            ("role", pa.string()),
            ("content", pa.string()),
            ("repo_owner", pa.string()),
            ("repo_name", pa.string()),
            ("branch", pa.string()),
            ("principal_id", pa.string()),
            ("dedup_key", pa.string()),
            ("raw_data", pa.string()),
        ]
    )
    rows = [
        (
            f"{session_id}-1",
            session_id,
            "macmini",
            "task-X",
            now,
            "user_message",
            "user",
            "please refactor foo.py",
            "arnab",
            "nexus",
            "main",
            "arnab",
            "k1",
            "{}",
        ),
        (
            f"{session_id}-2",
            session_id,
            "macmini",
            "task-X",
            now,
            "assistant_message",
            "assistant",
            "OK, working on foo.py now.",
            "arnab",
            "nexus",
            "main",
            "arnab",
            "k2",
            "{}",
        ),
    ]
    table = pa.table(
        {
            f.name: pa.array([r[i] for r in rows], type=f.type)
            for i, f in enumerate(schema)
        },
        schema=schema,
    )
    out = (
        parquet_dir
        / "agent_events"
        / f"date={now.date().isoformat()}"
        / "agent_id=macmini"
    )
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


class _StubBackend:
    name = "stub"
    model = "stub-active-v1"

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, prompt: str) -> dict:
        self.calls += 1
        return {
            "brief_md": "Agent is refactoring foo.py per user's request.",
            "last_user_req": "please refactor foo.py",
            "current_objective": "Refactor src/foo.py to extract helpers.",
            "files_touched": ["src/foo.py"],
            "open_blockers": "",
            "suggested_next": "Run pytest after the refactor lands.",
        }


def _seed_cached_row(
    duckdb_path: Path,
    *,
    session_id: str,
    age_sql: str,
) -> None:
    """Insert a row whose freshness_ts is set via SQL (so we can age it)."""
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            f"""INSERT INTO active_session_briefs
                (session_id, brief_md, last_user_req, current_objective,
                 files_touched, open_blockers, suggested_next,
                 events_seen, freshness_ts, generator_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, {age_sql}, ?)""",
            [
                session_id,
                "cached brief",
                "cached request",
                "cached objective",
                ["cached/file.py"],
                "",
                "cached next",
                5,
                "cached-model",
            ],
        )
    finally:
        con.close()


def test_cached_row_inside_ttl_skips_backend(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, session_id="S-FRESH")
    _seed_cached_row(duckdb_path, session_id="S-FRESH", age_sql="now()")

    backend = _StubBackend()
    out = generate_active_brief(
        duckdb_path, "S-FRESH", backend=backend, max_age_seconds=60
    )
    assert backend.calls == 0
    assert out["brief_md"] == "cached brief"
    assert out["generator_model"] == "cached-model"
    assert out["files_touched"] == ["cached/file.py"]


def test_cached_row_outside_ttl_refreshes(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, session_id="S-STALE")
    _seed_cached_row(
        duckdb_path,
        session_id="S-STALE",
        age_sql="now() - INTERVAL 1 HOUR",
    )

    backend = _StubBackend()
    out = generate_active_brief(
        duckdb_path, "S-STALE", backend=backend, max_age_seconds=60
    )
    assert backend.calls == 1
    assert out["brief_md"].startswith("Agent is refactoring")
    assert out["generator_model"] == "stub-active-v1"
    assert out["files_touched"] == ["src/foo.py"]

    # The row should now be marked with the new model.
    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT generator_model FROM active_session_briefs WHERE session_id='S-STALE'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "stub-active-v1"


def test_no_cached_row_inserts(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, session_id="S-NEW")

    backend = _StubBackend()
    out = generate_active_brief(
        duckdb_path, "S-NEW", backend=backend, max_age_seconds=60
    )
    assert backend.calls == 1
    assert out["brief_md"].startswith("Agent is refactoring")
    assert out["last_user_req"] == "please refactor foo.py"
    assert out["current_objective"].startswith("Refactor src/foo.py")
    assert out["suggested_next"].startswith("Run pytest")
    assert out["events_seen"] == 2

    con = duckdb.connect(str(duckdb_path))
    try:
        row = con.execute(
            "SELECT brief_md, events_seen FROM active_session_briefs WHERE session_id='S-NEW'"
        ).fetchone()
    finally:
        con.close()
    assert row[0].startswith("Agent is refactoring")
    assert row[1] == 2


def test_no_events_raises(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    backend = _StubBackend()
    try:
        generate_active_brief(
            duckdb_path, "S-MISSING", backend=backend, max_age_seconds=60
        )
    except RuntimeError as e:
        assert "no events" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
    assert backend.calls == 0
