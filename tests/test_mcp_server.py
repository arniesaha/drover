"""Smoke tests for the FastMCP server registration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.mcp.server import NEXUS_TOOL_ALIASES, build_mcp_server


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


def test_legacy_nexus_aliases_resolve(tmp_path: Path) -> None:
    """Compat contract: nexus_* names stay callable for one release but are
    not listed, so the tool surface shown to agents is not doubled
    (docs/porting-and-cutover.md §3)."""
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)

    server = build_mcp_server(duckdb_path=duckdb_path)
    listed = {t.name for t in asyncio.run(server.list_tools())}
    # Every legacy alias points at a listed drover_* tool, and covers all of them.
    assert set(NEXUS_TOOL_ALIASES.values()) == listed
    assert not any(name.startswith("nexus_") for name in listed)
    # An aliased call dispatches to the underlying tool instead of erroring.
    result = asyncio.run(server.call_tool("nexus_fleet_status", {}))
    assert result is not None


def test_each_tool_has_a_description(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    server = build_mcp_server(duckdb_path=duckdb_path)
    tools = asyncio.run(server.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 5, f"{t.name} missing description"
