"""Profile credentials cannot acquire host, device or operator capabilities."""

import http.client
import json
from urllib.parse import urlencode

import pytest

from drover.server.control_store import postgres_control_store
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.profile import (
    ProfileActor,
    act_on_proposal,
    issue_agent_credential,
    propose_profile,
    register_agent,
    resolve_actor,
    revoke_agent_credential,
)
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings, mint_session
from drover.server.web.credentials import PostgresCredentialStore
from drover.server.web.pairing import PairingCodes

OPERATOR = ProfileActor("operator", "private", True)


@pytest.fixture
def profile_server(pg_control_path, tmp_path):
    issued = issue_agent_credential(
        pg_control_path, "example-agent", "trusted", actor=OPERATOR
    )
    store = PostgresCredentialStore(pg_control_path)
    auth = AuthSettings(True, "example-operator-token", credentials=store)
    collector = MetricsCollector(
        duckdb_path=pg_control_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=auth, pairing=PairingCodes()
    )

    def request(method, path, body=None, token=issued["token"], headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        try:
            connection.request(
                method,
                path,
                body=(
                    body
                    if isinstance(body, str)
                    else json.dumps(body) if body is not None else None
                ),
                headers=request_headers,
            )
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    try:
        yield issued, store, auth, request
    finally:
        server.shutdown()
        server.server_close()


def test_issued_profile_credential_reads_trusted_bundle(
    pg_control_path, profile_server
):
    issued, store, _, request = profile_server
    credential = store.get(issued["credential_id"])
    assert credential.scope == "profile"
    assert credential.host_id is None
    for tier in ("general", "trusted", "private"):
        proposal = propose_profile(
            pg_control_path,
            dict(
                layer="user",
                kind="rule",
                tier=tier,
                body=f"Synthetic {tier} preference",
            ),
        )
        act_on_proposal(
            pg_control_path, proposal["proposal_id"], "accept", actor=OPERATOR
        )
    status, _, body = request("GET", "/profile?scope=first_turn")
    assert status == 200
    result = json.loads(body)
    assert "Synthetic general preference" in result["bundle"]
    assert "Synthetic trusted preference" in result["bundle"]
    assert "Synthetic private preference" not in body.decode()
    assert result["withheld_count"] == 1


def test_profile_scope_denied_before_non_profile_dispatch(profile_server):
    issued, store, auth, request = profile_server
    other, other_token = store.issue(
        scope="host", label="Example host", host_id="example-host"
    )
    routes = [
        ("POST", "/harness/hosts"),
        ("POST", "/harness/hosts/example-host/heartbeat"),
        ("GET", "/harness/relay"),
        ("POST", "/harness/events"),
        ("GET", "/harness"),
        ("GET", "/harness/hosts"),
        ("GET", "/harness/sessions"),
        ("GET", "/sessions/history"),
        ("GET", "/auth/credentials"),
        ("DELETE", f"/auth/credentials/{other.id}"),
        ("DELETE", f"/auth/credentials/{issued['credential_id']}"),
        ("POST", "/auth/credentials"),
        ("POST", "/auth/pair-codes"),
        ("POST", "/auth/pair"),
        ("POST", "/harness/probe"),
        ("GET", "/harness/sessions/example-session/terminal"),
        ("GET", "/harness/sessions/example-session/stream"),
        ("POST", "/harness/hosts/example-host/sessions"),
        ("POST", "/profile/proposals"),
        ("GET", "/profile/proposals"),
        ("POST", "/profile/agents"),
        ("POST", "/profile"),
        ("GET", "/profile/"),
        ("DELETE", "/auth/device/credential"),
        ("PUT", "/auth/device/apns"),
        ("GET", "/_internal/analytics/healthz"),
        ("POST", "/auth/login"),
        ("GET", "/unknown-future-route"),
    ]
    # A valid browser cookie cannot widen the bearer token's scope.
    cookie = {"Cookie": f"{auth.cookie_name}={mint_session(auth)}"}
    for method, path in routes:
        status, headers, _ = request(method, path, {}, headers=cookie)
        assert status == 401, (method, path, status)
        assert "Set-Cookie" not in headers
    assert store.find_active(other_token) is not None
    assert store.find_active(issued["token"]) is not None


def test_profile_token_cannot_exchange_for_browser_cookie(profile_server):
    issued, _, _, request = profile_server
    status, headers, _ = request(
        "POST",
        "/auth/login",
        token=None,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"token": issued["token"]}),
    )
    assert status == 302
    assert headers["Location"] == "/auth/login?error=1"
    assert "Set-Cookie" not in headers


def test_host_retirement_does_not_revoke_same_named_agent(
    pg_control_path, profile_server
):
    issued, store, auth, request = profile_server
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(
        host_id=issued["agent_id"], display_name="Example host", kind="linux"
    )
    host_credential, host_token = store.issue(
        scope="host", label="Example host", host_id=issued["agent_id"]
    )
    status, _, _ = request(
        "POST",
        f"/harness/hosts/{issued['agent_id']}/retire",
        {"reason": "Synthetic retirement"},
        token=auth.api_token,
    )
    assert status == 200
    assert registry.get_host(issued["agent_id"]).retired_at is not None
    assert store.get(host_credential.id).revoked_at is not None
    assert store.find_active(host_token) is None
    assert store.find_active(issued["token"]) is not None
    assert request("GET", "/profile")[0] == 200


