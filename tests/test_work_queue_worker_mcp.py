"""The coordinator stdio bridge is a bounded client of the existing hub API."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_work_queue_execution import service
from test_work_queue_transport import transport

from drover.server.work_queue.worker_mcp import (
    BoundWorkControl,
    BoundWorkError,
    build_server,
)


def _tool(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    assert len(content) == 1
    assert content[0].type == "text"
    return json.loads(content[0].text)


def test_bound_stdio_tools_use_existing_authenticated_hub_transport(
    transport, monkeypatch
):
    service, _auth, url, device_token, _host_token, _restricted = transport
    bound = BoundWorkControl(
        url,
        device_token,
        "a" * 32,
        "goal",
        1,
    )
    calls = []

    def update(goal_id, number, run_key, action, **kwargs):
        calls.append((goal_id, number, run_key, action, kwargs))
        return {"outcome": "deferred"}

    monkeypatch.setattr(service, "update", update)
    server = build_server(bound)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"drover_work_get", "drover_work_update"}
    assert _tool(server, "drover_work_get", {})["item"]["number"] == 1
    assert _tool(server, "drover_work_update", {"action": "complete"}) == {
        "outcome": "deferred"
    }
    assert len(calls) == 1
    goal_id, number, run_key, action, kwargs = calls[0]
    assert (goal_id, number, run_key, action) == ("goal", 1, "a" * 32, "complete")
    assert kwargs["evidence_key"] is None
    assert kwargs["estimated_cost"] is None


def test_bound_stdio_worker_rejects_missing_or_cross_scope_configuration():
    with pytest.raises(BoundWorkError):
        BoundWorkControl.from_environment({})
    with pytest.raises(BoundWorkError):
        BoundWorkControl.from_environment(
            {
                "DROVER_WORK_CONTROL_URL": "http://[::1:28182",
                "DROVER_MCP_TOKEN": "private",
                "DROVER_WORK_RUN_KEY": "a" * 32,
                "DROVER_WORK_GOAL_ID": "goal",
                "DROVER_WORK_ITEM_NUMBER": "1",
            }
        )
    bound = BoundWorkControl("http://127.0.0.1:28180", "private", "a" * 32, "goal", 1)
    with pytest.raises(BoundWorkError):
        bound.update("verified")
