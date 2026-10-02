"""Tests for the embeddings client + worker."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
import requests

import drover.server.embeddings.worker as embedding_worker_module
from drover.schema import bootstrap
from drover.server.embeddings.client import (
    ApiEmbedder,
    EmbeddingBackendConfig,
    OllamaEmbedder,
)
from drover.server.embeddings.worker import EmbedWorker as _EmbedWorker
from drover.server.embeddings.worker import (
    build_span_embedding_text,
    enqueue_embed,
    enqueue_missing_span_embeds,
    enqueue_span_embed,
)
from drover.server.jobs import JobStream

# This module covers span jobs alongside session jobs, so workers here opt
# into the optional span integration (#473); the default is tested below.
EmbedWorker = functools.partial(_EmbedWorker, spans_enabled=True)
from drover.server.summarizer.backends.types import BackendError
from drover.server.wol import GpuRig

# --- client ------------------------------------------------------------------


class _FakeResp:
    def __init__(self, *, status=200, payload=None, ok=True, text=""):
        self.status_code = status
        self.text = text
        self.ok = ok
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _rig() -> GpuRig:
    return GpuRig(
        relay_url="http://relay:9753", ollama_url="http://gpu:11434", wake_timeout_s=5
    )


def test_embedder_returns_vector() -> None:
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.embeddings.client.requests.post",
            return_value=_FakeResp(payload={"embeddings": [[0.1, 0.2, 0.3]]}),
        ),
    ):
        e = OllamaEmbedder(rig=_rig())
        v = e.embed("hello")
    assert v == [0.1, 0.2, 0.3]


def test_embedder_batch_preserves_order() -> None:
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.embeddings.client.requests.post",
            return_value=_FakeResp(payload={"embeddings": [[1.0], [2.0], [3.0]]}),
        ),
    ):
        e = OllamaEmbedder(rig=_rig())
        out = e.embed_batch(["a", "b", "c"])
    assert out == [[1.0], [2.0], [3.0]]


def test_embedder_raises_on_count_mismatch() -> None:
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.embeddings.client.requests.post",
            return_value=_FakeResp(payload={"embeddings": [[1.0]]}),
        ),
    ):
        e = OllamaEmbedder(rig=_rig())
        with pytest.raises(BackendError, match="expected 2"):
            e.embed_batch(["a", "b"])


def test_embedder_raises_on_http_error() -> None:
    with (
        patch("drover.server.wol.requests.get", return_value=_FakeResp()),
        patch(
            "drover.server.embeddings.client.requests.post",
            side_effect=requests.ConnectionError("nope"),
        ),
    ):
        e = OllamaEmbedder(rig=_rig())
        with pytest.raises(BackendError, match="HTTP error"):
            e.embed("x")


def test_embedder_skips_wake_when_disabled() -> None:
    with (
        patch("drover.server.wol.requests.get") as mock_get,
        patch(
            "drover.server.embeddings.client.requests.post",
            return_value=_FakeResp(payload={"embeddings": [[0.0]]}),
        ),
    ):
        e = OllamaEmbedder(rig=_rig(), wake_on_first_call=False)
        e.embed("x")
    mock_get.assert_not_called()


def test_api_embedder_returns_openai_compatible_vectors() -> None:
    with patch(
        "drover.server.embeddings.client.requests.post",
        return_value=_FakeResp(
            payload={"data": [{"embedding": [0.4, 0.5]}, {"embedding": [0.6, 0.7]}]}
        ),
    ) as mock_post:
        e = ApiEmbedder(
            base_url="https://embeddings.example/v1",
            api_key="embed-key",
            model="text-embedding-test",
        )
        out = e.embed_batch(["one", "two"])

    assert out == [[0.4, 0.5], [0.6, 0.7]]
    url = mock_post.call_args.args[0]
    body = mock_post.call_args.kwargs["json"]
    headers = mock_post.call_args.kwargs["headers"]
    assert url == "https://embeddings.example/v1/embeddings"
    assert body == {"model": "text-embedding-test", "input": ["one", "two"]}
    assert headers["Authorization"] == "Bearer embed-key"


def test_embedding_backend_config_prefers_api_over_local_gpu() -> None:
    cfg = EmbeddingBackendConfig(
        api_base_url="https://embeddings.example/v1", api_key="k", gpu_rig=_rig()
    )
    embedder = cfg.select_embedder()
    assert isinstance(embedder, ApiEmbedder)


def test_embedding_backend_config_prefers_mac_local_ollama_before_gpu() -> None:
    cfg = EmbeddingBackendConfig(
        api_base_url=None,
        api_key=None,
        mac_ollama_url="http://127.0.0.1:11435",
        gpu_rig=_rig(),
    )
    embedder = cfg.select_embedder()
    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.ollama_url == "http://127.0.0.1:11435"
    assert embedder.wake_on_first_call is True
    assert embedder.launchd_label == "com.drover.mac-ollama-embeddings"


def test_mac_local_embedder_kickstarts_launchd_before_request() -> None:
    with (
        patch("drover.server.embeddings.client.subprocess.run") as mock_run,
        patch(
            "drover.server.wol.requests.get",
            return_value=_FakeResp(payload={"models": [{"name": "nomic-embed-text"}]}),
        ) as mock_get,
        patch(
            "drover.server.embeddings.client.requests.post",
            return_value=_FakeResp(payload={"embeddings": [[0.0]]}),
        ) as mock_post,
    ):
        e = OllamaEmbedder(
            ollama_url="http://127.0.0.1:11435",
            launchd_label="com.drover.mac-ollama-embeddings",
        )
        e.embed("x")

    assert mock_run.call_args.args[0][:3] == [
        "/bin/launchctl",
        "kickstart",
        f"gui/{__import__('os').getuid()}/com.drover.mac-ollama-embeddings",
    ]
    mock_get.assert_called_once_with("http://127.0.0.1:11435/api/tags", timeout=3.0)
    assert mock_post.call_args.args[0] == "http://127.0.0.1:11435/api/embed"


def test_embedding_backend_config_falls_back_to_gpu_without_api_or_mac_local() -> None:
    cfg = EmbeddingBackendConfig(
        api_base_url=None, api_key=None, mac_ollama_url=None, gpu_rig=_rig()
    )
    embedder = cfg.select_embedder()
    assert isinstance(embedder, OllamaEmbedder)
    assert embedder.ollama_url == "http://gpu:11434"
    assert embedder.wake_on_first_call is True


# --- worker ------------------------------------------------------------------


def _seed(tmp_path: Path) -> Path:
    parquet_dir = tmp_path / "p"
    duckdb_path = tmp_path / "n.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return duckdb_path


def _insert_summary(duckdb_path: Path, session_id: str, body: str) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            """INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md, files_touched, tools_used,
                last_user_prompt, last_assistant, next_steps_md, open_questions, status,
                generator_model, generated_at)
               VALUES (?, NULL, 'a', now(), ?, [], MAP{}, '', '', '', [], 'completed', 't', now())""",
            [session_id, body],
        )
    finally:
        con.close()


class _StubEmbedder:
    name = "stub"
    model = "stub-embed-v1"

    def __init__(self):
        self.calls = 0
        self.ensure_calls = 0
        self.last_texts = []

    def ensure_ready(self):
        self.ensure_calls += 1

    def embed_batch(self, texts):
        self.calls += 1
        self.last_texts = list(texts)
        return [[float(len(t)), 1.0, 2.0] for t in texts]


class _ReadinessFailingEmbedder(_StubEmbedder):
    def ensure_ready(self):
        self.ensure_calls += 1
        raise RuntimeError("local Ollama not ready")


def test_enqueue_embed_inserts_pending(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    assert enqueue_embed(duckdb_path, "S1") == "queued"
    assert enqueue_embed(duckdb_path, "S1") == "already_queued"


def test_span_embedding_text_redacts_and_truncates() -> None:
    text = build_span_embedding_text(
        {
            "name": "llm_call",
            "project": "nexus",
            "task_label": "debug embeddings",
            "activity_type": "completion",
            "prompt_preview": "contact a@example.com with api_key=sk-abc123 "
            + ("x" * 5000),
            "response_preview": "Bearer secret-token should not leak",
        },
        max_chars=240,
    )

    assert "name: llm_call" in text
    assert "project: nexus" in text
    assert "a@example.com" not in text
    assert "[REDACTED_EMAIL]" in text
    assert "sk-abc123" not in text
    assert "Bearer secret-token" not in text
    assert len(text) <= 255
    assert text.endswith("...[truncated]")


def test_enqueue_span_embed_inserts_pending(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    assert enqueue_span_embed(duckdb_path, "span-1") == "queued"
    assert enqueue_span_embed(duckdb_path, "span-1") == "already_queued"


def test_enqueue_missing_span_embeds_is_dry_run_and_idempotent(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("DROP VIEW spans")
        con.execute("""
            CREATE TABLE spans AS
            SELECT 'trace-1'::VARCHAR AS trace_id,
                   'span-new'::VARCHAR AS span_id,
                   'llm_call'::VARCHAR AS name
            UNION ALL
            SELECT 'trace-1'::VARCHAR, 'span-done'::VARCHAR, 'done_span'::VARCHAR
            UNION ALL
            SELECT 'trace-1'::VARCHAR, 'span-queued'::VARCHAR, 'queued_span'::VARCHAR
        """)
        con.execute("""INSERT INTO span_embeddings
           (span_id, trace_id, session_id, task_id, agent_id, repo_owner, repo_name,
            branch, source_text, source_fields, embedding, model, dim, embedded_at)
           VALUES ('span-done', 'trace-1', NULL, NULL, NULL, NULL, NULL, NULL,
                   'done', ['name'], [1.0], 'test', 1, now())""")
        con.execute(
            "INSERT INTO span_embed_jobs (span_id, status, attempts) VALUES ('span-queued', 'pending', 0)"
        )
    finally:
        con.close()

    dry = enqueue_missing_span_embeds(
        duckdb_path=duckdb_path,
        parquet_dir=tmp_path / "missing-parquet",
        limit=10,
        apply=False,
    )
    assert dry == {"candidate_count": 1, "enqueued": 0, "apply": False}

    applied = enqueue_missing_span_embeds(
        duckdb_path=duckdb_path,
        parquet_dir=tmp_path / "missing-parquet",
        limit=10,
        apply=True,
    )
    assert applied == {"candidate_count": 1, "enqueued": 1, "apply": True}

    con = duckdb.connect(str(duckdb_path))
    try:
        rows = con.execute(
            "SELECT span_id, status FROM span_embed_jobs ORDER BY span_id"
        ).fetchall()
    finally:
        con.close()

    assert rows == [("span-new", "pending"), ("span-queued", "pending")]


def test_worker_persists_span_embeddings(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("DROP VIEW spans")
        con.execute("DROP VIEW spans_enriched")
        con.execute("""
            CREATE TABLE spans AS
            SELECT 'trace-1'::VARCHAR AS trace_id,
                   'span-1'::VARCHAR AS span_id,
                   'llm_call'::VARCHAR AS name,
                   'session-1'::VARCHAR AS session_id,
                   'task-1'::VARCHAR AS task_id,
                   'agent-a'::VARCHAR AS agent_id,
                   'arniesaha'::VARCHAR AS repo_owner,
                   'nexus'::VARCHAR AS repo_name,
                   'feat-span-embeddings'::VARCHAR AS branch,
                   'nexus'::VARCHAR AS project,
                   'implement span embeddings'::VARCHAR AS task_label,
                   'completion'::VARCHAR AS activity_type,
                   'prompt with api_key=sk-secret'::VARCHAR AS prompt_preview,
                   'response body'::VARCHAR AS response_preview,
                   now() AS start_time
            """)
    finally:
        con.close()
    enqueue_span_embed(duckdb_path, "span-1")

    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=embedder)
    n = worker.drain_batch()

    assert n == 1
    assert embedder.calls == 1
    assert "api_key" not in embedder.last_texts[0]
    assert "[REDACTED_SECRET]" in embedder.last_texts[0]
    con = duckdb.connect(str(duckdb_path))
    try:
        rows = con.execute(
            "SELECT span_id, dim, source_text, repo_owner, repo_name, branch FROM span_embeddings"
        ).fetchall()
        jobs = con.execute("SELECT span_id, status FROM span_embed_jobs").fetchall()
    finally:
        con.close()

    assert rows[0][0] == "span-1"
    assert rows[0][1] == 3
    assert "prompt:" in rows[0][2]
    assert rows[0][3:] == ("arniesaha", "nexus", "feat-span-embeddings")
    assert jobs == [("span-1", "done")]


def test_default_worker_leaves_span_jobs_untouched(tmp_path: Path) -> None:
    """Spans off (#473): historical span jobs are neither claimed nor failed."""
    duckdb_path = _seed(tmp_path)
    enqueue_span_embed(duckdb_path, "span-1")
    embedder = _StubEmbedder()

    processed = _EmbedWorker(duckdb_path=duckdb_path, embedder=embedder).drain_batch()

    con = duckdb.connect(str(duckdb_path))
    try:
        jobs = con.execute(
            "SELECT span_id, status, attempts FROM span_embed_jobs"
        ).fetchall()
    finally:
        con.close()
    assert processed == 0
    assert embedder.calls == 0
    assert jobs == [("span-1", "pending", 0)]


def test_enqueue_embed_already_done(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    enqueue_embed(duckdb_path, "S1")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("UPDATE embed_jobs SET status='done' WHERE session_id='S1'")
    finally:
        con.close()
    assert enqueue_embed(duckdb_path, "S1") == "already_done"


def test_worker_persists_embeddings(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S1", "hello world")
    _insert_summary(duckdb_path, "S2", "another summary text")
    enqueue_embed(duckdb_path, "S1")
    enqueue_embed(duckdb_path, "S2")

    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=embedder)
    n = worker.drain_batch()
    assert n == 2
    assert embedder.calls == 1  # one batch call for both
    assert embedder.ensure_calls == 1

    con = duckdb.connect(str(duckdb_path))
    try:
        rows = con.execute(
            "SELECT session_id, dim, model FROM session_embeddings ORDER BY session_id"
        ).fetchall()
        jobs = con.execute(
            "SELECT session_id, status FROM embed_jobs ORDER BY session_id"
        ).fetchall()
    finally:
        con.close()

    assert [r[0] for r in rows] == ["S1", "S2"]
    assert all(r[1] == 3 for r in rows)
    assert all(r[2] == "stub-embed-v1" for r in rows)
    assert [j[1] for j in jobs] == ["done", "done"]


def test_worker_releases_claimed_jobs_when_readiness_fails(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S1", "hello world")
    enqueue_embed(duckdb_path, "S1")

    embedder = _ReadinessFailingEmbedder()
    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=embedder)
    n = worker.drain_batch()

    assert n == 0
    assert embedder.ensure_calls == 1
    con = duckdb.connect(str(duckdb_path))
    try:
        assert con.execute(
            "SELECT status FROM embed_jobs WHERE session_id='S1'"
        ).fetchone() == ("pending",)
    finally:
        con.close()


def test_worker_skips_jobs_without_summary(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    enqueue_embed(duckdb_path, "S-orphan")

    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=embedder)
    n = worker.drain_batch()
    assert n == 0
    assert embedder.calls == 0


def test_worker_marks_errored_on_backend_failure(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S3", "text")
    enqueue_embed(duckdb_path, "S3")

    class _Fail:
        name = "fail"
        model = "x"

        def ensure_ready(self):
            pass

        def embed_batch(self, texts):
            raise BackendError("simulated outage")

    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=_Fail())
    worker.drain_batch()

    con = duckdb.connect(str(duckdb_path))
    try:
        s, e = con.execute(
            "SELECT status, last_error FROM embed_jobs WHERE session_id='S3'"
        ).fetchone()
    finally:
        con.close()
    assert s == "errored"
    assert "simulated outage" in e


def test_worker_no_embedder_skips_drain(tmp_path: Path) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S4", "x")
    enqueue_embed(duckdb_path, "S4")

    worker = EmbedWorker(duckdb_path=duckdb_path)  # no embedder, no rig
    assert worker.drain_batch() == 0

    con = duckdb.connect(str(duckdb_path))
    try:
        s = con.execute(
            "SELECT status FROM embed_jobs WHERE session_id='S4'"
        ).fetchone()
    finally:
        con.close()
    # Job stays pending — try again later
    assert s[0] == "pending"


def test_worker_does_not_prepare_embedder_when_no_jobs(tmp_path: Path) -> None:
    """Idle polling must not wake/kickstart local embedding backends."""
    duckdb_path = _seed(tmp_path)
    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=duckdb_path, embedder=embedder)

    assert worker.drain_batch() == 0

    assert embedder.ensure_calls == 0
    assert embedder.calls == 0


def test_worker_acks_session_stream_after_durable_embedding_write(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S-stream-ok", "stream summary")
    enqueue_embed(duckdb_path, "S-stream-ok")
    stream = JobStream("embed_jobs")
    stream.add({"session_id": "S-stream-ok"})

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_StubEmbedder(),
        session_job_stream=stream,
        worker_id="embed-worker-a",
    )

    assert worker.drain_batch(max_jobs=1) == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_worker_leaves_failed_session_stream_job_unacked(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S-stream-fail", "stream summary")
    enqueue_embed(duckdb_path, "S-stream-fail")
    stream = JobStream("embed_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "S-stream-fail"})

    class _FailingEmbedder(_StubEmbedder):
        def embed_batch(self, texts):
            raise BackendError("embedding service down")

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_FailingEmbedder(),
        session_job_stream=stream,
        worker_id="embed-worker-a",
    )

    assert worker.drain_batch(max_jobs=1) == 1
    pending = stream.pending()
    assert len(pending) == 1
    assert pending[0].last_error == "embedding service down"
    reclaimed = stream.reclaim("embed-worker-b")
    assert len(reclaimed) == 1
    assert reclaimed[0].fields["session_id"] == "S-stream-fail"


def test_worker_acks_session_stream_redelivery_when_embedding_already_done(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    enqueue_embed(duckdb_path, "S-stream-done")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE embed_jobs SET status='done' WHERE session_id='S-stream-done'"
        )
    finally:
        con.close()
    stream = JobStream("embed_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "S-stream-done"})

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_StubEmbedder(),
        session_job_stream=stream,
        worker_id="embed-worker-b",
    )

    assert worker.drain_batch(max_jobs=1) == 0
    assert stream.pending() == []
    assert stream.length() == 0


def test_worker_acks_versioned_session_stream_redelivery_when_embedding_already_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S-versioned-done", "finished summary")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('S-versioned-done', 'done', 'v1')"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, source_version) "
            "VALUES ('S-versioned-done', 'done', 1, 'v1')"
        )
    finally:
        con.close()

    class _RejectTerminalUpdate:
        def __init__(self, path: Path) -> None:
            self._con = duckdb.connect(str(path))

        def execute(self, query: str, parameters=None):
            if "UPDATE embed_jobs e" in query:
                raise AssertionError("terminal embed job must not be updated")
            if parameters is None:
                return self._con.execute(query)
            return self._con.execute(query, parameters)

        def close(self) -> None:
            self._con.close()

    monkeypatch.setattr(
        embedding_worker_module,
        "open_duckdb_connection",
        lambda path: _RejectTerminalUpdate(path),
    )
    stream = JobStream("embed_jobs", visibility_timeout_ms=0)
    stream.add({"session_id": "S-versioned-done", "source_version": "v1"})

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_StubEmbedder(),
        session_job_stream=stream,
        worker_id="embed-worker-b",
    )

    assert worker.drain_batch(max_jobs=1) == 0
    assert stream.pending() == []
    assert stream.length() == 0


