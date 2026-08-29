"""Composition and character-budget tests for the recall bundle service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from drover.config import ArchiveConfig
from drover.schema import bootstrap
from drover.server.archive import (
    ArchiveMessage,
    ArchiveMessageNeighborhood,
    ArchiveMessageRequest,
    ArchivePartSummary,
    ArchiveSearchHit,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSession,
)
from drover.server.recall_bundle import RecallBundleService

RETRIEVED_AT = datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc)


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    return parquet_dir, duckdb_path


def _archive_config(
    *, search_limit: int = 3, max_context_chars: int = 20_000
) -> ArchiveConfig:
    return ArchiveConfig(
        enabled=True,
        base_url="http://127.0.0.1:8585",
        timeout_seconds=3.0,
        search_limit=search_limit,
        context_before=2,
        context_after=2,
        max_context_chars=max_context_chars,
        max_response_bytes=1_048_576,
    )


def _hit(
    *,
    rank: int,
    message_id: str,
    session_id: str,
    timestamp: str,
    text: str,
) -> ArchiveSearchHit:
    return ArchiveSearchHit(
        rank=rank,
        message_id=message_id,
        session_id=session_id,
        project="arniesaha/drover",
        source_agent="codex",
        role="user",
        timestamp=timestamp,
        text=text,
        score=1.0 / rank,
        parts_summary=(ArchivePartSummary(kind="file", label=f"rank-{rank}.py"),),
    )


def _neighborhood(
    hit: ArchiveSearchHit, *, sibling_text: str
) -> ArchiveMessageNeighborhood:
    return ArchiveMessageNeighborhood(
        session=ArchiveSession(
            session_id=hit.session_id,
            project=hit.project,
            source_agent=hit.source_agent,
            created_at="2026-08-20T09:00:00Z",
            parent_session_id=None,
            parent_message_id=None,
        ),
        target=ArchiveMessage(
            message_id=hit.message_id,
            session_id=hit.session_id,
            project=hit.project,
            source_agent=hit.source_agent,
            role=hit.role,
            timestamp=hit.timestamp,
            text=None,
            parts=(),
        ),
        siblings=(
            ArchiveMessage(
                message_id=f"{hit.message_id}-sibling",
                session_id=hit.session_id,
                project=hit.project,
                source_agent=hit.source_agent,
                role="assistant",
                timestamp="2026-08-20T09:01:00Z",
                text=sibling_text,
                parts=(
                    ArchivePartSummary(
                        kind="tool_call", label="Read", call_id=f"call-{hit.rank}"
                    ),
                ),
            ),
        ),
        target_part_count=2,
        target_parts_remaining=1,
        context_before=2,
        context_after=2,
    )


@dataclass
class StrictArchive:
    result: ArchiveSearchResult
    neighborhoods: dict[str, ArchiveMessageNeighborhood]
    expected_query: str | None = None
    expected_project: str | None = None
    expected_since: str | None = None
    expected_limit: int | None = None
    search_requests: list[ArchiveSearchRequest] = field(default_factory=list)
    message_requests: list[ArchiveMessageRequest] = field(default_factory=list)
    _hydrating: bool = False

    def search(self, request: ArchiveSearchRequest) -> ArchiveSearchResult:
        assert request == ArchiveSearchRequest(
            query=self.expected_query or request.query,
            project=self.expected_project,
            since=self.expected_since,
            limit=self.expected_limit or request.limit,
        )
        self.search_requests.append(request)
        return self.result

    def get_message(self, request: ArchiveMessageRequest) -> ArchiveMessageNeighborhood:
        assert not self._hydrating, "archive hydrations overlapped"
        assert request.context_before == 2
        assert request.context_after == 2
        assert request.message_id in self.neighborhoods
        self._hydrating = True
        try:
            self.message_requests.append(request)
            return self.neighborhoods[request.message_id]
        finally:
            self._hydrating = False


def _empty_archive(**expectations: object) -> StrictArchive:
    return StrictArchive(
        result=ArchiveSearchResult(
            hits=(), matched_total=0, searchable_in_scope=0, has_more=False
        ),
        neighborhoods={},
        **expectations,
    )


def _service(
    duckdb_path: Path,
    archive: StrictArchive,
    *,
    search_limit: int = 3,
    max_context_chars: int = 20_000,
) -> RecallBundleService:
    return RecallBundleService(
        duckdb_path=duckdb_path,
        archive_config=_archive_config(
            search_limit=search_limit, max_context_chars=max_context_chars
        ),
        archive=archive,
        clock=lambda: RETRIEVED_AT,
    )


@pytest.mark.parametrize("query", ["", " ", "\n\t", True, None])
def test_validation_rejects_blank_or_non_string_queries(
    tmp_path: Path, query: object
) -> None:
    _, duckdb_path = _seed(tmp_path)
    service = _service(duckdb_path, _empty_archive())

    with pytest.raises(ValueError, match="query"):
        service.recall_bundle(query=query)  # type: ignore[arg-type]


@pytest.mark.parametrize("since", ["", "yesterday", "2026-99-99", True, 42])
def test_validation_rejects_invalid_iso_8601_since(
    tmp_path: Path, since: object
) -> None:
    _, duckdb_path = _seed(tmp_path)
    service = _service(duckdb_path, _empty_archive())

    with pytest.raises(ValueError, match="since"):
        service.recall_bundle(query="retry", since=since)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [True, 1.0, 0, 21])
def test_validation_rejects_non_integer_or_out_of_range_limits(
    tmp_path: Path, limit: object
) -> None:
    _, duckdb_path = _seed(tmp_path)
    service = _service(duckdb_path, _empty_archive())

    with pytest.raises(ValueError, match="limit"):
        service.recall_bundle(query="retry", limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("budget", [True, 1000.0, 999, 100_001])
def test_validation_rejects_non_integer_or_out_of_range_context_budgets(
    tmp_path: Path, budget: object
) -> None:
    _, duckdb_path = _seed(tmp_path)
    service = _service(duckdb_path, _empty_archive())

    with pytest.raises(ValueError, match="max_context_chars"):
        service.recall_bundle(
            query="retry", max_context_chars=budget  # type: ignore[arg-type]
        )


def test_validation_normalizes_query_and_reports_requested_and_effective_limits(
    tmp_path: Path,
) -> None:
    _, duckdb_path = _seed(tmp_path)
    archive = _empty_archive(
        expected_query="retry state machine",
        expected_project=None,
        expected_since="2026-08-01T00:00:00Z",
        expected_limit=3,
    )
    service = _service(duckdb_path, archive, search_limit=3, max_context_chars=1_500)

    bundle = service.recall_bundle(
        query="  retry\n state\t machine  ",
        since="2026-08-01T00:00:00Z",
        limit=20,
        max_context_chars=100_000,
    )

    assert bundle["query"] == {
        "text": "retry state machine",
        "repo": None,
        "since": "2026-08-01T00:00:00Z",
    }
    assert bundle["limits"] == {
        "requested_limit": 20,
        "effective_limit": 3,
        "requested_max_context_chars": 100_000,
        "effective_max_context_chars": 1_500,
        "used_chars": 0,
        "truncated": False,
        "dropped": {
            "archive_neighborhoods": 0,
            "repository_open_loops": 0,
            "repository_recent_summaries": 0,
            "project_brief": 0,
            "drover_keyword_matches": 0,
            "exact_session_summaries": 0,
        },
    }


def _write_agent_events(parquet_dir: Path) -> None:
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
        {
            "id": "event-linked",
            "session_id": "pond-session-exact",
            "agent_id": "codex",
            "task_id": "task-drover",
            "timestamp": datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
            "event_type": "assistant_message",
            "role": "assistant",
            "content": "Retry state machine linked event",
            "repo_owner": "arniesaha",
            "repo_name": "drover",
            "branch": "main",
            "principal_id": "operator",
            "dedup_key": "dedup-linked",
            "raw_data": "{}",
        },
        {
            "id": "event-independent",
            "session_id": "drover-session-independent",
            "agent_id": "claude",
            "task_id": "task-drover",
            "timestamp": datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc),
            "event_type": "user_message",
            "role": "user",
            "content": "Retry state machine independent event",
            "repo_owner": "arniesaha",
            "repo_name": "drover",
            "branch": "main",
            "principal_id": "operator",
            "dedup_key": "dedup-independent",
            "raw_data": "{}",
        },
    ]
    table = pa.table(
        {
            column.name: pa.array([row[column.name] for row in rows], type=column.type)
            for column in schema
        },
        schema=schema,
    )
    out = parquet_dir / "agent_events" / "date=2026-08-27" / "agent_id=test"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / "part-recall.parquet")


def _seed_composition_rows(parquet_dir: Path, duckdb_path: Path) -> None:
    _write_agent_events(parquet_dir)
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("""INSERT INTO tasks
               (task_id, repo_owner, repo_name, branch, principal_id, status,
                created_at, last_activity_at, session_count, total_cost_usd)
               VALUES ('task-drover', 'arniesaha', 'drover', 'main', 'operator',
                       'open', '2026-08-20', '2026-08-27', 2, 0.0)""")
        con.execute("""INSERT INTO session_summaries
               (session_id, task_id, agent_id, ended_at, summary_md,
                files_touched, tools_used, last_user_prompt, last_assistant,
                next_steps_md, open_questions, status, generator_model, generated_at)
               VALUES
               ('pond-session-exact', 'task-drover', 'codex',
                '2026-08-27 13:00:00', 'Exact linked session summary.',
                ['src/drover/server/recall_bundle.py'], MAP{}, '', '',
                'Keep the exact join.', ['Does the citation remain?'],
                'completed', 'test-model', '2026-08-27 13:05:00'),
               ('repo-session-recent', 'task-drover', 'claude',
                '2026-08-27 12:30:00', 'Repository recent summary.',
                [], MAP{}, '', '', 'Review the bundle.', [],
                'completed', 'test-model', '2026-08-27 12:35:00')""")
        con.execute("""INSERT INTO project_briefs
               (project_key, repo_owner, repo_name, brief_md, recent_themes_md,
                key_files, open_questions, next_steps_md, session_count,
                last_activity_at, generator_model, generated_at)
               VALUES ('arniesaha/drover', 'arniesaha', 'drover',
                       'Drover coordinates native agent harnesses.',
                       'Bounded recall is the current theme.',
                       ['src/drover/server/recall_bundle.py'],
                       ['How much archive context is enough?'],
                       'Run deterministic budget tests.', 2,
                       '2026-08-27 13:00:00', 'test-model',
                       '2026-08-27 13:05:00')""")
        con.execute("""INSERT INTO context_containers
               (context_id, container_type, label, source_harness, confidence,
                evidence, last_touched_at, next_action, open_loop, session_ids,
                task_ids, repo_owner, repo_name, branch, summary_md,
                redaction_policy, created_at, updated_at)
               VALUES ('ctx-drover-recall', 'code_project', 'Recall pilot',
                       'codex', 0.95, 'Repository-scoped evidence.',
                       '2026-08-27 14:00:00+00', 'Complete Task 5.',
                       'Budget review remains open.', ['pond-session-exact'],
                       ['task-drover'], 'arniesaha', 'drover', 'main',
                       'Recall bundle work is active.', 'session-summary-redacted',
                       '2026-08-27 10:00:00+00', '2026-08-27 14:05:00+00')""")
    finally:
        con.close()


def _composition_archive() -> StrictArchive:
    hits = (
        _hit(
            rank=1,
            message_id="pond-message-1",
            session_id="pond-session-exact",
            timestamp="2026-08-27T15:00:00Z",
            text="Selected search-hit target one.",
        ),
        _hit(
            rank=2,
            message_id="pond-message-1",
            session_id="pond-session-exact",
            timestamp="2026-08-27T15:00:00Z",
            text="Duplicate target that must not replace rank one.",
        ),
        _hit(
            rank=3,
            message_id="pond-message-2",
            session_id="pond-session-missing",
            timestamp="2026-08-27T16:00:00+00:00",
            text="Selected search-hit target two.",
        ),
        _hit(
            rank=4,
            message_id="pond-message-3",
            session_id="pond-session-missing",
            timestamp="2026-08-27T14:00:00-07:00",
            text="Selected search-hit target three.",
        ),
    )
    return StrictArchive(
        result=ArchiveSearchResult(
            hits=hits, matched_total=8, searchable_in_scope=80, has_more=True
        ),
        neighborhoods={
            hit.message_id: _neighborhood(hit, sibling_text=f"Sibling for {hit.rank}.")
            for hit in (hits[0], hits[2], hits[3])
        },
        expected_query="retry state machine",
        expected_project="arniesaha/drover",
        expected_since="2026-08-01T00:00:00Z",
        expected_limit=3,
    )


def _content_items(bundle: dict) -> list[dict]:
    archive_items = [
        content
        for neighborhood in bundle["archive_evidence"]
        for content in [neighborhood, *neighborhood["siblings"]]
    ]
    drover = bundle["drover_context"]
    project_brief = [drover["project_brief"]] if drover["project_brief"] else []
    return [
        *archive_items,
        *drover["keyword_matches"],
        *drover["exact_session_summaries"],
        *project_brief,
        *drover["repository_recent_summaries"],
        *drover["repository_open_loops"],
    ]


def _assert_complete_citation(item: dict) -> None:
    assert item["source_type"]
    assert item["source_identifiers"]
    assert item["source_timestamp"]
    assert item["retrieval_timestamp"] == "2026-08-28T19:30:00+00:00"
    assert item["join_basis"]
    assert item["truncated"] is False


def test_composition_preserves_rank_exact_joins_and_self_contained_citations(
    tmp_path: Path,
) -> None:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _seed_composition_rows(parquet_dir, duckdb_path)
    archive = _composition_archive()
    service = _service(duckdb_path, archive)

    bundle = service.recall_bundle(
        query="retry state machine",
        repo="arniesaha/drover",
        since="2026-08-01T00:00:00Z",
        limit=3,
    )

    assert list(bundle) == [
        "query",
        "archive",
        "archive_evidence",
        "drover_context",
        "limits",
    ]
    assert [item["rank"] for item in bundle["archive_evidence"]] == [1, 3, 4]
    assert [request.message_id for request in archive.message_requests] == [
        "pond-message-1",
        "pond-message-2",
        "pond-message-3",
    ]
    first = bundle["archive_evidence"][0]
    assert first["text"] == "Selected search-hit target one."
    assert first["source_type"] == "pond_message"
    assert first["join_basis"] == "archive_rank"
    assert first["siblings"][0]["text"] == "Sibling for 1."
    assert "parts" not in first
    assert "target" not in first

    archive_metadata = dict(bundle["archive"])
    search_latency_ms = archive_metadata.pop("search_latency_ms")
    assert type(search_latency_ms) is int and search_latency_ms >= 0
    assert archive_metadata == {
        "status": "available",
        "matched_total": 8,
        "searchable_in_scope": 80,
        "has_more": True,
        "selected_count": 3,
        "hydrated_count": 3,
        "retained_count": 3,
        "result_set_freshness": "2026-08-27T14:00:00-07:00",
        "retrieval_timestamp": "2026-08-28T19:30:00+00:00",
    }
    assert "archive_freshness" not in bundle["archive"]

    context = bundle["drover_context"]
    assert [
        item["source_identifiers"]["session_id"]
        for item in context["exact_session_summaries"]
    ] == ["pond-session-exact"]
    assert context["exact_session_summaries"][0]["join_basis"] == "exact_session_id"
    assert context["project_brief"]["join_basis"] == "caller_repo_scope"
    assert context["project_brief"]["source_agent"] is None
    assert all(
        item["join_basis"] == "caller_repo_scope"
        for item in context["repository_recent_summaries"]
    )
    assert all(
        item["join_basis"] == "caller_repo_scope"
        for item in context["repository_open_loops"]
    )

    keyword_by_session = {
        item["source_identifiers"]["session_id"]: item
        for item in context["keyword_matches"]
    }
    assert keyword_by_session["pond-session-exact"]["join_basis"] == "exact_session_id"
    assert (
        keyword_by_session["drover-session-independent"]["join_basis"]
        == "drover_keyword_match"
    )
    assert all(
        item["join_basis"] != "exact_session_id"
        for item in _content_items(bundle)
        if item["source_identifiers"].get("session_id") == "drover-session-independent"
    )
    for item in _content_items(bundle):
        _assert_complete_citation(item)
        assert "score" not in item

    expected_used = sum(len(item["text"]) for item in _content_items(bundle))
    assert bundle["limits"]["used_chars"] == expected_used
    assert bundle["limits"]["used_chars"] <= 20_000
    json.dumps(bundle)


def test_composition_passes_non_exact_repo_only_to_archive_and_keyword_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, duckdb_path = _seed(tmp_path)
    archive = _empty_archive(
        expected_query="retry",
        expected_project="drover",
        expected_since=None,
        expected_limit=3,
    )
    service = _service(duckdb_path, archive)

    def forbidden_repo_supplement(**_: object) -> dict:
        raise AssertionError("repository-only supplement must not run")

    monkeypatch.setattr(
        "drover.server.recall_bundle.drover_project_brief",
        forbidden_repo_supplement,
    )
    monkeypatch.setattr(
        "drover.server.recall_bundle.drover_recent_sessions",
        forbidden_repo_supplement,
    )
    monkeypatch.setattr(
        "drover.server.recall_bundle.drover_open_loops",
        forbidden_repo_supplement,
    )

    bundle = service.recall_bundle(query="retry", repo="drover")

    assert bundle["query"]["repo"] == "drover"
    assert bundle["drover_context"] == {
        "keyword_matches": [],
        "exact_session_summaries": [],
        "project_brief": None,
        "repository_recent_summaries": [],
        "repository_open_loops": [],
    }


def _budget_service(tmp_path: Path, *, exact_summary_text: str) -> RecallBundleService:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _seed_composition_rows(parquet_dir, duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE session_summaries SET summary_md=?, next_steps_md='', open_questions=[] "
            "WHERE session_id='pond-session-exact'",
            [exact_summary_text],
        )
    finally:
        con.close()
    return _service(duckdb_path, _composition_archive(), max_context_chars=1_200)


def _budget_order_service(tmp_path: Path) -> RecallBundleService:
    parquet_dir, duckdb_path = _seed(tmp_path)
    _seed_composition_rows(parquet_dir, duckdb_path)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(
            "UPDATE session_summaries SET summary_md=?, next_steps_md='', open_questions=[] "
            "WHERE session_id='pond-session-exact'",
            ["E" * 1_000],
        )
        con.execute(
            "UPDATE session_summaries SET summary_md=?, next_steps_md='', open_questions=[] "
            "WHERE session_id='repo-session-recent'",
            ["S" * 200],
        )
        con.execute(
            """UPDATE project_briefs
                  SET brief_md=?, recent_themes_md='', key_files=[],
                      open_questions=[], next_steps_md=''
                WHERE project_key='arniesaha/drover'""",
            ["B" * 200],
        )
        con.execute(
            """UPDATE context_containers
                  SET summary_md='', next_action=?, open_loop='', evidence=''
                WHERE context_id='ctx-drover-recall'""",
            ["L" * 200],
        )
    finally:
        con.close()

    hits = tuple(
        _hit(
            rank=rank,
            message_id=f"budget-message-{rank}",
            session_id=("pond-session-exact" if rank == 1 else "pond-session-missing"),
            timestamp=f"2026-08-27T{10 + rank:02d}:00:00Z",
            text="A" * 100,
        )
        for rank in (1, 3, 4)
    )
    archive = StrictArchive(
        result=ArchiveSearchResult(
            hits=hits, matched_total=3, searchable_in_scope=30, has_more=False
        ),
        neighborhoods={
            hit.message_id: _neighborhood(hit, sibling_text="N" * 100) for hit in hits
        },
        expected_query="retry state machine",
        expected_project="arniesaha/drover",
        expected_since=None,
        expected_limit=3,
    )
    return _service(duckdb_path, archive, max_context_chars=4_000)


@pytest.mark.parametrize(
    (
        "budget",
        "archive_drops",
        "loop_drops",
        "recent_drops",
        "brief_drops",
        "keyword_drops",
    ),
    [
        (3_100, 1, 0, 0, 0, 0),
        (2_669, 3, 0, 0, 0, 0),
        (2_500, 3, 1, 0, 0, 0),
        (2_300, 3, 1, 1, 0, 0),
        (1_200, 3, 1, 2, 1, 0),
        (1_050, 3, 1, 2, 1, 1),
        (1_000, 3, 1, 2, 1, 2),
    ],
)
def test_budget_removal_boundaries_follow_the_required_order(
    tmp_path: Path,
    budget: int,
    archive_drops: int,
    loop_drops: int,
    recent_drops: int,
    brief_drops: int,
    keyword_drops: int,
) -> None:
    service = _budget_order_service(tmp_path)

    bundle = service.recall_bundle(
        query="retry state machine",
        repo="arniesaha/drover",
        max_context_chars=budget,
    )

    assert bundle["limits"]["dropped"] == {
        "archive_neighborhoods": archive_drops,
        "repository_open_loops": loop_drops,
        "repository_recent_summaries": recent_drops,
        "project_brief": brief_drops,
        "drover_keyword_matches": keyword_drops,
        "exact_session_summaries": 0,
    }
    assert bundle["limits"]["used_chars"] <= budget
    for item in _content_items(bundle):
        assert item["source_identifiers"]
        assert item["join_basis"]


def test_budget_drops_optional_evidence_in_the_required_order(
    tmp_path: Path,
) -> None:
    service = _budget_service(tmp_path, exact_summary_text="E" * 1_000)

    bundle = service.recall_bundle(
        query="retry state machine",
        repo="arniesaha/drover",
        since="2026-08-01T00:00:00Z",
        max_context_chars=1_000,
    )

    assert bundle["archive_evidence"] == []
    assert bundle["drover_context"]["repository_open_loops"] == []
    assert bundle["drover_context"]["repository_recent_summaries"] == []
    assert bundle["drover_context"]["project_brief"] is None
    assert bundle["drover_context"]["keyword_matches"] == []
    assert len(bundle["drover_context"]["exact_session_summaries"]) == 1
    surviving = bundle["drover_context"]["exact_session_summaries"][0]
    assert surviving["text"] == "E" * 1_000
    assert surviving["truncated"] is False
    assert surviving["source_identifiers"]["session_id"] == "pond-session-exact"
    assert surviving["join_basis"] == "exact_session_id"
    assert bundle["limits"]["dropped"] == {
        "archive_neighborhoods": 3,
        "repository_open_loops": 1,
        "repository_recent_summaries": 2,
        "project_brief": 1,
        "drover_keyword_matches": 2,
        "exact_session_summaries": 0,
    }
    assert bundle["limits"]["used_chars"] == 1_000
    assert (
        bundle["limits"]["used_chars"]
        <= bundle["limits"]["effective_max_context_chars"]
    )


def test_budget_truncates_only_final_highest_priority_text_on_unicode_boundary(
    tmp_path: Path,
) -> None:
    service = _budget_service(tmp_path, exact_summary_text="😀" * 1_100)

    bundle = service.recall_bundle(
        query="retry state machine",
        repo="arniesaha/drover",
        since="2026-08-01T00:00:00Z",
        max_context_chars=1_000,
    )

    items = _content_items(bundle)
    assert len(items) == 1
    surviving = items[0]
    assert surviving["text"] == "😀" * 1_000
    assert len(surviving["text"]) == 1_000
    assert surviving["truncated"] is True
    assert surviving["source_type"] == "session_summary"
    assert surviving["source_identifiers"] == {
        "session_id": "pond-session-exact",
        "task_id": "task-drover",
    }
    assert surviving["source_timestamp"] == "2026-08-27T13:00:00"
    assert surviving["retrieval_timestamp"] == "2026-08-28T19:30:00+00:00"
    assert surviving["join_basis"] == "exact_session_id"
    assert bundle["limits"]["used_chars"] == 1_000
    assert bundle["limits"]["truncated"] is True
    assert (
        bundle["limits"]["used_chars"]
        <= bundle["limits"]["effective_max_context_chars"]
    )


@pytest.mark.parametrize("requested", [1_000, 100_000])
def test_validation_accepts_caller_context_budget_boundaries(
    tmp_path: Path, requested: int
) -> None:
    _, duckdb_path = _seed(tmp_path)
    service = _service(duckdb_path, _empty_archive(), max_context_chars=20_000)

    bundle = service.recall_bundle(query="retry", max_context_chars=requested)

    assert bundle["limits"]["requested_max_context_chars"] == requested
    assert bundle["limits"]["effective_max_context_chars"] == min(requested, 20_000)


@pytest.mark.parametrize("requested", [1, 20])
def test_validation_accepts_caller_limit_boundaries(
    tmp_path: Path, requested: int
) -> None:
    _, duckdb_path = _seed(tmp_path)
    archive = _empty_archive(expected_limit=min(requested, 3))
    service = _service(duckdb_path, archive, search_limit=3)

    bundle = service.recall_bundle(query="retry", limit=requested)

    assert bundle["limits"]["requested_limit"] == requested
    assert bundle["limits"]["effective_limit"] == min(requested, 3)
