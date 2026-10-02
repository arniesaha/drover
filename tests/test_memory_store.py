"""Session memory, briefs and pgvector embeddings in PostgreSQL (#480)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import pgvector_available

from drover.server.db import control_plane_connection
from drover.server.memory_store import (
    EmbeddingMismatch,
    EmbeddingStore,
    MemoryRepository,
    ProjectBrief,
    SessionSummary,
    VectorStoreUnavailable,
    vector_status,
)


def _summary(session_id: str, **kw) -> SessionSummary:
    base = dict(
        summary_md=f"did {session_id}",
        next_steps_md="ship it",
        project_key="o/r",
        ended_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        files_touched=("a.py",),
        tools_used={"Edit": 2},
        open_questions=("why?",),
        source_version="v1",
        generator_model="m",
    )
    base.update(kw)
    return SessionSummary(session_id=session_id, **base)


def test_recap_then_summary_are_phases_of_one_row(pg_control_path):
    repo = MemoryRepository(pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        assert repo.put_recap(con, "s1", "working on it", 5, "fast")
        assert not repo.put_recap(con, "s1", "stale", 3, "fast")
    latest = repo.latest(["s1"])["s1"]
    assert latest.phase == "live" and latest.text == "working on it"
    assert repo.summary("s1") is None

    with control_plane_connection(pg_control_path) as con:
        repo.put_summary(con, _summary("s1"))
    latest = repo.latest(["s1"])["s1"]
    assert latest.phase == "final" and latest.text == "did s1"
    assert latest.recap is not None and latest.recap.source_seq == 5
    summary = repo.summary("s1")
    assert summary.tools_used == {"Edit": 2} and summary.files_touched == ("a.py",)
    assert summary.as_dict()["open_questions"] == ["why?"]
    assert repo.live_recaps(["s1", "missing"]).keys() == {"s1"}


def test_recent_and_search_and_counts(pg_control_path):
    repo = MemoryRepository(pg_control_path)
    with control_plane_connection(pg_control_path) as con:
        repo.put_summary(
            con, _summary("old", ended_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        )
        repo.put_summary(con, _summary("new", summary_md="fixed 100% of the bug"))
        repo.put_summary(con, _summary("other", project_key="x/y", next_steps_md=None))
        repo.put_recap(con, "live-only", "hi", 1, None)
    assert [s.session_id for s in repo.recent_summaries(project_key="o/r")] == [
        "new",
        "old",
    ]
    assert [s.session_id for s in repo.search_summaries("100%")] == ["new"]
    assert repo.counts() == {
        "session_summaries": 3,
        "live_recaps": 1,
        "bundle_ready_summaries": 2,
        "project_briefs": 0,
    }


def test_canonical_harness_summary_hides_explicit_native_duplicate(pg_control_path):
    repo = MemoryRepository(pg_control_path)
    with repo.connection() as con:
        con.execute("""INSERT INTO harness_sessions
            (session_id, host_id, harness, command, native_session_id, status)
            VALUES ('harness', 'host', 'codex', 'codex', 'native', 'exited')""")
        repo.put_summary(con, _summary("native", summary_md="native memory"))
    assert [s.session_id for s in repo.recent_summaries()] == ["native"]
    with repo.connection() as con:
        repo.put_summary(con, _summary("harness", summary_md="canonical memory"))
    assert [s.session_id for s in repo.recent_summaries()] == ["harness"]
    assert [s.session_id for s in repo.search_summaries("memory")] == ["harness"]


def test_non_numeric_and_zero_vectors_are_explicit_input_errors(pg_control_path):
    store = EmbeddingStore(pg_control_path, model="m")
    for vector in ([0.0] * 768, ["bad"] * 768, [float("nan")] * 768):
        with pytest.raises(EmbeddingMismatch):
            store.search(vector)


def test_brief_round_trip(pg_control_path):
    repo = MemoryRepository(pg_control_path)
    brief = ProjectBrief(
        project_key="o/r",
        repo_owner="o",
        repo_name="r",
        brief_md="b",
        key_files=("a.py",),
        open_questions=("q",),
        session_count=3,
    )
    with control_plane_connection(pg_control_path) as con:
        repo.put_brief(con, brief)
    stored = repo.brief("o/r")
    assert stored.key_files == ("a.py",) and stored.session_count == 3
    assert stored.generated_at is not None
    assert [b.project_key for b in repo.briefs()] == ["o/r"]


def test_embedding_space_is_validated_before_any_io(pg_control_path):
    with pytest.raises(EmbeddingMismatch, match="768"):
        EmbeddingStore(pg_control_path, model="m", dim=1024)
    store = EmbeddingStore(pg_control_path, model="nomic-embed-text")
    with pytest.raises(EmbeddingMismatch, match="dimensions"):
        store.search([0.1] * 10)
    with pytest.raises(EmbeddingMismatch, match="does not match"):
        store.search([0.1] * 768, model="other-model")


def test_missing_pgvector_is_explicit(pg_control_path, postgres_dsn):
    if pgvector_available(postgres_dsn):
        pytest.skip("pgvector is installed on this server")
    store = EmbeddingStore(pg_control_path, model="nomic-embed-text")
    ready, detail = store.status()
    assert not ready and "pgvector is not installed" in detail
    with pytest.raises(VectorStoreUnavailable, match="pgvector"):
        store.search([0.1] * 768)
    with control_plane_connection(pg_control_path) as con:
        with pytest.raises(VectorStoreUnavailable):
            store.put(con, "s", [0.1] * 768, model="nomic-embed-text")
    assert store.count() == {"embedded": 0, "other_model": 0}


def test_exact_cosine_search_with_pgvector(pg_control_path, postgres_dsn):
    if not pgvector_available(postgres_dsn):
        pytest.skip("pgvector is not installed on this PostgreSQL server")
    store = EmbeddingStore(pg_control_path, model="nomic-embed-text")
    with control_plane_connection(pg_control_path) as con:
        assert vector_status(con)[0]
        MemoryRepository.put_summary(con, _summary("near", source_version="v1"))
        MemoryRepository.put_summary(con, _summary("far", source_version=""))
        store.put(
            con,
            "near",
            [1.0] + [0.0] * 767,
            model="nomic-embed-text",
            source_version="v1",
        )
        store.put(con, "far", [0.0, 1.0] + [0.0] * 766, model="nomic-embed-text")
        with pytest.raises(EmbeddingMismatch):
            store.put(con, "bad", [1.0] * 767, model="nomic-embed-text")
    hits = store.search([0.9, 0.1] + [0.0] * 766, limit=2)
    assert [h.session_id for h in hits] == ["near", "far"]
    assert hits[0].similarity > hits[1].similarity
    assert store.count() == {"embedded": 2, "other_model": 0}
    assert store.embedded_session_ids(["near", "x"]) == {"near"}


def test_semantic_search_excludes_stale_generation(pg_control_path, postgres_dsn):
    if not pgvector_available(postgres_dsn):
        pytest.skip("pgvector is not installed on this PostgreSQL server")
    repo = MemoryRepository(pg_control_path)
    store = EmbeddingStore(pg_control_path, model="m")
    with repo.connection() as con:
        repo.put_summary(con, _summary("s", source_version="v1"))
        store.put(con, "s", [1.0] + [0.0] * 767, model="m", source_version="v1")
    assert store.search([1.0] + [0.0] * 767)
    with repo.connection() as con:
        repo.put_summary(con, _summary("s", source_version="v2"))
    assert store.search([1.0] + [0.0] * 767) == []
    assert store.embedded_session_ids(["s"]) == set()
