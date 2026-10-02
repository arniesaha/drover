"""Hub recall keeps its public envelope and bounds returned context."""

from datetime import datetime, timezone

import pytest

from drover.server import recall_bundle as module
from drover.server.recall_bundle import RecallBundleService


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module,
        "drover_search",
        lambda **kw: {
            "results": [
                {
                    "id": "event",
                    "session_id": "session",
                    "content": "recall " * 500,
                    "agent_id": "codex",
                    "event_type": "user_message",
                }
            ]
        },
    )
    monkeypatch.setattr(
        module, "drover_project_brief", lambda **kw: {"brief_md": "brief"}
    )
    monkeypatch.setattr(
        module,
        "drover_recent_sessions",
        lambda **kw: {"sessions": [{"session_id": "session", "summary_md": "summary"}]},
    )
    monkeypatch.setattr(
        module,
        "drover_open_loops",
        lambda **kw: {"open_loops": [{"context_id": "loop", "next_action": "next"}]},
    )
    return RecallBundleService(
        duckdb_path=tmp_path / "hub.duckdb",
        clock=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_hub_bundle_preserves_envelope_and_scoped_context(service):
    bundle = service.recall_bundle("  recall   question ", repo="owner/repo")
    assert list(bundle) == [
        "query",
        "archive",
        "archive_evidence",
        "drover_context",
        "limits",
        "sources",
    ]
    assert bundle["sources"] == ["hub"]
    assert bundle["query"]["text"] == "recall question"
    assert bundle["archive"]["status"] == "removed"
    assert bundle["archive_evidence"] == []
    context = bundle["drover_context"]
    assert context["keyword_matches"][0]["source_identifiers"]["event_id"] == "event"
    assert context["exact_session_summaries"] == []
    assert context["project_brief"]["text"] == "brief"
    assert context["repository_recent_summaries"][0]["text"] == "summary"
    assert context["repository_open_loops"][0]["text"] == "next"


def test_hub_bundle_character_budget(service):
    bundle = service.recall_bundle("recall", repo="owner/repo", max_context_chars=1000)
    assert bundle["limits"]["used_chars"] == 1000
    assert bundle["limits"]["truncated"] is True
    assert bundle["drover_context"]["keyword_matches"][0]["truncated"] is True


def test_unscoped_hub_bundle_has_no_repository_context(service):
    context = service.recall_bundle("recall")["drover_context"]
    assert context["project_brief"] is None
    assert context["repository_recent_summaries"] == []
    assert context["repository_open_loops"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " "},
        {"query": "recall", "since": "2026-02-30"},
        {"query": "recall", "limit": True},
        {"query": "recall", "limit": 21},
        {"query": "recall", "max_context_chars": 999},
        {"query": "recall", "repo": 1},
    ],
)
def test_hub_bundle_rejects_invalid_inputs(service, arguments):
    with pytest.raises(ValueError):
        service.recall_bundle(**arguments)
