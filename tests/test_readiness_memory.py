"""``/readyz`` reports derived memory: pgvector, embeddings and job health (#480).

#471 was session embeddings stopping for days -- a disabled service flag and a
poisoned queue row -- with nothing red anywhere. Readiness now carries the
memory store: a missing pgvector extension fails it outright, a disabled
embedder is stated, and every job kind reports its backlog, lease age, dead
letters and last success.
"""

from __future__ import annotations

import json

from conftest import pgvector_available

from drover.server.ledger import EMBED_SESSION, SUMMARIZE_SESSION, JobLedger
from drover.server.metrics import MetricsCollector
from drover.server.readiness import (
    STATE_FAILED,
    STATE_OK,
    STORE_CONTROL_PLANE,
    STORE_MEMORY,
    ReadinessProbe,
)


def _probe(path, **kw) -> ReadinessProbe:
    return ReadinessProbe(path, include_analytical=False, cache_seconds=0, **kw)


def test_memory_section_reports_per_kind_job_health(pg_control_path, postgres_dsn):
    ledger = JobLedger(pg_control_path, jitter=lambda a, b: 0.0)
    ledger.enqueue(SUMMARIZE_SESSION, "s1", source_version="v1")
    ledger.enqueue(SUMMARIZE_SESSION, "s2", source_version="v1")
    ledger.enqueue(EMBED_SESSION, "s0", source_version="v1")
    [job] = ledger.claim(EMBED_SESSION, worker_id="w")
    ledger.fail(job, "bad vector", retryable=False, category="embedding_mismatch")
    [running] = ledger.claim(SUMMARIZE_SESSION, worker_id="w")

    report = _probe(
        pg_control_path,
        embeddings={"enabled": False, "backend": None, "detail": "disabled"},
    ).check()
    memory = report.memory
    assert memory["available"] is True
    assert memory["embeddings"]["enabled"] is False
    summarize = memory["jobs"][SUMMARIZE_SESSION]
    assert summarize["pending"] == 1 and summarize["running"] == 1
    assert summarize["oldest_lease_age_seconds"] is not None
    assert summarize["oldest_pending_age_seconds"] is not None
    embed = memory["jobs"][EMBED_SESSION]
    assert embed["quarantined"] == 1 and "bad vector" in embed["last_error"]
    assert set(memory["jobs"]) == {
        "summarize_session",
        "embed_session",
        "brief_project",
        "recap_session",
    }
    states = {s.store: s for s in report.stores}
    assert states[STORE_CONTROL_PLANE].state == STATE_OK
    if pgvector_available(postgres_dsn):
        assert states[STORE_MEMORY].state == STATE_OK
        assert "embeddings disabled" in states[STORE_MEMORY].detail
    del running


def test_missing_pgvector_fails_readiness_with_a_clear_message(
    pg_control_path, postgres_dsn
):
    if pgvector_available(postgres_dsn):
        import pytest

        pytest.skip("pgvector is installed on this server")
    report = _probe(pg_control_path).check()
    memory_store = next(s for s in report.stores if s.store == STORE_MEMORY)
    assert memory_store.state == STATE_FAILED
    assert "pgvector is not installed" in memory_store.detail
    assert report.ok is False
    status, body = report.as_response()
    assert status == 503
    assert json.loads(body)["memory"]["vector"]["ready"] is False


def test_anonymous_callers_get_counts_but_not_error_text(pg_control_path):
    ledger = JobLedger(pg_control_path, jitter=lambda a, b: 0.0)
    ledger.enqueue(EMBED_SESSION, "s", source_version="v")
    [job] = ledger.claim(EMBED_SESSION, worker_id="w")
    ledger.fail(job, "secret /Users/someone/path", retryable=False)
    _, body = _probe(pg_control_path).check().as_response(include_detail=False)
    payload = json.loads(body)
    assert payload["memory"]["jobs"][EMBED_SESSION]["quarantined"] == 1
    assert "last_error" not in payload["memory"]["jobs"][EMBED_SESSION]
    assert "/Users/someone" not in body


def test_duckdb_control_store_reports_memory_unavailable_without_failing(tmp_path):
    from drover.schema import bootstrap

    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    report = _probe(path, embeddings={"enabled": True}).check()
    assert report.ok
    assert STORE_MEMORY not in {s.store for s in report.stores}
    assert report.memory["available"] is False
    assert "postgres" in report.memory["detail"]


def test_metrics_collector_passes_embedding_state_to_readiness(pg_control_path):
    collector = MetricsCollector(
        duckdb_path=pg_control_path,
        incoming_dir=pg_control_path.parent / "incoming",
        summarizer_report={},
        include_analytical_readiness=False,
        embeddings_state={"enabled": True, "backend": "api", "model": "m"},
    )
    _, body = collector.render_readiness()
    payload = json.loads(body)
    assert payload["memory"]["embeddings"] == {
        "enabled": True,
        "backend": "api",
        "model": "m",
    }


def test_memory_job_metrics_render_per_kind(pg_control_path):
    from drover.server.metrics import _append_operational_health_metrics

    JobLedger(pg_control_path).enqueue(SUMMARIZE_SESSION, "s", source_version="v")
    lines: list[str] = []
    _append_operational_health_metrics(lines, pg_control_path, {})
    text = "\n".join(lines)
    assert 'drover_memory_jobs{kind="summarize_session",status="pending"} 1' in text
    assert 'drover_memory_job_last_success_timestamp{kind="embed_session"} 0' in text