@pytest.mark.parametrize("revocation", ["profile-command", "generic-operator-route"])
def test_operator_listing_and_revocation(pg_control_path, profile_server, revocation):
    issued, store, auth, request = profile_server
    status, _, body = request("GET", "/auth/credentials", token=auth.api_token)
    assert status == 200
    listing = json.loads(body)["credentials"]
    credential = next(c for c in listing if c["id"] == issued["credential_id"])
    assert credential["scope"] == "profile" and credential["host_id"] is None
    assert "token" not in credential and "verifier" not in credential
    assert issued["token"] not in body.decode()
    if revocation == "profile-command":
        revoke_agent_credential(pg_control_path, issued["agent_id"], actor=OPERATOR)
    else:
        assert (
            request(
                "DELETE",
                f"/auth/credentials/{issued['credential_id']}",
                token=auth.api_token,
            )[0]
            == 204
        )
    assert store.find_active(issued["token"]) is None
    assert request("GET", "/profile")[0] == 401


@pytest.mark.parametrize(
    "scope,revoked",
    [
        ("profile", False),
        ("device", False),
        ("host", False),
        ("preflight", False),
        ("profile", True),
    ],
)
def test_agent_link_requires_active_profile_credential(
    pg_control_path, profile_server, scope, revoked
):
    _, store, auth, request = profile_server
    credential, _ = store.issue(scope=scope, label="example-link")
    if revoked:
        store.revoke(credential.id)
    body = dict(credential_id=credential.id, agent_id="example-link", tier="trusted")
    status, _, payload = request("POST", "/profile/agents", body, token=auth.api_token)
    if scope == "profile" and not revoked:
        assert status == 200
        assert json.loads(payload) == {"agent_id": "example-link", "tier": "trusted"}
        assert register_agent(pg_control_path, actor=OPERATOR, **body) == json.loads(
            payload
        )
    else:
        assert status == 400
        assert json.loads(payload) == {"error": "active profile credential required"}
        with pytest.raises(ValueError, match="^active profile credential required$"):
            register_agent(pg_control_path, actor=OPERATOR, **body)
        with postgres_control_store(pg_control_path).connection() as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM profile_agents WHERE agent_id = 'example-link'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize(
    "scope,revoked",
    [
        ("host", False),
        ("device", False),
        ("profile", True),
        ("profile", False),
    ],
)
def test_legacy_binding_resolution_requires_active_profile(
    pg_control_path, profile_server, scope, revoked
):
    _, store, _, request = profile_server
    credential, token = store.issue(scope=scope, label="example-legacy-agent")
    if revoked:
        store.revoke(credential.id)
    # Simulate a persisted binding that predates registry scope validation.
    with postgres_control_store(pg_control_path).connection() as con:
        con.execute(
            "INSERT INTO profile_agents (agent_id, credential_id, tier, updated_by) "
            "VALUES ('example-legacy-agent', ?, 'trusted', 'operator')",
            [credential.id],
        )
        before = con.execute(
            "SELECT to_jsonb(a) FROM profile_agents a WHERE agent_id = 'example-legacy-agent'"
        ).fetchone()[0]
    for tier in ("general", "trusted", "private"):
        proposal = propose_profile(
            pg_control_path,
            dict(
                layer="user",
                kind="rule",
                tier=tier,
                body=f"Synthetic {tier} preference",
            ),
        )
        act_on_proposal(
            pg_control_path, proposal["proposal_id"], "accept", actor=OPERATOR
        )
    trusted = scope == "profile" and not revoked
    expected = (
        ProfileActor("example-legacy-agent", "trusted")
        if trusted
        else ProfileActor(credential.id)
    )
    assert resolve_actor(pg_control_path, credential.id) == expected
    status, _, body = request("GET", "/profile", token=token)
    if revoked:
        assert status == 401
    else:
        assert status == 200
        result = json.loads(body)
        assert "Synthetic general preference" in result["bundle"]
        assert ("Synthetic trusted preference" in result["bundle"]) == trusted
        assert "Synthetic private preference" not in body.decode()
        assert result["withheld_count"] == (1 if trusted else 2)
    if scope in ("host", "device"):
        status, _, body = request(
            "POST",
            "/profile/proposals",
            dict(
                layer="user",
                kind="preference",
                tier="general",
                body="Synthetic new preference",
            ),
            token=token,
        )
        assert status == 200
        proposed = json.loads(body)
        assert proposed["status"] == "pending"
        with postgres_control_store(pg_control_path).connection() as con:
            assert (
                con.execute(
                    "SELECT agent_id FROM profile_proposals WHERE proposal_id = ?",
                    [proposed["proposal_id"]],
                ).fetchone()[0]
                == credential.id
            )
    # Resolution must not migrate, rewrite or remove legacy registry rows.
    with postgres_control_store(pg_control_path).connection() as con:
        after = con.execute(
            "SELECT to_jsonb(a) FROM profile_agents a WHERE agent_id = 'example-legacy-agent'"
        ).fetchone()[0]
    assert after == before
