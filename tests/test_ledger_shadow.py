"""Tests for the AGE-44 shadow-write wiring into the durable pipeline ledger.

These verify the acceptance criteria of "shadow-write the durable ledger from
existing pipeline paths":

* duplicate source ingestion is a receipt no-op (no duplicate downstream job);
* retries append attempts; successful derive writes emit artifact lineage;
* the shadow is best-effort — a missing ledger never breaks the live path.

The worker-side lifecycle helpers (begin_attempt / succeed / retry) are exercised
directly against a bootstrapped DuckDB, plus one end-to-end pass through the
summarizer worker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.schema import bootstrap
from drover.server import ledger_shadow
from drover.server.ingest import ingest_file
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
# Receipts                                                                     #
# --------------------------------------------------------------------------- #


def _write_jsonl(path: Path, *, session_id: str, n: int) -> None:
    events = []
    for i in range(n):
        ts = datetime(2026, 6, 19, 9, 0, i, tzinfo=timezone.utc)
        events.append(
            {
                "id": f"{session_id}-e{i}",
                "session_id": session_id,
                "timestamp": ts.isoformat(),
                "agent_id": "a1",
                "event_type": "user_message",
                "message": {"role": "user", "content": f"hello {i}"},
                "raw_data": {"_repo_owner": "acme", "_repo_name": "widgets"},
            }
        )
    path.write_text("\n".join(json.dumps(e) for e in events))


def test_ingest_records_one_receipt_per_accepted_event(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(jsonl, session_id="s1", n=3)

    stats = ingest_file(jsonl, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats.inserted == 3
    assert stats.ledger_receipts == 3
    assert _count(duckdb_path, "pipeline_receipts") == 3
    # Receipts carry their session subject for downstream lineage.
    assert _count(duckdb_path, "pipeline_receipts", "subject_key = 's1'") == 3
    assert _count(duckdb_path, "pipeline_receipts", "source_kind = 'agent_event'") == 3


def test_duplicate_ingestion_is_a_receipt_noop(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(jsonl, session_id="s1", n=3)

    ingest_file(jsonl, parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    # Re-ingesting the same file: rows dedupe, so no new receipts are written.
    stats2 = ingest_file(jsonl, parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    assert stats2.inserted == 0
    assert stats2.ledger_receipts == 0
    assert _count(duckdb_path, "pipeline_receipts") == 3


def test_record_receipt_is_idempotent_lookup(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        first = ledger_shadow.record_receipt(
            con, source_kind="agent_event", source_key="k1", subject_key="s1"
        )
        second = ledger_shadow.record_receipt(
            con, source_kind="agent_event", source_key="k1", subject_key="s1"
        )
    finally:
        con.close()

    assert first is not None and first.is_duplicate is False
    assert second is not None and second.is_duplicate is True
    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert _count(duckdb_path, "pipeline_receipts") == 1


def test_record_receipt_swallows_missing_table(tmp_path: Path) -> None:
    # No bootstrap → pipeline_receipts does not exist; must not raise.
    duckdb_path = tmp_path / "bare.duckdb"
    con = duckdb.connect(str(duckdb_path))
    try:
        assert (
            ledger_shadow.record_receipt(
                con, source_kind="agent_event", source_key="k1"
            )
            is None
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Job lifecycle (begin_attempt / succeed / retry)                             #
# --------------------------------------------------------------------------- #


def test_begin_attempt_creates_and_leases(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    job_id = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    assert job_id is not None
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [job_id]
        )
        == "leased"
    )
    assert _count(duckdb_path, "pipeline_job_attempts", f"job_id = '{job_id}'") == 1


def test_success_emits_artifact_lineage(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    job_id = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    ledger_shadow.succeed(
        duckdb_path,
        job_id,
        artifact_kind="session_summary",
        subject_key="s1",
        storage_uri="session_summaries/s1",
    )
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [job_id]
        )
        == "succeeded"
    )
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            f"job_id='{job_id}' AND artifact_kind='session_summary' AND is_current",
        )
        == 1
    )


def test_retry_then_success_appends_a_second_attempt(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    # First attempt fails → retry parks the logical job in retry_wait.
    job_id = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    ledger_shadow.retry(duckdb_path, job_id, error_message="boom")
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [job_id]
        )
        == "retry_wait"
    )

    # Second drain reuses the SAME logical job (no duplicate), appending attempt 2.
    job_id_2 = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    assert job_id_2 == job_id
    ledger_shadow.succeed(
        duckdb_path, job_id_2, artifact_kind="session_summary", subject_key="s1"
    )

    assert _count(duckdb_path, "pipeline_jobs", "subject_key='s1'") == 1
    assert _count(duckdb_path, "pipeline_job_attempts", f"job_id='{job_id}'") == 2
    # One retryable_failed attempt and one succeeded attempt.
    assert (
        _count(
            duckdb_path,
            "pipeline_job_attempts",
            f"job_id='{job_id}' AND result='retryable_failed'",
        )
        == 1
    )
    assert (
        _count(
            duckdb_path,
            "pipeline_job_attempts",
            f"job_id='{job_id}' AND result='succeeded'",
        )
        == 1
    )


def test_replay_supersedes_prior_winner(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    first = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    ledger_shadow.succeed(
        duckdb_path, first, artifact_kind="session_summary", subject_key="s1"
    )

    # A later re-summarize of the same session: prior winner is superseded and a
    # fresh logical generation is opened.
    second = ledger_shadow.begin_attempt(
        duckdb_path,
        job_kind="summarize_session",
        subject_key="s1",
        subject_kind="session",
    )
    assert second != first
    ledger_shadow.succeed(
        duckdb_path, second, artifact_kind="session_summary", subject_key="s1"
    )

    assert (
        _scalar(duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [first])
        == "superseded"
    )
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM pipeline_jobs WHERE job_id=?", [second]
        )
        == "succeeded"
    )
    # Exactly one current session_summary artifact survives; history is preserved.
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "artifact_kind='session_summary' AND subject_key='s1' AND is_current",
        )
        == 1
    )
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "artifact_kind='session_summary' AND subject_key='s1'",
        )
        == 2
    )


def test_succeed_and_retry_are_noops_without_job(tmp_path: Path) -> None:
    _, duckdb_path = _seed(tmp_path)
    # None job_id (begin_attempt could not lease) must be safe.
    ledger_shadow.succeed(
        duckdb_path, None, artifact_kind="session_summary", subject_key="s1"
    )
    ledger_shadow.retry(duckdb_path, None, error_message="x")
    assert _count(duckdb_path, "pipeline_artifacts") == 0


# --------------------------------------------------------------------------- #
# End-to-end through the summarizer worker                                     #
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


def test_summarizer_worker_shadow_writes_job_and_artifact(tmp_path: Path) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _write_events(parquet_dir, "sess-LS1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, attempts) VALUES (?, 'pending', 0)",
            ["sess-LS1"],
        )
    finally:
        con.close()

    worker = SummarizerWorker(duckdb_path=duckdb_path, _llm_call=_fake_llm_call)
    assert worker.drain_once() == 1

    # Live serving table still works unchanged.
    assert (
        _scalar(
            duckdb_path, "SELECT status FROM summarize_jobs WHERE session_id='sess-LS1'"
        )
        == "done"
    )
    assert _count(duckdb_path, "session_summaries", "session_id='sess-LS1'") == 1

    # Shadow ledger recorded the logical job, one attempt, and artifact lineage.
    assert (
        _count(
            duckdb_path,
            "pipeline_jobs",
            "job_kind='summarize_session' AND subject_key='sess-LS1' AND status='succeeded'",
        )
        == 1
    )
    assert _count(duckdb_path, "pipeline_job_attempts", "result='succeeded'") == 1
    assert (
        _count(
            duckdb_path,
            "pipeline_artifacts",
            "artifact_kind='session_summary' AND subject_key='sess-LS1' AND is_current",
        )
        == 1
    )
