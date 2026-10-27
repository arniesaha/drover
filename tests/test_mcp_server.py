"""Smoke tests for the FastMCP server registration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.mcp.server import build_mcp_server

pytestmark = pytest.mark.usefixtures("authenticated_mcp_builder")


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
        "drover_work_list",
        "drover_work_get",
        "drover_continuity_status",
        "drover_work_launch",
        "drover_work_link",
        "drover_work_update",
        "drover_work_question",
        "drover_work_decide",
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


@pytest.mark.parametrize("connection_kind", ["direct", "relay", "relay_disconnected"])
def test_provider_quota_fresh_uses_hub_transport(
    tmp_path, monkeypatch, connection_kind
):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock

    from drover.server.harness.registry import HarnessRegistry
    from drover.server.metrics import MetricsCollector
    from drover.server.providers.service import ProviderUsageService

    path = tmp_path / "nexus.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=path)
    HarnessRegistry(path).register_host(
        host_id="quota-host",
        display_name="Quota host",
        kind="mac",
        connection_kind="direct" if connection_kind == "direct" else "relay",
        local_url="http://127.0.0.1:7081" if connection_kind == "direct" else "",
    )
    now = datetime.now(timezone.utc)
    observed = now - timedelta(minutes=20)
    payload = {
        "accounts": [
            {
                "snapshot_id": "quota-good",
                "dedup_key": "quota-good",
                "provider": "openai",
                "account_label": "Synthetic subscription",
                "host_id": "quota-host",
                "status": "ok",
                "usage_status": "supported",
                "observed_at": observed.isoformat(),
                "source": "codex-app-server",
                "windows": [{"kind": "primary", "used_percent": 25}],
            }
        ]
    }
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        api_token="synthetic-host-token",
        summarizer_report={},
    )
    connection = Mock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = json.dumps(payload).encode()
    dial = Mock(return_value=connection)
    monkeypatch.setattr("drover.server.metrics.http.client.HTTPConnection", dial)
    relay = Mock()
    relay.is_live.return_value = connection_kind != "relay_disconnected"
    relay.request.return_value = (200, json.dumps(payload))
    if connection_kind != "direct":
        collector.relay_manager = relay
    service = ProviderUsageService(
        path, parquet, freshness_threshold_seconds=3600, clock=lambda: now
    )
    if connection_kind == "relay_disconnected":
        host = HarnessRegistry(path).get_host("quota-host")
        service.refresh_host(host, fetch=lambda host: payload)
        before = service.latest_accounts()
    server = build_mcp_server(
        duckdb_path=path,
        provider_usage=service,
        provider_fetch=collector.fetch_harness_provider_usage,
    )
    result = _call_registered_tool(server, "drover_provider_quota", {"fresh": True})
    if connection_kind == "relay_disconnected":
        assert result["refresh_errors"] == [
            {"host_id": "quota-host", "error_category": "unavailable"}
        ]
        assert service.latest_accounts() == before
        dial.assert_not_called()
        relay.request.assert_not_called()
        assert result["accounts"][0]["status"] == "ok"
        return
    assert result["refresh_errors"] == []
    assert result["accounts"][0]["status"] == "ok"
    assert result["accounts"][0]["windows"][0]["used_percent"] == 25
    if connection_kind == "relay":
        dial.assert_not_called()
        relay.request.assert_called_once_with(
            "quota-host",
            "GET",
            "/providers/usage",
            {},
            timeout_s=10.0,
        )
    else:
        relay.request.assert_not_called()
        dial.assert_called_once_with("127.0.0.1", 7081, timeout=10.0)
        connection.request.assert_called_once_with(
            "GET",
            "/providers/usage",
            body=None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer synthetic-host-token",
            },
        )
