"""Tests for drover.hook.client — MCP-over-streamable-HTTP wrapper."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from drover.hook.client import HookTimeout, call_tool
from drover.schema import bootstrap
from drover.server.mcp.server import build_mcp_server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path):
    parquet_dir = tmp_path / "parquet"
    duckdb_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=duckdb_path)
    port = _free_port()
    server = build_mcp_server(duckdb_path=duckdb_path, host="127.0.0.1", port=port)

    def _run():
        try:
            server.run(transport="streamable-http")
        except Exception:
            pass

    th = threading.Thread(target=_run, daemon=True)
    th.start()

    # Wait for the port to accept connections (uvicorn is slow to bind)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("MCP server did not start in time")

    yield {"port": port, "duckdb_path": duckdb_path}


def test_call_tool_returns_tool_result(live_server) -> None:
    url = f"http://127.0.0.1:{live_server['port']}/mcp"
    out = call_tool(
        mcp_url=url,
        tool="drover_handoff",
        args={"repo_owner": "nobody", "repo_name": "nothing", "branch": "never"},
        timeout_s=5.0,
    )
    # Empty lakehouse → well-formed empty payload
    assert "task_id" in out
    assert out.get("summaries") == []


def test_call_tool_timeout(live_server) -> None:
    """Sub-microsecond timeout always fires."""
    url = f"http://127.0.0.1:{live_server['port']}/mcp"
    with pytest.raises(HookTimeout):
        call_tool(
            mcp_url=url,
            tool="drover_handoff",
            args={"repo_owner": "x", "repo_name": "y", "branch": "z"},
            timeout_s=0.0001,
        )


def test_call_tool_error_for_unknown_tool(live_server) -> None:
    url = f"http://127.0.0.1:{live_server['port']}/mcp"
    with pytest.raises(Exception):  # noqa: PT011 — MCP raises a McpError variant
        call_tool(
            mcp_url=url,
            tool="nonexistent_tool",
            args={},
            timeout_s=5.0,
        )
