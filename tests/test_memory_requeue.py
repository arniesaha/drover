"""`drover-server memory requeue`: regenerate derived memory after the rebuild (#480)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from drover.schema import bootstrap
from drover.server.db import control_plane_connection
from drover.server.ledger import EMBED_SESSION, SUMMARIZE_SESSION, JobLedger
from drover.server.memory_requeue import REQUEUE_PRIORITY, requeue_memory
from drover.server.memory_store import MemoryRepository, SessionSummary
from drover.server.summarizer.jobs import source_version_for_session

_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("session_id", pa.string()),
        ("agent_id", pa.string()),
        ("task_id", pa.string()),
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("event_type", pa.string()),
        ("role", pa.string()),
        ("content", pa.string()),
        ("dedup_key", pa.string()),
    ]
)


def _write(parquet_dir: Path, day: str, rows: list[tuple]) -> None:
    table = pa.table(
        {
            f.name: pa.array([r[i] for r in rows], type=f.type)
            for i, f in enumerate(_SCHEMA)
        },
        schema=_SCHEMA,
    )
    out = parquet_dir / "agent_events" / f"date={day}" / "agent_id=a"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{len(list(out.iterdir()))}.parquet")


def _event(sid: str, n: int, role: str, content: str, ts: datetime) -> tuple:
    return (f"{sid}-{n}", sid, "a", None, ts, "message", role, content, f"{sid}-{n}")


def _seed(pg_control_path: Path) -> Path:
    parquet_dir = pg_control_path.parent / "parquet"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=pg_control_path)
    sep30 = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    aug1 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    _write(
        parquet_dir,
        "2026-09-30",
        [
            _event("real", 1, "user", "fix the bug", sep30),
            _event("real", 2, "assistant", "fixed", sep30),
            _event("status-only", 1, "system", "session started", sep30),
            _event("summarized", 1, "user", "hi", sep30),
            _event("summarized", 2, "assistant", "hello", sep30),
            _event("unknown_session", 1, "user", "x", sep30),
        ],
    )
    _write(
        parquet_dir,
        "2026-08-01",
        [
            _event("old", 1, "user", "long ago", aug1),
            _event("old", 2, "assistant", "yes", aug1),
        ],
    )
    return parquet_dir


def _put_current_summary(path: Path, session_id: str) -> None:
    from drover.server.db import open_duckdb_connection

    con = open_duckdb_connection(path, read_only=True, role="diagnostic")
    try:
        version = source_version_for_session(con, session_id)
    finally:
        con.close()
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