def test_worker_acks_obsolete_versioned_embed_without_backend_execution(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_summary(duckdb_path, "S-stale", "obsolete summary")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "INSERT INTO summarize_jobs (session_id, status, source_version) "
            "VALUES ('S-stale', 'pending', 'v2')"
        )
        con.execute(
            "INSERT INTO embed_jobs (session_id, status, attempts, source_version) "
            "VALUES ('S-stale', 'pending', 0, 'v1')"
        )
    finally:
        con.close()
    stream = JobStream("embed_jobs")
    stream.add({"session_id": "S-stale", "source_version": "v1"})
    embedder = _StubEmbedder()
    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=embedder,
        session_job_stream=stream,
    )

    assert worker.drain_batch(max_jobs=1) == 0
    assert embedder.calls == 0
    assert stream.pending() == []
    assert stream.length() == 0


def _insert_span_for_stream(duckdb_path: Path, span_id: str) -> None:
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("DROP VIEW spans")
        con.execute("DROP VIEW spans_enriched")
        con.execute(
            """
            CREATE TABLE spans AS
            SELECT 'trace-1'::VARCHAR AS trace_id,
                   ?::VARCHAR AS span_id,
                   'llm_call'::VARCHAR AS name,
                   'session-1'::VARCHAR AS session_id,
                   'task-1'::VARCHAR AS task_id,
                   'agent-a'::VARCHAR AS agent_id,
                   'arniesaha'::VARCHAR AS repo_owner,
                   'nexus'::VARCHAR AS repo_name,
                   'feat-stream-embeddings'::VARCHAR AS branch,
                   'nexus'::VARCHAR AS project,
                   'implement stream embeddings'::VARCHAR AS task_label,
                   'completion'::VARCHAR AS activity_type,
                   'prompt body'::VARCHAR AS prompt_preview,
                   'response body'::VARCHAR AS response_preview,
                   now() AS start_time
            """,
            [span_id],
        )
    finally:
        con.close()


