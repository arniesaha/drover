"""`drover-server memory requeue`: regenerate derived memory after the rebuild (#480)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from drover.server.db import control_plane_connection
from drover.server.ledger import EMBED_SESSION, SUMMARIZE_SESSION, JobLedger
from drover.server.memory_requeue import (
    REQUEUE_PRIORITY,
    canonical_sessions,
    requeue_memory,
)
from drover.server.memory_store import MemoryRepository, SessionSummary
from drover.server.summarizer.jobs import source_version_for_session


def _seed(path: Path) -> None:
    sep30 = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    aug1 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    with control_plane_connection(path) as con:
        for sid, turns, ts in [
            ("real", [("user", "fix the bug"), ("assistant", "fixed")], sep30),
            ("status-only", [("system", "session started")], sep30),
            ("summarized", [("user", "hi"), ("assistant", "hello")], sep30),
            ("unknown_session", [("user", "x")], sep30),
            ("old", [("user", "long ago"), ("assistant", "yes")], aug1),
        ]:
            con.execute(
                """INSERT INTO harness_sessions
                (session_id, host_id, harness, command, status, native_session_id, summary_session_id)
                VALUES (?, 'host', 'codex', 'codex', 'ended', ?, ?)""",
                [sid, "native-" + sid, "summary-" + sid],
            )
            for n, (role, text) in enumerate(turns):
                kind = {"user": "user_input", "assistant": "assistant_output"}.get(
                    role, "status"
                )
                con.execute(
                    """INSERT INTO harness_events
                    (event_id, session_id, event_type, normalized_type, payload_json, created_at, content_preview)
                    VALUES (?, ?, ?, ?, ?, ?, 'preview is not message content')""",
                    [f"{sid}-{n}", sid, kind, kind, json.dumps({"text": text}), ts],
                )


def _put_current_summary(path: Path, session_id: str) -> None:
    version = next(
        s.source_version
        for s in canonical_sessions(
            path, since=date(2026, 7, 1), session_ids=[session_id]
        )
    )
    with control_plane_connection(path) as pg:
        MemoryRepository.put_summary(
            pg,
            SessionSummary(
                session_id=session_id, summary_md="s", source_version=version
            ),
        )


def _jobs(path: Path) -> dict[tuple[str, str], int]:
    with control_plane_connection(path) as con:
        rows = con.execute(
            "SELECT job_kind, subject_key, priority FROM pipeline_jobs WHERE status = 'pending'"
        ).fetchall()
    return {(kind, subject): priority for kind, subject, priority in rows}


def test_requeue_enqueues_summaries_and_embed_only_jobs(pg_control_path):
    _seed(pg_control_path)
    _put_current_summary(pg_control_path, "summarized")

    report = requeue_memory(
        pg_control_path,
        since=date(2026, 9, 1),
        embedding_model="nomic-embed-text",
        sleep=lambda s: None,
    )
    assert (
        report.sessions_scanned == 3
    )  # old is outside the window; placeholder skipped
    assert _jobs(pg_control_path) == {
        (SUMMARIZE_SESSION, "real"): REQUEUE_PRIORITY,
        (SUMMARIZE_SESSION, "status-only"): REQUEUE_PRIORITY,
        # Its summary is current but it has no vector: embed only.
        (EMBED_SESSION, "summarized"): REQUEUE_PRIORITY,
    }
    assert report.summarize == {"queued": 2} and report.embed == {"queued": 1}

    again = requeue_memory(
        pg_control_path,
        since=date(2026, 9, 1),
        embedding_model="nomic-embed-text",
        sleep=lambda s: None,
    )
    assert again.summarize == {"already_queued": 2}


def test_substantive_only_dry_run_changes_nothing(pg_control_path):
    _seed(pg_control_path)
    report = requeue_memory(
        pg_control_path,
        since=date(2026, 7, 1),
        substantive_only=True,
        dry_run=True,
        sleep=lambda s: None,
    )
    assert report.skipped_not_substantive == 1  # status-only
    assert report.summarize == {"would_enqueue": 3}  # real, summarized, old
    assert _jobs(pg_control_path) == {}


def test_targeted_sessions_force_a_fresh_generation(pg_control_path):
    _seed(pg_control_path)
    _put_current_summary(pg_control_path, "summarized")
    ledger = JobLedger(pg_control_path)
    report = requeue_memory(
        pg_control_path,
        since=date(2026, 9, 1),
        session_ids=["summarized"],
        sleep=lambda s: None,
    )
    assert report.summarize == {"queued": 1}
    assert ledger.latest(SUMMARIZE_SESSION, "summarized").status == "pending"


def test_requeue_is_rate_limited(pg_control_path):
    _seed(pg_control_path)
    sleeps: list[float] = []
    requeue_memory(
        pg_control_path,
        since=date(2026, 9, 1),
        rate_per_second=4.0,
        sleep=sleeps.append,
    )
    assert sleeps == [0.25, 0.25, 0.25]


def test_cli_requires_postgres(tmp_path):
    from drover.server.__main__ import main

    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\nduckdb_path = "{tmp_path / "drover.duckdb"}"\n'
        f'parquet_dir = "{tmp_path / "parquet"}"\n'
        f'incoming_dir = "{tmp_path / "incoming"}"\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main, ["--config", str(config), "memory", "requeue", "--since", "2026-09-01"]
    )
    assert result.exit_code != 0
    assert "postgres" in result.output


def test_pg_requeue_never_opens_analytical_store(pg_control_path, monkeypatch):
    _seed(pg_control_path)
    assert not pg_control_path.exists()
    from drover.server import db

    def forbidden(*args, **kwargs):
        pytest.fail("requeue attempted to open analytical DuckDB")

    monkeypatch.setattr(db, "open_duckdb_connection", forbidden)
    for alias in ("real", "native-real", "summary-real"):
        report = requeue_memory(
            pg_control_path,
            since=date(2026, 9, 1),
            session_ids=[alias],
            substantive_only=True,
            dry_run=True,
        )
        assert report.summarize == {"would_enqueue": 1}
        assert report.as_dict()["counts_by_reason"]["summarize"] == 1
        assert report.since == "2026-09-01"
        assert report.through
    assert _jobs(pg_control_path) == {}


def test_substantive_uses_full_envelope_and_session_history(pg_control_path):
    _seed(pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        con.execute(
            "UPDATE harness_events SET payload_json=? WHERE event_id='real-1'",
            [json.dumps({"text": "  "})],
        )
        # A user turn before the window still belongs to this active session.
        con.execute(
            "UPDATE harness_events SET created_at=? WHERE event_id='summarized-0'",
            [datetime(2026, 8, 1, tzinfo=timezone.utc)],
        )
    report = requeue_memory(
        pg_control_path, since=date(2026, 9, 1), substantive_only=True, dry_run=True
    )
    assert report.skipped_not_substantive == 2
    assert report.summarize == {"would_enqueue": 1}
    _put_current_summary(pg_control_path, "summarized")
    current = requeue_memory(
        pg_control_path, since=date(2026, 9, 1), substantive_only=True, dry_run=True
    )
    assert current.as_dict()["counts_by_reason"] == {
        "summarize": 0,
        "embed_only": 0,
        "skipped_non_substantive": 2,
        "already_current": 1,
    }


def test_pg_source_version_matches_analytical_projection(pg_control_path):
    from drover.server.memory_identity import (
        project_control_event,
        read_memory_sessions,
    )
    from drover.server.memory_requeue import _substantive
    from drover.server.summarizer.derive import SUBSTANTIVE_SQL

    _seed(pg_control_path)
    with control_plane_connection(pg_control_path) as pg:
        identities = read_memory_sessions(pg)
        cur = pg.execute("SELECT * FROM harness_events")
        cols = [d[0] for d in cur.description]
        rows = [
            project_control_event(dict(zip(cols, row)), identities[row[1]])
            for row in cur.fetchall()
        ]
    with duckdb.connect(":memory:") as con:
        from drover.server.memory_identity import ensure_memory_schema

        ensure_memory_schema(con)
        con.execute("CREATE VIEW agent_events AS SELECT * FROM control_memory_events")
        for row in rows:
            con.execute(
                "INSERT INTO control_memory_events ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                list(row.values()),
            )
        for session in canonical_sessions(pg_control_path, since=date(2026, 7, 1)):
            assert session.source_version == source_version_for_session(
                con, session.session_id
            )
        for row in rows:
            expected = con.execute(
                f"SELECT {SUBSTANTIVE_SQL} FROM agent_events WHERE id=?", [row["id"]]
            ).fetchone()[0]
            assert _substantive(row) == bool(expected)


def test_analytical_lock_conflict_is_immediate(tmp_path, monkeypatch):
    from drover.server import db

    calls = []

    def locked(*args, **kwargs):
        calls.append(args)
        raise duckdb.IOException(
            "Could not set lock on file: Conflicting lock is held in hub (PID 123)"
        )

    monkeypatch.setattr(db.duckdb, "connect", locked)
    with pytest.raises(db.AnalyticalStoreLocked, match="even read-only"):
        db.open_duckdb_connection(tmp_path / "drover.duckdb")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_cli_closes_pool_on_success_and_failure(monkeypatch, failure):
    from drover.server import __main__ as cli

    closed = []
    monkeypatch.setattr(
        cli, "close_all_postgres_control_stores", lambda: closed.append(True)
    )
    monkeypatch.setattr(
        cli,
        "_resolve_config",
        lambda _: type("Config", (), {"duckdb_path": Path("unused")})(),
    )

    def require(_):
        if failure:
            raise duckdb.IOException(
                "Could not set lock on file: Conflicting lock held by hub"
            )

    monkeypatch.setattr(cli, "_require_memory_store", require)
    monkeypatch.setattr(cli, "_configured_embedding_model", lambda _: None)
    monkeypatch.setattr(
        cli,
        "requeue_memory",
        lambda *a, **kw: type("Report", (), {"as_dict": lambda _: {}})(),
    )
    result = CliRunner().invoke(
        cli.main, ["memory", "requeue", "--since", "2026-09-01", "--dry-run"]
    )
    assert closed == [True]
    assert result.exit_code == (1 if failure else 0)
    if failure:
        assert "locked by another process" in result.output
        assert "even read-only" in result.output


def test_read_only_open_fails_fast_under_real_process_lock(tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "analytical.duckdb"
    # The writer owns a disposable database in this process; only the reader
    # runs separately, so there is no hub/database contact or orphaned writer.
    with duckdb.connect(str(path)):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
from pathlib import Path
from drover.server.db import AnalyticalStoreLocked, open_duckdb_connection
try:
    open_duckdb_connection(Path(sys.argv[1]), read_only=True, role="diagnostic")
except AnalyticalStoreLocked as exc:
    print(exc)
    sys.exit(2)
""",
                str(path),
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 2, result.stderr
    assert "locked by another process" in result.stdout
    assert "even read-only" in result.stdout


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("tool_action", {"tool": {"name": "read", "input": {}}}),
        ("file_change", {"path": "src/a.py"}),
        ("command", {"command": "true"}),
        ("status", {"item": {"type": "file_change", "changes": [{"path": "a.py"}]}}),
        ("assistant_output", {"text": "\t"}),
        ("assistant_output", {"text": "", "tool_use_blocks": [{"name": "read"}]}),
    ],
)
def test_pg_version_for_tool_and_blank_events(pg_control_path, kind, payload):
    from drover.server.memory_identity import (
        ensure_memory_schema,
        project_control_event,
        read_memory_sessions,
    )

    _seed(pg_control_path)
    with control_plane_connection(pg_control_path) as pg:
        pg.execute(
            "UPDATE harness_events SET normalized_type=?, payload_json=? WHERE event_id='real-1'",
            [kind, json.dumps(payload)],
        )
        identities = read_memory_sessions(pg)
        cur = pg.execute("SELECT * FROM harness_events WHERE session_id='real'")
        cols = [d[0] for d in cur.description]
        projected = [
            project_control_event(dict(zip(cols, row)), identities["real"])
            for row in cur.fetchall()
        ]
    session = canonical_sessions(
        pg_control_path, since=date(2026, 9, 1), session_ids=["real"]
    )[0]
    with duckdb.connect(":memory:") as con:
        ensure_memory_schema(con)
        con.execute("CREATE VIEW agent_events AS SELECT * FROM control_memory_events")
        for row in projected:
            con.execute(
                "INSERT INTO control_memory_events ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                list(row.values()),
            )
        assert session.source_version == source_version_for_session(con, "real")
