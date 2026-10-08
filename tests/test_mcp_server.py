"""Smoke tests for the FastMCP server registration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.mcp.server import build_mcp_server


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
        "drover_profile",
        "drover_profile_propose",
        "drover_memory_acceptance",
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
        # Provider capacity and quota with routing hint:
        "drover_provider_quota",
    }
    assert server.settings.host == "127.0.0.1"

    recall_tool = next(tool for tool in tools if tool.name == "drover_recall_bundle")
    recall_description = recall_tool.description.lower()
    assert "bounded hub recall" in recall_description
    assert "scoped drover context" in recall_description
    assert "yyyy-mm-dd" in recall_description
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


def test_recall_bundle_invocation_returns_the_public_hub_bundle(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(
        duckdb_path=duckdb_path,
    )

    result = _call_registered_tool(
        server,
        "drover_recall_bundle",
        {
            "query": "bounded recall",
            "repo": "arniesaha/drover",
            "since": "2026-08-01",
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
        "sources",
        "store",
        "store_authoritative",
        "host",
        "data_watermark",
        "truncated",
    ]
    assert result["sources"] == ["hub"]
    assert result["store"] == "local"
    assert result["store_authoritative"] is False
    assert result["archive"]["status"] == "removed"
    assert result["limits"]["effective_limit"] == 1
    assert result["limits"]["effective_max_context_chars"] == 1_500


def test_recall_bundle_returns_hub_context(
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
        "sources",
        "store",
        "store_authoritative",
        "host",
        "data_watermark",
        "truncated",
    ]
    assert result["sources"] == ["hub"]
    assert result["store"] == "local"
    assert result["store_authoritative"] is False
    assert result["archive"]["status"] == "removed"
    assert result["archive_evidence"] == []
    assert result["limits"]["effective_limit"] == 5
    assert result["limits"]["effective_max_context_chars"] == 24_000
    assert result["limits"]["used_chars"] <= 24_000


def test_provider_quota_registered_tool(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(duckdb_path=duckdb_path)

    result = _call_registered_tool(server, "drover_provider_quota", {})

    assert "accounts" in result
    assert "routing_hint" in result
    assert result["routing_hint"] == "no provider accounts configured"
    assert result["store"] == "local"
    assert result["store_authoritative"] is False
    assert "data_watermark" in result


def test_provider_quota_registered_hub_preserves_source_identity(
    pg_control_path, tmp_path
):
    from datetime import datetime, timezone

    from drover.server.providers.service import ProviderUsageService
    from drover.server.providers.types import (
        ProviderAccountSnapshot,
        ProviderUsageWindow,
    )

    path = pg_control_path
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=path)
    observed = datetime.now(timezone.utc).replace(microsecond=0)
    service = ProviderUsageService(duckdb_path=path, parquet_dir=parquet)
    service._persist_new_snapshots(
        (
            ProviderAccountSnapshot(
                snapshot_id="hub-snapshot",
                dedup_key="hub-snapshot",
                provider="codex",
                account_label="Test subscription",
                plan_label="Test",
                host_id="test-host",
                status="ok",
                observed_at=observed,
                windows=(ProviderUsageWindow(kind="primary", used_percent=25),),
                source="provider_api",
            ),
        ),
        host_id="test-host",
    )
    server = build_mcp_server(duckdb_path=path)
    result = _call_registered_tool(server, "drover_provider_quota", {})
    assert result["accounts"][0]["provider"] == "codex"
    assert result["accounts"][0]["hosts"] == ["test-host"]
    assert result["store"] == "hub"
    assert result["store_authoritative"] is True
    assert result["data_watermark"] == {
        "timestamp": observed.isoformat(),
        "basis": "control_state_updated_at",
    }