def test_worker_acks_span_stream_after_durable_embedding_write(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_span_for_stream(duckdb_path, "span-stream-ok")
    enqueue_span_embed(duckdb_path, "span-stream-ok")
    stream = JobStream("span_embed_jobs")
    stream.add({"span_id": "span-stream-ok"})

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_StubEmbedder(),
        span_job_stream=stream,
        worker_id="span-worker-a",
    )

    assert worker.drain_batch(max_jobs=1) == 1
    assert stream.pending() == []
    assert stream.length() == 0


def test_worker_leaves_failed_span_stream_job_unacked(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    _insert_span_for_stream(duckdb_path, "span-stream-fail")
    enqueue_span_embed(duckdb_path, "span-stream-fail")
    stream = JobStream("span_embed_jobs", visibility_timeout_ms=0)
    stream.add({"span_id": "span-stream-fail"})

    class _FailingEmbedder(_StubEmbedder):
        def embed_batch(self, texts):
            raise BackendError("span embedding service down")

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_FailingEmbedder(),
        span_job_stream=stream,
        worker_id="span-worker-a",
    )

    assert worker.drain_batch(max_jobs=1) == 1
    pending = stream.pending()
    assert len(pending) == 1
    assert pending[0].last_error == "span embedding service down"
    reclaimed = stream.reclaim("span-worker-b")
    assert len(reclaimed) == 1
    assert reclaimed[0].fields["span_id"] == "span-stream-fail"


