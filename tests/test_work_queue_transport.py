"""HTTP credential boundaries and authenticated MCP delegation to control."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import requests

from drover.server.mcp.server import build_mcp_server
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore
from drover.server.work_queue.client import control_call
from test_work_queue_execution import ACTOR, launch, service, update


@pytest.fixture
def transport(service, tmp_path):
    credentials = CredentialStore(tmp_path / "credentials.json")
    device, device_token = credentials.issue(scope="device", label="synthetic device")
    host, host_token = credentials.issue(
        scope="host", label="synthetic host", host_id="host"
    )
    _, restricted = credentials.issue(scope="preflight", label="synthetic restricted")
    cfg = service.config.model_copy(
        update={"principal_roles": {"credential:" + device.id: "nix"}}
    )
    service.config = cfg
    auth = AuthSettings(
        enabled=True, api_token="synthetic-legacy", credentials=credentials
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=SimpleNamespace(work_control=service),
        auth=auth,
    )
    url = f"http://127.0.0.1:{server.server_port}"
    yield service, auth, url, device_token, host_token, restricted
    server.shutdown()
    server.server_close()


def test_http_reads_auth_launch_and_identity_spoofing(transport):
    service, auth, url, device, host, restricted = transport
    args = {"goal_id": "goal", "number": 1}
    for token, expected in [(None, 401), (restricted, 401), (device, 200), (host, 200)]:
        response = requests.get(
            url + "/work/get",
            params=args,
            headers={"Authorization": "Bearer " + token} if token else {},
            timeout=5,
        )
        assert response.status_code == expected
    assert response.headers["Cache-Control"] == "no-store"
    launch_args = {**args, "request_key": "http:first", "estimated_cost": 5}
    assert (
        requests.post(
            url + "/work/launch",
            json=launch_args,
            headers={"Authorization": "Bearer " + host},
            timeout=5,
        ).status_code
        == 403
    )
    spoof = requests.post(
        url + "/work/launch",
        json={**launch_args, "actor_principal": "nix"},
        headers={"Authorization": "Bearer " + device},
        timeout=5,
    )
    assert spoof.status_code == 400
    row = control_call(
        url, {"Authorization": "Bearer " + device}, "launch", launch_args
    )
    assert row["actor_principal"].startswith("credential:")
    assert row["actor_principal"] != "nix"
    assert len(service.harness.calls) == 1
    control_call(url, {"Authorization": "Bearer " + device}, "launch", launch_args)
    assert len(service.harness.calls) == 1
    question = control_call(
        url,
        {"Authorization": "Bearer " + host},
        "question",
        {
            **args,
            "request_key": "http:question",
            "category": "revision",
            "options": ["reject"],
        },
    )
    decision = {**args, "decision_id": question["decision_id"], "action": "reject"}
    assert (
        requests.post(
            url + "/work/decide",
            json=decision,
            headers={"Authorization": "Bearer " + host},
            timeout=5,
        ).status_code
        == 403
    )
    assert control_call(url, {"Authorization": "Bearer " + device}, "decide", decision)[
        "resolved"
    ]


def test_bound_live_reads_and_shadow_write_guard(transport):
    service, _, url, device, _, _ = transport
    service.providers["goal"].data["body"] = "x" * 10000
    result = control_call(
        url,
        {"Authorization": "Bearer " + device},
        "get",
        {"goal_id": "goal", "number": 1},
    )
    assert len(result["item"]["body"].encode()) <= 4096
    assert result["truncated"]
    service.config = service.config.model_copy(update={"shadow_mode": True})
    response = requests.post(
        url + "/work/launch",
        json={
            "goal_id": "goal",
            "number": 1,
            "request_key": "shadow",
            "estimated_cost": 5,
        },
        headers={"Authorization": "Bearer " + device},
        timeout=5,
    )
    assert response.status_code == 403
    assert not service.store.links()


def test_mcp_seven_tools_are_thin_control_proxies(
    transport, monkeypatch, authenticated_mcp
):
    service, auth, url, device, _, _ = transport
    # Use the verified HTTP request identity supplied by the existing MCP auth fixture.
    from drover.server.mcp import auth as mcp_auth

    monkeypatch.setattr(
        mcp_auth,
        "current_headers",
        lambda server=None: {"Authorization": "Bearer " + device},
    )
    from drover.server.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "current_headers", mcp_auth.current_headers)
    server = build_mcp_server(
        duckdb_path=service.store.path, auth=auth, work_control_url=url
    )
    tools = asyncio.run(server.list_tools())
    assert {
        "drover_work_" + name
        for name in ("list", "get", "launch", "link", "update", "question", "decide")
    } <= {t.name for t in tools}

    def call(name, args):
        content = asyncio.run(server.call_tool("drover_work_" + name, args))
        return json.loads(content[0].text)

    assert call("list", {"goal_id": "goal"})["items"][0]["number"] == 1
    row = call(
        "launch",
        {
            "goal_id": "goal",
            "number": 1,
            "request_key": "mcp:first",
            "estimated_cost": 5,
        },
    )
    linked = call(
        "link",
        {
            "goal_id": "goal",
            "number": 1,
            "run_key": row["run_key"],
            "session_id": row["session_id"],
            "pr_number": 2,
        },
    )
    assert linked["pr_ref"] == "https://github.com/example/project/pull/2"
    assert call("get", {"goal_id": "goal", "number": 1})["links"][0]["id"] == row["id"]
    assert (
        call(
            "update",
            {
                "goal_id": "goal",
                "number": 1,
                "run_key": row["run_key"],
                "action": "progress",
                "evidence_key": "progress:1",
            },
        )["outcome"]
        == "progress"
    )
    question = call(
        "question",
        {
            "goal_id": "goal",
            "number": 1,
            "request_key": "mcp:question",
            "category": "revision",
            "options": ["reject"],
        },
    )
    assert call(
        "decide",
        {
            "goal_id": "goal",
            "number": 1,
            "decision_id": question["decision_id"],
            "action": "reject",
        },
    )["resolved"]


def test_host_can_report_only_its_linked_execution(transport):
    service, _, url, _, host, _ = transport
    row = launch(service)
    args = {
        "goal_id": "goal",
        "number": 1,
        "run_key": row["run_key"],
        "action": "progress",
        "evidence_key": "progress:host",
    }
    assert (
        control_call(url, {"Authorization": "Bearer " + host}, "update", args)[
            "outcome"
        ]
        == "progress"
    )
    service.harness.sessions[row["run_key"]].host_id = "other-host"
    args["evidence_key"] = "progress:other"
    assert (
        requests.post(
            url + "/work/update",
            json=args,
            headers={"Authorization": "Bearer " + host},
            timeout=5,
        ).status_code
        == 403
    )


def test_proxy_refuses_remote_and_missing_bearer():
    with pytest.raises(ValueError):
        control_call("https://example.test", {}, "get", {})
    with pytest.raises(PermissionError):
        control_call("http://127.0.0.1:1", {}, "get", {})


def test_restart_bridge_resolves_lost_daemon_response_without_post(service):
    from drover.server.work_queue.runtime import HarnessBridge

    service.harness.timeout = True
    with pytest.raises(TimeoutError):
        launch(service)
    intent = service.store.links()[0]
    remote = {
        "session_id": "remote-session",
        "client_session_id": intent["run_key"],
        "harness": "codex",
        "parent_session_id": None,
    }
    calls = []
    sessions = {}
    registry = SimpleNamespace(
        session_by_client_id=lambda key: sessions.get(key),
        list_hosts=lambda: [
            SimpleNamespace(
                host_id="host", liveness=lambda: SimpleNamespace(usable=True)
            )
        ],
    )

    def request(host, path, *, method, timeout_s):
        calls.append((method, path))
        return 200, json.dumps({"sessions": [remote]})

    def sync(host_id, payload, body):
        assert host_id == "host"
        sessions[payload["client_session_id"]] = SimpleNamespace(**json.loads(body))

    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry
    bridge.collector = SimpleNamespace(
        _harness_request=request, _sync_created_harness_session=sync
    )
    assert bridge.resolve(intent["run_key"]).session_id == "remote-session"
    assert bridge.resolve(intent["run_key"]).session_id == "remote-session"
    assert calls == [("GET", "/sessions?client_session_id=" + intent["run_key"])]
