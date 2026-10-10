"""MCP refuses anonymous callers and preserves HTTP capability and tier policy."""

import asyncio
import json

import pytest
from mcp_auth_helpers import bearer_context
from starlette.testclient import TestClient

from drover.server.mcp.auth import HTTPTokenVerifier
from drover.server.mcp.server import build_mcp_server
from drover.server.profile import (
    ProfileActor,
    act_on_proposal,
    issue_agent_credential,
    propose_profile,
    register_agent,
)
from drover.server.web.auth import (
    DISABLED,
    AuthSettings,
    credential_allows_request,
    mint_session,
)
from drover.server.web.credentials import CredentialStore, PostgresCredentialStore


@pytest.fixture
def credentials(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    return store, AuthSettings(True, "synthetic-operator", credentials=store)


TOOLS = [
    ("drover_session_replay", {"session_id": "test"}),
    ("drover_search", {"query": "test"}),
    ("drover_session_summary", {"session_id": "test"}),
    ("drover_handoff", {}),
    ("drover_fleet_status", {}),
    ("drover_provider_quota", {}),
    ("drover_profile", {}),
    (
        "drover_profile_propose",
        {"layer": "user", "kind": "rule", "tier": "general", "body": "Synthetic rule"},
    ),
    ("drover_session_close", {"session_id": "test"}),
    ("drover_active_handoff", {"session_id": "test"}),
]


@pytest.mark.parametrize("name,args", TOOLS)
@pytest.mark.parametrize("identity", ["anonymous", "revoked"])
def test_anonymous_and_revoked_tools_refused(
    tmp_path, credentials, name, args, identity
):
    store, auth = credentials
    credential, token = store.issue(scope="host", label="Synthetic host")
    store.revoke(credential.id)
    server = build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", auth=auth)
    with bearer_context(token if identity == "revoked" else None):
        with pytest.raises(Exception, match="requires an active|does not authorize"):
            asyncio.run(server.call_tool(name, args))
    assert not (tmp_path / "absent.duckdb").exists()


@pytest.mark.parametrize("scope", ["profile", "preflight"])
@pytest.mark.parametrize(
    "name,args", [tool for tool in TOOLS if tool[0] != "drover_profile"]
)
def test_restricted_scope_cannot_read_general_data_or_mutate(
    tmp_path, credentials, scope, name, args
):
    store, auth = credentials
    _, token = store.issue(scope=scope, label="Synthetic reader")
    server = build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", auth=auth)
    with bearer_context(token):
        with pytest.raises(Exception, match="does not authorize"):
            asyncio.run(server.call_tool(name, args))
    assert not (tmp_path / "absent.duckdb").exists()


def test_preflight_cannot_read_profile(tmp_path, credentials):
    store, auth = credentials
    _, token = store.issue(scope="preflight", label="Synthetic preflight")
    server = build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", auth=auth)
    with bearer_context(token), pytest.raises(Exception, match="does not authorize"):
        asyncio.run(server.call_tool("drover_profile", {}))


@pytest.mark.parametrize("scope", ["host", "device"])
@pytest.mark.parametrize("name,args", TOOLS)
def test_host_and_device_match_http_policy(
    tmp_path, credentials, monkeypatch, scope, name, args
):
    store, auth = credentials
    credential, token = store.issue(scope=scope, label="Synthetic client")
    assert credential_allows_request(
        credential, method="POST", path="/profile/proposals"
    )
    if name != "drover_profile_propose":
        monkeypatch.setattr(
            "drover.server.mcp.tools." + name, lambda **kwargs: {"status": "ok"}
        )
    monkeypatch.setattr(
        "drover.server.mcp.server.http_actor", lambda *args: ProfileActor("test")
    )
    monkeypatch.setattr(
        "drover.server.profile.propose_profile", lambda *a, **kw: {"status": "ok"}
    )
    server = build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", auth=auth)
    with bearer_context(token):
        result = asyncio.run(server.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    assert json.loads(content[0].text)["status"] == "ok"


@pytest.mark.parametrize("tier", ["general", "trusted", "private"])
def test_registered_profile_tiers_unchanged(pg_control_path, tier):
    operator = ProfileActor("operator", "private", True)
    issued = issue_agent_credential(
        pg_control_path, "test-agent", "general", actor=operator
    )
    register_agent(
        pg_control_path, issued["credential_id"], "test-agent", tier, actor=operator
    )
    for item_tier in ("general", "trusted", "private"):
        proposal = propose_profile(
            pg_control_path,
            dict(
                layer="user",
                kind="rule",
                tier=item_tier,
                body=f"Synthetic {item_tier} rule",
            ),
            actor=operator,
        )
        act_on_proposal(
            pg_control_path, proposal["proposal_id"], "accept", actor=operator
        )
    auth = AuthSettings(
        True,
        "",
        credentials=PostgresCredentialStore(pg_control_path),
        legacy_token_enabled=False,
    )
    server = build_mcp_server(duckdb_path=pg_control_path, auth=auth)
    with bearer_context(issued["token"]):
        response = asyncio.run(server.call_tool("drover_profile", {}))
    content = response[0] if isinstance(response, tuple) else response
    result = json.loads(content[0].text)
    allowed = ("general", "trusted", "private")[
        : ("general", "trusted", "private").index(tier) + 1
    ]
    for item_tier in ("general", "trusted", "private"):
        assert (f"Synthetic {item_tier} rule" in result["bundle"]) == (
            item_tier in allowed
        )
    assert result["withheld_count"] == 3 - len(allowed)
    PostgresCredentialStore(pg_control_path).revoke(issued["credential_id"])
    with (
        bearer_context(issued["token"]),
        pytest.raises(Exception, match="does not authorize"),
    ):
        asyncio.run(server.call_tool("drover_profile", {}))


def test_transport_requires_bearer_on_every_request(tmp_path, credentials):
    store, auth = credentials
    credential, token = store.issue(scope="host", label="Synthetic host")
    server = build_mcp_server(
        duckdb_path=tmp_path / "absent.duckdb", host="localhost", auth=auth
    )
    app = server.streamable_http_app()
    with TestClient(app, base_url="http://localhost") as client:
        for method in ("GET", "POST", "DELETE"):
            assert client.request(method, "/mcp").status_code == 401
        assert (
            client.get(
                "/mcp", headers={"Cookie": f"drover_session={mint_session(auth)}"}
            ).status_code
            == 401
        )
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        response = client.post("/mcp", json=payload, headers=headers)
        assert response.status_code == 200
        session = response.headers["mcp-session-id"]
        assert (
            client.post(
                "/mcp", json=payload, headers={"Mcp-Session-Id": session}
            ).status_code
            == 401
        )
        store.revoke(credential.id)
        assert (
            client.post(
                "/mcp", json=payload, headers={**headers, "Mcp-Session-Id": session}
            ).status_code
            == 401
        )
    assert not (tmp_path / "absent.duckdb").exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.invalid", "192.0.2.1"])
def test_remote_bind_refused(tmp_path, host):
    with pytest.raises(
        ValueError, match="protected remote transport is not configured"
    ):
        build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", host=host)


def test_disabled_auth_refused(tmp_path):
    with pytest.raises(ValueError, match="requires authentication"):
        build_mcp_server(duckdb_path=tmp_path / "absent.duckdb", auth=DISABLED)


def test_legacy_operator_policy(credentials):
    _, auth = credentials
    verifier = HTTPTokenVerifier(auth)
    assert asyncio.run(verifier.verify_token("synthetic-operator")) is not None
    assert asyncio.run(verifier.verify_token("wrong")) is None
    assert asyncio.run(HTTPTokenVerifier(DISABLED).verify_token("anything")) is None
    disabled = AuthSettings(True, "synthetic-operator", legacy_token_enabled=False)
    assert (
        asyncio.run(HTTPTokenVerifier(disabled).verify_token("synthetic-operator"))
        is None
    )


def test_every_registered_tool_refuses_anonymous(tmp_path):
    server = build_mcp_server(duckdb_path=tmp_path / "absent.duckdb")
    tools = asyncio.run(server.list_tools())
    with bearer_context(None):
        for tool in tools:
            arguments = {
                key: (["test"] if key == "harness_ids" else "test")
                for key in tool.inputSchema.get("required", [])
            }
            with pytest.raises(Exception, match="requires an active bearer"):
                asyncio.run(server.call_tool(tool.name, arguments))
    assert not (tmp_path / "absent.duckdb").exists()


def test_transport_uses_current_request_capability(tmp_path, credentials, monkeypatch):
    store, auth = credentials
    _, host_token = store.issue(scope="host", label="Synthetic host")
    _, profile_token = store.issue(scope="profile", label="Synthetic profile")
    calls = []

    def read(**kwargs):
        calls.append("read")
        return {"status": "ok"}

    monkeypatch.setattr("drover.server.mcp.tools.drover_fleet_status", read)
    server = build_mcp_server(
        duckdb_path=tmp_path / "absent.duckdb", host="localhost", auth=auth
    )
    server.settings.json_response = True
    with TestClient(
        server.streamable_http_app(), base_url="http://localhost"
    ) as client:
        headers = {
            "Authorization": f"Bearer {host_token}",
            "Accept": "application/json, text/event-stream",
        }
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        response = client.post("/mcp", json=init, headers=headers)
        assert response.status_code == 200
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            ).status_code
            == 202
        )
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "drover_fleet_status", "arguments": {}},
        }
        assert (
            not client.post("/mcp", json=call, headers=headers)
            .json()["result"]
            .get("isError")
        )
        # A session initialized with broad authority must not retain that authority.
        restricted = {**headers, "Authorization": f"Bearer {profile_token}"}
        response = client.post("/mcp", json=call, headers=restricted)

        def assert_scope_denied(response):
            assert response.status_code == 200
            payload = response.json()
            assert "error" not in payload
            assert payload["result"]["isError"] is True
            assert payload["result"]["content"] == [
                {
                    "type": "text",
                    "text": "Error executing tool drover_fleet_status: "
                    "MCP credential does not authorize this capability",
                }
            ]

        if response.status_code == 404:
            # Newer SDKs conceal sessions owned by another credential.
            payload = response.json()
            assert "result" not in payload
            assert payload["error"] == {"code": -32600, "message": "Session not found"}
        else:
            # Older supported SDKs dispatch using the current request identity.
            assert_scope_denied(response)

        # An authenticated profile client in its own session reaches the tool
        # guard and receives a tool-level scope denial, not a protocol error.
        profile_headers = {
            "Authorization": f"Bearer {profile_token}",
            "Accept": "application/json, text/event-stream",
        }
        response = client.post("/mcp", json=init, headers=profile_headers)
        assert response.status_code == 200
        profile_headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=profile_headers,
            ).status_code
            == 202
        )
        assert_scope_denied(client.post("/mcp", json=call, headers=profile_headers))
    assert calls == ["read"]


def test_runtime_injects_shared_http_auth(tmp_path, monkeypatch):
    from drover.config import default_config
    from drover.server import __main__ as cli

    cfg = default_config()
    auth = AuthSettings(True, "synthetic-operator")
    seen = {}
    monkeypatch.setattr(
        cli, "load_auth", lambda supplied: auth if supplied is cfg else None
    )
    monkeypatch.setattr(cli, "build_mcp_server", lambda **kwargs: seen.update(kwargs))
    cli._build_runtime_mcp_server(cfg=cfg, host="localhost", backend_config=None)
    assert seen["auth"] is auth
    assert seen["duckdb_path"] == cfg.duckdb_path