def test_worker_acks_span_stream_redelivery_when_embedding_already_done(
    tmp_path: Path,
) -> None:
    duckdb_path = _seed(tmp_path)
    enqueue_span_embed(duckdb_path, "span-stream-done")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE span_embed_jobs SET status='done' WHERE span_id='span-stream-done'"
        )
    finally:
        con.close()
    stream = JobStream("span_embed_jobs", visibility_timeout_ms=0)
    stream.add({"span_id": "span-stream-done"})

    worker = EmbedWorker(
        duckdb_path=duckdb_path,
        embedder=_StubEmbedder(),
        span_job_stream=stream,
        worker_id="span-worker-b",
    )

    assert worker.drain_batch(max_jobs=1) == 0
    assert stream.pending() == []
    assert stream.length() == 0


class _PoisonedClaimConnection:
    """Inject only the unavailable live ART ghost; all other SQL is real DuckDB.

    duckdb/duckdb#4886's UPDATE/INSERT repro succeeds on our pinned 1.5.5,
    so it cannot deterministically reproduce the live index corruption.
    """

    def __init__(self, path, table, failure, claim_error=duckdb.ConstraintException):
        self.con = duckdb.connect(str(path))
        self.table = table
        self.failure = failure
        self.claim_error = claim_error

    def execute(self, query, parameters=None):
        if (
            query.lstrip().startswith(f"UPDATE {self.table}")
            and parameters
            and "poison" in parameters
        ):
            raise self.claim_error(
                'Duplicate key "poison" violates primary key constraint'
            )
        if (
            self.failure
            and query.startswith(f"{self.failure} {self.table}")
            and parameters
            and "poison" in parameters
        ):
            raise duckdb.ConstraintException("quarantine index failure")
        return (
            self.con.execute(query, parameters)
            if parameters is not None
            else self.con.execute(query)
        )

    def close(self):
        self.con.close()


