"""Tests for the embeddings client + the PG-ledger embed worker (#480)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from conftest import pgvector_available

import drover.server.embeddings.worker as embedding_worker_module
from drover.server.db import control_plane_connection
from drover.server.embeddings.client import (
    DEFAULT_EMBED_MODEL,
    ApiEmbedder,
    EmbeddingBackendConfig,
    OllamaEmbedder,
)
from drover.server.embeddings.worker import EmbedWorker
from drover.server.ledger import EMBED_SESSION, JobLedger
from drover.server.memory_store import EmbeddingStore, MemoryRepository, SessionSummary
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
    assert embedder.launchd_label is None


def test_mac_local_embedder_probes_before_launchd() -> None:
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

    mock_run.assert_not_called()
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

DIM = 768


def _put_summary(
    path: Path, session_id: str, *, version: str = "v1", body: str | None = None
) -> None:
    with control_plane_connection(path) as con:
        MemoryRepository.put_summary(
            con,
            SessionSummary(
                session_id=session_id,
                summary_md=body if body is not None else f"summary for {session_id}",
                project_key="o/r",
                source_version=version,
            ),
        )


def _enqueue(path: Path, session_id: str, *, version: str = "v1") -> None:
    assert (
        JobLedger(path).enqueue(EMBED_SESSION, session_id, source_version=version)
        == "queued"
    )


def _job(path: Path, session_id: str):
    return JobLedger(path).latest(EMBED_SESSION, session_id)


class _StubEmbedder:
    """Embeds every text as a 768-d vector; texts containing 'short' get 767."""

    name = "stub"
    model = DEFAULT_EMBED_MODEL

    def __init__(self):
        self.calls = 0
        self.ensure_calls = 0
        self.last_texts: list[str] = []

    def ensure_ready(self):
        self.ensure_calls += 1

    def embed_batch(self, texts):
        self.calls += 1
        self.last_texts = list(texts)
        return [[0.1] * (DIM - 1 if "short" in t else DIM) for t in texts]


class _ReadinessFailingEmbedder(_StubEmbedder):
    def ensure_ready(self):
        self.ensure_calls += 1
        raise RuntimeError("local Ollama not ready")


class _FailingEmbedder(_StubEmbedder):
    def embed_batch(self, texts):
        self.calls += 1
        raise BackendError("embeddings: 503 overloaded")


class _RecordingStore(EmbeddingStore):
    """Real validation, in-memory persistence: the success path without pgvector."""

    puts: dict[str, tuple[int, str, str]] = {}

    def put(self, con, session_id, vector, *, model, source_version=""):
        self._validate(vector, model)
        type(self).puts[session_id] = (len(vector), model, source_version)


@pytest.fixture
def recording_store(monkeypatch):
    _RecordingStore.puts = {}
    monkeypatch.setattr(embedding_worker_module, "EmbeddingStore", _RecordingStore)
    return _RecordingStore


def test_worker_idle_drain_does_not_touch_embedder(pg_control_path: Path) -> None:
    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=embedder)
    worker._resolve_embedder = lambda: pytest.fail("idle drain resolved the embedder")
    assert worker.drain_batch() == 0
    assert embedder.ensure_calls == 0 and embedder.calls == 0


def test_worker_without_postgres_memory_is_a_noop(tmp_path: Path) -> None:
    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=tmp_path / "drover.duckdb", embedder=embedder)
    assert worker.drain_batch() == 0
    assert worker.drain_batch() == 0
    assert embedder.ensure_calls == 0


def test_worker_persists_embedding_and_completes(
    pg_control_path: Path, recording_store
) -> None:
    _put_summary(pg_control_path, "S1", body="hello world")
    _enqueue(pg_control_path, "S1")
    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=embedder)

    assert worker.drain_batch() == 1
    assert embedder.last_texts == ["hello world"]
    assert recording_store.puts == {"S1": (DIM, DEFAULT_EMBED_MODEL, "v1")}
    assert _job(pg_control_path, "S1").status == "succeeded"
    assert worker.drain_batch() == 0


def test_worker_supersedes_stale_and_missing_summaries(pg_control_path: Path) -> None:
    _put_summary(pg_control_path, "stale", version="v2")
    _enqueue(pg_control_path, "stale", version="v1")
    _enqueue(pg_control_path, "missing")
    embedder = _StubEmbedder()
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=embedder)

    assert worker.drain_batch() == 2
    stale = _job(pg_control_path, "stale")
    missing = _job(pg_control_path, "missing")
    assert (
        stale.status == "superseded" and "source version v2" in stale.disposition_reason
    )
    assert (
        missing.status == "superseded"
        and "summary missing" in missing.disposition_reason
    )
    assert stale.failures == 0 and missing.failures == 0
    assert embedder.ensure_calls == 0 and embedder.calls == 0


def test_worker_releases_jobs_without_an_embedder(pg_control_path: Path) -> None:
    _put_summary(pg_control_path, "S1")
    _enqueue(pg_control_path, "S1")
    worker = EmbedWorker(duckdb_path=pg_control_path)  # no embedder, no config

    assert worker.drain_batch() == 0
    job = _job(pg_control_path, "S1")
    assert job.status == "retry_wait" and job.error_category == "released"
    assert job.failures == 0
    assert "no embedder configured" in job.last_error


def test_worker_releases_jobs_when_readiness_fails(pg_control_path: Path) -> None:
    _put_summary(pg_control_path, "S1")
    _enqueue(pg_control_path, "S1")
    embedder = _ReadinessFailingEmbedder()
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=embedder)

    assert worker.drain_batch() == 0
    job = _job(pg_control_path, "S1")
    assert job.status == "retry_wait" and job.failures == 0
    assert "local Ollama not ready" in job.last_error
    assert embedder.ensure_calls == 1 and embedder.calls == 0


def test_worker_releases_jobs_when_embedder_model_differs(
    pg_control_path: Path,
) -> None:
    _put_summary(pg_control_path, "S1")
    _enqueue(pg_control_path, "S1")
    embedder = _StubEmbedder()
    cfg = EmbeddingBackendConfig(api_base_url="https://e.example/v1", api_key="k")
    worker = EmbedWorker(
        duckdb_path=pg_control_path, embedder=embedder, embedding_config=cfg
    )
    assert worker.configured_model == cfg.api_model

    assert worker.drain_batch() == 0
    job = _job(pg_control_path, "S1")
    assert job.status == "retry_wait" and job.failures == 0
    assert "does not match the configured embedding model" in job.last_error
    assert embedder.calls == 0


def test_worker_retries_on_backend_error(pg_control_path: Path) -> None:
    _put_summary(pg_control_path, "S1")
    _enqueue(pg_control_path, "S1")
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=_FailingEmbedder())

    assert worker.drain_batch() == 1
    job = _job(pg_control_path, "S1")
    assert job.status == "retry_wait" and job.failures == 1
    assert job.error_category == "embed_backend" and "503" in job.last_error


def test_worker_quarantines_mismatched_vector_without_blocking_sibling(
    pg_control_path: Path, recording_store
) -> None:
    _put_summary(pg_control_path, "bad", body="short vector")
    _put_summary(pg_control_path, "good", body="a full vector")
    _enqueue(pg_control_path, "bad")
    _enqueue(pg_control_path, "good")
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=_StubEmbedder())

    assert worker.drain_batch() == 2
    bad = _job(pg_control_path, "bad")
    assert bad.status == "quarantined" and bad.error_category == "embedding_mismatch"
    assert "dimensions" in bad.disposition_reason
    assert _job(pg_control_path, "good").status == "succeeded"
    assert set(recording_store.puts) == {"good"}


def test_worker_quarantines_malformed_row_without_blocking_sibling(
    pg_control_path, recording_store
):
    class MalformedEmbedder(_StubEmbedder):
        def embed_batch(self, texts):
            return [None if "bad" in text else [0.1] * DIM for text in texts]

    for sid in ("bad", "good"):
        _put_summary(pg_control_path, sid)
        _enqueue(pg_control_path, sid)
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=MalformedEmbedder())
    assert worker.drain_batch() == 2
    assert _job(pg_control_path, "bad").status == "quarantined"
    assert _job(pg_control_path, "good").status == "succeeded"
    assert set(recording_store.puts) == {"good"}


def test_worker_releases_when_the_embedder_space_has_the_wrong_dimension(
    pg_control_path: Path, recording_store
) -> None:
    """A 1536-d model against vector(768) is config, not 2 poisoned rows."""
    for sid in ("a", "b"):
        _put_summary(pg_control_path, sid, body="short vector")
        _enqueue(pg_control_path, sid)
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=_StubEmbedder())

    assert worker.drain_batch() == 0
    for sid in ("a", "b"):
        job = _job(pg_control_path, sid)
        assert job.status == "retry_wait" and job.failures == 0
        assert "vector(768)" in job.last_error
    assert recording_store.puts == {}


def test_worker_releases_jobs_when_pgvector_is_missing(
    pg_control_path: Path, postgres_dsn: str, caplog
) -> None:
    if pgvector_available(postgres_dsn):
        pytest.skip("pgvector is installed on this server")
    for sid in ("S1", "S2"):
        _put_summary(pg_control_path, sid)
        _enqueue(pg_control_path, sid)
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=_StubEmbedder())

    with caplog.at_level(logging.ERROR, logger="drover.embeddings.worker"):
        assert worker.drain_batch() == 0
    for sid in ("S1", "S2"):
        job = _job(pg_control_path, sid)
        assert job.status == "retry_wait" and job.failures == 0
        assert "pgvector" in job.last_error
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "pgvector" in errors[0].getMessage()


def test_worker_writes_pgvector_rows_end_to_end(
    pg_control_path: Path, postgres_dsn: str
) -> None:
    if not pgvector_available(postgres_dsn):
        pytest.skip("pgvector is not installed on this PostgreSQL server")
    _put_summary(pg_control_path, "bad", body="short vector")
    _put_summary(pg_control_path, "good", body="a full vector")
    _enqueue(pg_control_path, "bad")
    _enqueue(pg_control_path, "good")
    worker = EmbedWorker(duckdb_path=pg_control_path, embedder=_StubEmbedder())

    assert worker.drain_batch() == 2
    store = EmbeddingStore(pg_control_path, model=DEFAULT_EMBED_MODEL)
    assert store.embedded_session_ids(["good", "bad"]) == {"good"}
    assert _job(pg_control_path, "bad").status == "quarantined"
    assert _job(pg_control_path, "good").status == "succeeded"
