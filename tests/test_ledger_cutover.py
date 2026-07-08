"""Tests for the AGE-45 cutover onto the durable pipeline ledger.

These verify the issue's acceptance criteria:

* workers recover runnable work after a process crash by reconciling from DuckDB;
* Redis loss does not lose durable job state (the lifecycle never touches Redis);
* replay regenerates artifacts without duplicating serving rows and preserves
  append-only attempt/artifact lineage.

The recovery/replay primitives are exercised both directly against the ledger and
end-to-end through the summarizer worker.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from drover.schema import bootstrap
from drover.server import ledger_shadow
from drover.server.ledger import Ledger
from drover.server.summarizer.worker import SummarizerWorker


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _count(duckdb_path: Path, table: str, where: str = "") -> int:
    con = duckdb.connect(str(duckdb_path))
    try:
        clause = f" WHERE {where}" if where else ""
        return con.execute(f"SELECT count(*) FROM {table}{clause}").fetchone()[0]
    finally:
        con.close()


def _scalar(duckdb_path: Path, sql: str, params: list | None = None):
    con = duckdb.connect(str(duckdb_path))
    try:
        return con.execute(sql, params or []).fetchone()[0]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Ledger-level reclaim primitives                                             #
# --------------------------------------------------------------------------- #


def test_reclaim_lease_walks_leased_job_back_to_pending(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        ledger = Ledger(con)
        job = ledger.open_job(job_kind="summarize_session", subject_key="s1").job
        ledger.lease_job(job.job_id, worker_id="w1")
        assert ledger._load_job(job.job_id).status == "leased"

        reclaimed = ledger.reclaim_stale_leases(job_kind="summarize_session")
        assert reclaimed == [job.job_id]

        recovered = ledger._load_job(job.job_id)
        assert recovered.status == "pending"
        # The crashed attempt is closed append-only as retryable_failed.
        assert (
            con.execute(
                "SELECT count(*) FROM pipeline_job_attempts "
                "WHERE job_id=? AND result='retryable_failed'",
                [job.job_id],
            ).fetchone()[0]
            == 1
        )
    finally:
        con.close()


def test_stale_before_filters_fresh_leases(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        ledger = Ledger(con)
        job = ledger.open_job(job_kind="summarize_session", subject_key="s1").job
        ledger.lease_job(job.job_id, worker_id="w1")
        # A staleness cut in the past leaves a just-taken lease untouched.
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        assert ledger.reclaim_stale_leases(stale_before=past) == []
        assert ledger._load_job(job.job_id).status == "leased"
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Crash recovery — reconcile from DuckDB                                      #
# --------------------------------------------------------------------------- #


def _crash_in_flight(duckdb_path: Path, session_id: str) -> str:
    """Simulate a worker that died mid-job: serving row 'running' + leased ledger."""
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) "
            "VALUES (?, 'running', 1)",
            [session_id],
        )
    finally:
        con.close()
    job_id = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key=session_id,
        subject_kind="session",
    )
    assert job_id is not None
    return job_id


def test_recover_runnable_reconciles_serving_and_ledger(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    job_id = _crash_in_flight(duckdb_path, "sess-crash")

    res = ledger_shadow.recover_runnable(duckdb_path, job_kind="summarize_session")

    assert res["serving_reset"] == 1
    assert res["leases_reclaimed"] == [job_id]
    assert (
        _scalar(
            duckdb_path,
            "SELECT status FROM summarize_jobs WHERE session_id='sess-crash'",
        )
        == "pending"
    )
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [job_id]
        )
        == "pending"
    )


def test_worker_start_recovers_and_drains_crashed_job(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-crash")
    job_id = _crash_in_flight(duckdb_path, "sess-crash")

    # start() reconciles the crashed job from DuckDB, then the poll loop drains it.
    worker = SummarizerWorker(duckdb_path=duckdb_path, _llm_call=_fake_llm_call)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = _scalar(
                duckdb_path,
                "SELECT status FROM summarize_jobs WHERE session_id='sess-crash'",
            )
            if status == "done":
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert (
        _scalar(
            duckdb_path,
            "SELECT status FROM summarize_jobs WHERE session_id='sess-crash'",
        )
        == "done"
    )
    assert _count(duckdb_path, "session_summaries", "session_id='sess-crash'") == 1
    # Reclaimed attempt (retryable_failed) + the successful re-run attempt: lineage
    # is append-only, never overwritten.
    assert _count(duckdb_path, "pipeline_job_attempts", f"job_id='{job_id}'") == 2
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "artifact_kind='session_summary' AND subject_key='sess-crash' AND is_current",
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# Redis-loss durability                                                       #
# --------------------------------------------------------------------------- #


def test_durable_state_survives_without_redis(tmp_path: Path) -> None:
    """The full job lifecycle is DuckDB-only; no Redis client is ever required."""
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-noredis")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) "
            "VALUES ('sess-noredis', 'pending', 0)"
        )
    finally:
        con.close()

    # No shadow_publisher / Redis anywhere in this path.
    worker = SummarizerWorker(duckdb_path=duckdb_path, _llm_call=_fake_llm_call)
    assert worker.drain_once() == 1

    # Durable truth is fully reconstructable from DuckDB alone.
    assert (
        _scalar(
            duckdb_path,
            "SELECT status FROM pipeline_jobs "
            "WHERE job_kind='summarize_session' AND subject_key='sess-noredis'",
        )
        == "succeeded"
    )
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "subject_key='sess-noredis' AND is_current",
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# Operator replay                                                             #
# --------------------------------------------------------------------------- #


def test_replay_regenerates_without_duplicate_serving_rows(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-replay")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) "
            "VALUES ('sess-replay', 'pending', 0)"
        )
    finally:
        con.close()

    worker = SummarizerWorker(duckdb_path=duckdb_path, _llm_call=_fake_llm_call)
    assert worker.drain_once() == 1
    first_job = _scalar(
        duckdb_path,
        "SELECT job_id FROM pipeline_jobs WHERE subject_key='sess-replay'",
    )

    # Dry-run preview leaves everything untouched.
    preview = ledger_shadow.replay(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="sess-replay",
        apply=False,
    )
    assert preview["eligible"] is True
    assert preview["ledger_status"] == "succeeded"
    assert preview["serving_reset"] is False
    assert (
        _scalar(
            duckdb_path,
            "SELECT status FROM summarize_jobs WHERE session_id='sess-replay'",
        )
        == "done"
    )

    # Apply: promote the finished job back to pending.
    res = ledger_shadow.replay(
        duckdb_path, job_kind="summarize_session", subject_key="sess-replay", apply=True
    )
    assert res["serving_reset"] is True
    assert (
        _scalar(
            duckdb_path,
            "SELECT status FROM summarize_jobs WHERE session_id='sess-replay'",
        )
        == "pending"
    )
    # No duplicate serving row — the subject key is the primary key.
    assert _count(duckdb_path, "summarize_jobs", "session_id='sess-replay'") == 1
    # Prior winner superseded; a fresh logical generation now exists.
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [first_job]
        )
        == "superseded"
    )

    # Re-drain regenerates the artifact.
    assert worker.drain_once() == 1
    assert _count(duckdb_path, "summarize_jobs", "session_id='sess-replay'") == 1
    # Exactly one current artifact survives; history is preserved (>=2 rows total).
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "subject_key='sess-replay' AND is_current",
        )
        == 1
    )
    assert _count(duckdb_path, "pipeline_artifacts", "subject_key='sess-replay'") >= 2
    # Serving projection stays a single row.
    assert _count(duckdb_path, "session_summaries", "session_id='sess-replay'") == 1


def test_replay_missing_subject_is_noop(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    res = ledger_shadow.replay(
        duckdb_path, job_kind="summarize_session", subject_key="nope", apply=True
    )
    assert res["ledger_status"] is None
    assert res["eligible"] is False
    assert res["serving_reset"] is False


def test_replay_refuses_leased_job(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    # Leased == in flight; replay must refuse rather than race it.
    ledger_shadow.begin_attempt(
        duckdb_path, job_kind="summarize_session", subject_key="s-leased"
    )
    res = ledger_shadow.replay(
        duckdb_path, job_kind="summarize_session", subject_key="s-leased", apply=True
    )
    assert res["ledger_status"] == "leased"
    assert res["eligible"] is False


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _write_events(parquet_dir: Path, session_id: str) -> None:
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
            "e1",
            session_id,
            "macmini-claude",
            "tid1",
            now - timedelta(minutes=2),
            "user_message",
            "user",
            "do the thing",
            "arniesaha",
            "nexus",
            "main",
            "arnab",
            f"{session_id}-k1",
            "{}",
        ),
        (
            "e2",
            session_id,
            "macmini-claude",
            "tid1",
            now - timedelta(minutes=1),
            "tool_call",
            "assistant",
            "edited foo.py",
            "arniesaha",
            "nexus",
            "main",
            "arnab",
            f"{session_id}-k2",
            "{}",
        ),
    ]
    cols = {f.name: [r[i] for r in rows] for i, f in enumerate(schema)}
    table = pa.table(
        {k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()},
        schema=schema,
    )
    out = parquet_dir / "agent_events" / "date=2026-05-09" / "agent_id=macmini-claude"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"part-{session_id}.parquet")


def _fake_llm_call(prompt: str, *, api_key, model, _client=None, **kw) -> dict:
    return {
        "summary_md": "Fixture summary describing the session.",
        "next_steps_md": "Move on.",
        "open_questions": [],
        "last_user_prompt": "do the thing",
        "last_assistant": "edited foo.py",
    }