@pytest.mark.parametrize(
    "kind,streamed",
    [
        ("session", False),
        ("session", True),
        ("session", "versioned"),
        ("span", False),
        ("span", True),
    ],
)
@pytest.mark.parametrize("failure", [None, "DELETE FROM", "INSERT INTO"])
def test_poisoned_embed_claim_does_not_block_queue(
    tmp_path, monkeypatch, caplog, kind, streamed, failure
):
    path = _seed(tmp_path)
    table = "embed_jobs" if kind == "session" else "span_embed_jobs"
    key = f"{kind}_id"
    if kind == "span":
        _insert_span_for_stream(path, "poison")
        con = duckdb.connect(str(path))
        for name in ("healthy", "later"):
            con.execute(
                "INSERT INTO spans SELECT * REPLACE (? AS span_id) FROM spans WHERE span_id='poison'",
                [name],
            )
        con.close()
    else:
        for name in ("poison", "healthy", "later"):
            _insert_summary(path, name, name)
    con = duckdb.connect(str(path))
    for i, name in enumerate(("poison", "healthy", "later")):
        con.execute(
            f"INSERT INTO {table} ({key}, status, attempts, enqueued_at) VALUES (?, 'pending', 3, TIMESTAMP '2026-09-18' + ? * INTERVAL 1 SECOND)",
            [name, i],
        )
    if streamed == "versioned":
        for name in ("poison", "healthy", "later"):
            con.execute(
                "INSERT INTO summarize_jobs (session_id, status, source_version) VALUES (?, 'done', 'v1')",
                [name],
            )
        con.execute("UPDATE embed_jobs SET source_version='v1'")
    before = con.execute(
        f"SELECT enqueued_at FROM {table} WHERE {key}='poison'"
    ).fetchone()[0]
    con.close()
    monkeypatch.setattr(
        embedding_worker_module,
        "open_duckdb_connection",
        lambda path: _PoisonedClaimConnection(path, table, failure),
    )
    stream = JobStream(table, visibility_timeout_ms=0) if streamed else None
    if stream is not None:
        for name in ("poison", "healthy", "later"):
            stream.add(
                {
                    key: name,
                    **({"source_version": "v1"} if streamed == "versioned" else {}),
                }
            )
    worker = EmbedWorker(
        duckdb_path=path,
        embedder=_StubEmbedder(),
        **({f"{kind}_job_stream": stream} if streamed else {}),
    )
    assert worker.drain_batch(max_jobs=2) == 1
    assert worker.drain_batch(max_jobs=1) == 1
    if stream is not None:
        stream.add({key: "poison"})
    assert worker.drain_batch(max_jobs=1) == 0
    if stream is not None:
        assert stream.pending() == []
        assert stream.length() == 0
    con = duckdb.connect(str(path))
    assert con.execute(
        f"SELECT {key} FROM {kind}_embeddings ORDER BY {key}"
    ).fetchall() == [("healthy",), ("later",)]
    poison = con.execute(
        f"SELECT status, last_error, attempts, enqueued_at FROM {table} WHERE {key}='poison'"
    ).fetchone()
    if failure is None and streamed == "versioned":
        assert con.execute(
            "SELECT source_version FROM embed_jobs WHERE session_id='poison'"
        ).fetchone() == ("v1",)
    con.close()
    if failure is None:
        assert poison == ("errored", "constraint: index ghost; requeued", 3, before)
    elif failure == "DELETE FROM":
        assert poison[0] == "pending"
    else:
        assert poison is None  # DELETE committed, replacement INSERT failed.
    records = [r for r in caplog.records if getattr(r, "job_key", None) == "poison"]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].quarantine == ("errored" if failure is None else "memory_skip")


