"""Smoke tests for the FastMCP server registration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
from drover.server.mcp.server import build_mcp_server


class NormalizedArchive:
    """Return complete Drover-owned values at the archive protocol boundary."""

    def search(self, request: ArchiveSearchRequest) -> ArchiveSearchResult:
        assert request == ArchiveSearchRequest(
            query="bounded recall",
            project="arniesaha/drover",
            since="2026-08-01T00:00:00Z",
            limit=1,
        )
        return ArchiveSearchResult(
            hits=(
                ArchiveSearchHit(
                    rank=1,
                    message_id="pond-message-1",
                    session_id="pond-session-1",
                    project="arniesaha/drover",
                    source_agent="codex",
                    role="user",
                    timestamp="2026-08-20T10:00:00Z",
                    text="Keep recall bounded.",
                    score=0.875,
                    parts_summary=(
                        ArchivePartSummary(kind="file", label="src/recall.py"),
                    ),
                ),
            ),
            matched_total=1,
            searchable_in_scope=12,
            has_more=False,
        )

    def get_message(self, request: ArchiveMessageRequest) -> ArchiveMessageNeighborhood:
        assert request == ArchiveMessageRequest(
            message_id="pond-message-1",
            context_before=1,
            context_after=1,
        )
        return ArchiveMessageNeighborhood(
            session=ArchiveSession(
                session_id="pond-session-1",
                project="arniesaha/drover",
                source_agent="codex",
                created_at="2026-08-20T09:55:00Z",
                parent_session_id=None,
                parent_message_id=None,
            ),
            target=ArchiveMessage(
                message_id="pond-message-1",
                session_id="pond-session-1",
                project="arniesaha/drover",
                source_agent="codex",
                role="user",
                timestamp="2026-08-20T10:00:00Z",
                text=None,
                parts=(),
            ),
            siblings=(
                ArchiveMessage(
                    message_id="pond-message-2",
                    session_id="pond-session-1",
                    project="arniesaha/drover",
                    source_agent="codex",
                    role="assistant",
                    timestamp="2026-08-20T10:01:00Z",
                    text="Use a strict character budget.",
                    parts=(
                        ArchivePartSummary(
                            kind="tool_call", label="Read", call_id="call-1"
                        ),
                    ),
                ),
            ),
            target_part_count=1,
            target_parts_remaining=0,
            context_before=1,
            context_after=1,
        )


def _archive_config() -> ArchiveConfig:
    return ArchiveConfig(
        enabled=True,
        base_url="http://127.0.0.1:8585",
        timeout_seconds=3.0,
        search_limit=1,
        context_before=1,
        context_after=1,
        max_context_chars=2_000,
        max_response_bytes=1_048_576,
    )


def _call_registered_tool(server, name: str, arguments: dict) -> dict:
    content = asyncio.run(server.call_tool(name, arguments))
    assert len(content) == 1
    assert content[0].type == "text"
    return json.loads(content[0].text)


def test_server_registers_all_tools(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    server = build_mcp_server(duckdb_path=duckdb_path)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "drover_handoff",
        "drover_session_replay",
        "drover_session_summary",
        "drover_active_sessions",
        "drover_search",
        "drover_recall_bundle",
        "drover_files_touched",
        "drover_task_status",
        "drover_session_close",
        # New in Tier 1 + Door 1:
        "drover_project_brief",
        "drover_recent_sessions",
        "drover_recall",
        # General context containers beyond repo-first attribution:
        "drover_recent_contexts",
        "drover_context_brief",
        "drover_open_loops",
        "drover_resume_context",
        # Attribution + analytics phase 2:
        "drover_project_activity",
        "drover_fleet_status",
        # Read-only quality self-check for agents:
        "drover_data_quality",
        # Pipeline Observatory saved-artifact/project drilldown:
        "drover_pipeline_observatory",
        # Rolling handoff brief for OPEN sessions:
        "drover_active_handoff",
    }
    assert server.settings.host == "127.0.0.1"

    recall_tool = next(tool for tool in tools if tool.name == "drover_recall_bundle")
    assert recall_tool.description == (
        "Return bounded native-harness archive evidence plus scoped Drover context."
    )
    assert list(recall_tool.inputSchema["properties"]) == [
        "query",
        "repo",
        "since",
        "limit",
        "max_context_chars",
    ]
    assert recall_tool.inputSchema["required"] == ["query"]


def test_each_tool_has_a_description(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(duckdb_path=duckdb_path)
    tools = asyncio.run(server.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 5, f"{t.name} missing description"


def test_recall_bundle_invocation_returns_the_public_five_field_bundle(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(
        duckdb_path=duckdb_path,
        archive_config=_archive_config(),
        archive=NormalizedArchive(),
    )

    result = _call_registered_tool(
        server,
        "drover_recall_bundle",
        {
            "query": "bounded recall",
            "repo": "arniesaha/drover",
            "since": "2026-08-01T00:00:00Z",
            "limit": 1,
            "max_context_chars": 1_500,
        },
    )

    assert list(result) == [
        "query",
        "archive",
        "archive_evidence",
        "drover_context",
        "limits",
    ]
    assert result["archive"]["status"] == "available"
    assert result["archive_evidence"][0]["source_identifiers"] == {
        "message_id": "pond-message-1",
        "session_id": "pond-session-1",
        "project": "arniesaha/drover",
    }
    assert result["limits"]["effective_limit"] == 1
    assert result["limits"]["effective_max_context_chars"] == 1_500


def test_recall_bundle_without_a_client_returns_bounded_disabled_fallback(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(duckdb_path=duckdb_path)

    result = _call_registered_tool(
        server, "drover_recall_bundle", {"query": "local fallback"}
    )

    assert list(result) == [
        "query",
        "archive",
        "archive_evidence",
        "drover_context",
        "limits",
    ]
    assert result["archive"]["status"] == "disabled"
    assert result["archive_evidence"] == []
    assert result["limits"]["effective_limit"] == 5
    assert result["limits"]["effective_max_context_chars"] == 24_000
    assert result["limits"]["used_chars"] <= 24_000
