"""The coordinator stdio bridge is a bounded client of the existing hub API."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from test_work_queue_execution import service
from test_work_queue_local_candidates import _deferred_completion_service
from test_work_queue_transport import transport

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore
from drover.server.work_queue.service import identity
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


def test_bound_completion_drops_nonsemantic_metadata_but_progress_keeps_it(
    monkeypatch,
):
    bound = BoundWorkControl("http://127.0.0.1:28180", "private", "a" * 32, "goal", 1)
    calls = []

    def call(_self, operation, args):
        calls.append((operation, args))
        return {"outcome": "ok"}

    monkeypatch.setattr(BoundWorkControl, "_call", call)
    assert bound.update(
        "complete", evidence_key="sha=" + "a" * 64, estimated_cost=0.01
    ) == {"outcome": "ok"}
    assert bound.update("progress", evidence_key="progress:1", estimated_cost=0.01) == {
        "outcome": "ok"
    }
    assert calls == [
        (
            "update",
            {
                "goal_id": "goal",
                "number": 1,
                "run_key": "a" * 32,
                "action": "complete",
                "evidence_key": None,
                "estimated_cost": None,
            },
        ),
        (
            "update",
            {
                "goal_id": "goal",
                "number": 1,
                "run_key": "a" * 32,
                "action": "progress",
                "evidence_key": "progress:1",
                "estimated_cost": 0.01,
            },
        ),
    ]


def test_bound_stdio_worker_rejects_missing_or_cross_scope_configuration():
    with pytest.raises(BoundWorkError):
        BoundWorkControl.from_environment({})
    for url in ("http://[::1:28182", "http://127.0.0.1:65536"):
        with pytest.raises(BoundWorkError):
            BoundWorkControl.from_environment(
                {
                    "DROVER_WORK_CONTROL_URL": url,
                    "DROVER_MCP_TOKEN": "private",
                    "DROVER_WORK_RUN_KEY": "a" * 32,
                    "DROVER_WORK_GOAL_ID": "goal",
                    "DROVER_WORK_ITEM_NUMBER": "1",
                }
            )
    bound = BoundWorkControl("http://127.0.0.1:28180", "private", "a" * 32, "goal", 1)
    with pytest.raises(BoundWorkError):
        bound.update("verified")


def _stdio_request(process, request):
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    while True:
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, f"stdio MCP did not answer {request['method']}"
        response = json.loads(process.stdout.readline())
        if response.get("id") == request.get("id"):
            return response


def test_bound_stdio_child_lists_tools_and_defers_through_existing_hub(
    tmp_path, pg_control_path
):
    scope, _provider, harness, service, worker, work = _deferred_completion_service(
        tmp_path, pg_control_path
    )
    credentials = CredentialStore(tmp_path / "credentials.json")
    host, host_token = credentials.issue(
        scope="host", label="synthetic host", host_id="host"
    )
    # The stdio child receives a host credential. It is not a coordinator:
    # its authority is the immutable host/run binding from the launch.
    assert service.config.principal_roles.get("credential:" + host.id) is None
    auth = AuthSettings(
        enabled=True, api_token="synthetic-legacy", credentials=credentials
    )
    httpd = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=type("Collector", (), {"work_control": service})(),
        auth=auth,
    )
    url = f"http://127.0.0.1:{httpd.server_port}"
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(source_root),
        "DROVER_WORK_CONTROL_URL": url,
        "DROVER_MCP_TOKEN": host_token,
        "DROVER_WORK_RUN_KEY": work["run_key"],
        "DROVER_WORK_GOAL_ID": scope.goal_id,
        "DROVER_WORK_ITEM_NUMBER": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "drover.server.work_queue.worker_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        initialized = _stdio_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "drover-bound-work"
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        process.stdin.flush()
        tools = _stdio_request(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert {tool["name"] for tool in tools["result"]["tools"]} == {
            "drover_work_get",
            "drover_work_update",
        }
        deferred = _stdio_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "drover_work_update",
                    "arguments": {
                        "action": "complete",
                        # Natural model prose often supplies this as a label.
                        # It is not part of a bound completion's authority.
                        "evidence_key": "sha=" + "a" * 64,
                    },
                },
            },
        )
        assert json.loads(deferred["result"]["content"][0]["text"]) == {
            "outcome": "deferred"
        }
    finally:
        process.terminate()
        process.wait(timeout=5)
        httpd.shutdown()
        httpd.server_close()

    # The actual service stored exactly one terminal-only intent. Its normal
    # queue worker, not the stdio child, creates the verification successor
    # after the original worker reaches its terminal boundary.
    assert len(service.store.events(identity(work["run_key"], "complete", None))) == 1
    harness.sessions[work["run_key"]].status = "completed"
    # A terminal daemon status alone is insufficient: the deferred path also
    # requires the exact signed worker turn to have finished.
    harness.sessions[work["run_key"]].turn_finished = True
    worker.process_once()
    assert [
        link["run_role"]
        for link in service.store.links(goal_id=scope.goal_id, number=1)
    ] == [
        "work",
        "verify",
    ]