@pytest.mark.parametrize("name", ["summarizer", "briefs"])
@pytest.mark.parametrize("streamed", [False, True])
def test_other_workers_skip_poisoned_claim(tmp_path, monkeypatch, name, streamed):
    import importlib

    module = importlib.import_module(f"drover.server.{name}.worker")
    path = _seed(tmp_path)
    table, key = (
        ("summarize_jobs", "session_id")
        if name == "summarizer"
        else ("brief_jobs", "project_key")
    )
    con = duckdb.connect(str(path))
    for i, subject in enumerate(("poison", "healthy")):
        con.execute(
            f"INSERT INTO {table} ({key}, status, enqueued_at) VALUES (?, 'pending', TIMESTAMP '2026-09-18' + ? * INTERVAL 1 SECOND)",
            [subject, i],
        )
    con.close()
    monkeypatch.setattr(
        module,
        "open_duckdb_connection",
        lambda path, **kwargs: _PoisonedClaimConnection(path, table, "DELETE FROM"),
    )
    stream = JobStream(table, visibility_timeout_ms=0) if streamed else None
    if stream is not None:
        for subject in ("poison", "healthy"):
            stream.add({key: subject})
    cls = module.SummarizerWorker if name == "summarizer" else module.BriefWorker
    worker = cls(duckdb_path=path, job_stream=stream)
    claim = worker._claim_stream_job if streamed else worker._claim_duckdb_job
    first = claim()
    if name == "briefs" or streamed:
        assert first is None or first[0] is None
        first = claim()
    assert first[0] == "healthy"
    if name == "briefs" and not streamed:
        assert worker._oldest_pending_project() is None


def test_embed_claim_only_catches_constraint_exception(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    _insert_summary(path, "poison", "summary")
    enqueue_embed(path, "poison")
    monkeypatch.setattr(
        embedding_worker_module,
        "open_duckdb_connection",
        lambda path: _PoisonedClaimConnection(
            path, "embed_jobs", None, duckdb.BinderException
        ),
    )
    worker = EmbedWorker(duckdb_path=path, embedder=_StubEmbedder())
    with pytest.raises(duckdb.BinderException):
        worker.drain_batch()
    assert not worker._session_quarantine.skipped
