import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drover.server.control_store import postgres_control_store
from drover.server.mcp.server import build_mcp_server
from drover.server.profile import (
    ProfileActor,
    ProfileConflict,
    act_on_proposal,
    propose_profile,
    read_profile,
    register_agent,
)
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import DISABLED, AuthSettings
from drover.server.web.credentials import PostgresCredentialStore

USER = ProfileActor("operator", "private", True)
TRUSTED = ProfileActor("example-trusted", "trusted")
GENERAL = ProfileActor("example-general")


def change(body="Use tables", tier="general"):
    return dict(layer="user", kind="preference", tier=tier, body=body)


def stored(path, item_id):
    with postgres_control_store(path).connection() as con:
        return con.execute(
            "SELECT to_jsonb(i) FROM profile_items i WHERE item_id = ?", [item_id]
        ).fetchone()[0]


def test_auto_accept_and_private_approval(pg_control_path):
    accepted = propose_profile(
        pg_control_path, change(), actor=TRUSTED, session_id="example-session"
    )
    assert accepted["status"] == "accepted"
    item = stored(pg_control_path, accepted["item_id"])
    assert item["provenance"]["agent"] == TRUSTED.agent_id
    assert item["provenance"]["session"] == "example-session"
    assert item["provenance"]["time"]
    for actor in (GENERAL, TRUSTED, USER):
        pending = propose_profile(
            pg_control_path, change("Private information", "private"), actor=actor
        )
        assert pending["status"] == "pending"
        with pytest.raises(PermissionError):
            act_on_proposal(
                pg_control_path, pending["proposal_id"], "accept", actor=TRUSTED
            )
        act_on_proposal(pg_control_path, pending["proposal_id"], "accept", actor=USER)
        assert stored(pg_control_path, pending["item_id"])["tier"] == "private"
    assert (
        propose_profile(pg_control_path, change(), actor=GENERAL)["status"] == "pending"
    )


def test_reject_and_revert_creation_update(pg_control_path):
    pending = propose_profile(pg_control_path, change(), actor=GENERAL)
    assert (
        act_on_proposal(pg_control_path, pending["proposal_id"], "reject", actor=USER)[
            "status"
        ]
        == "rejected"
    )
    with pytest.raises(ProfileConflict):
        act_on_proposal(pg_control_path, pending["proposal_id"], "accept", actor=USER)
    first = propose_profile(pg_control_path, change("Original"), actor=TRUSTED)
    before = stored(pg_control_path, first["item_id"])
    second = propose_profile(
        pg_control_path, change("Replacement"), actor=TRUSTED, item_id=first["item_id"]
    )
    act_on_proposal(pg_control_path, second["proposal_id"], "revert", actor=USER)
    reverted = stored(pg_control_path, first["item_id"])
    assert reverted["body"] == "Original"
    assert reverted["updated_at"] == before["updated_at"]
    assert reverted["revision"] == 3
    assert reverted["provenance"]["reversion"]["actor"] == "operator"
    with postgres_control_store(pg_control_path).connection() as con:
        accepted_history = con.execute(
            "SELECT change FROM profile_proposals WHERE proposal_id = ?",
            [second["proposal_id"]],
        ).fetchone()[0]["accepted_provenance"]
    assert accepted_history["agent"] == TRUSTED.agent_id
    assert accepted_history["time"]
    assert accepted_history["actor"] == TRUSTED.agent_id
    creation = propose_profile(pg_control_path, change("Temporary"), actor=TRUSTED)
    act_on_proposal(pg_control_path, creation["proposal_id"], "revert", actor=USER)
    assert "Temporary" not in read_profile(pg_control_path)["bundle"]
    assert stored(pg_control_path, creation["item_id"])["status"] == "reverted"


def test_stale_accept_and_revert_conflicts(pg_control_path):
    first = propose_profile(pg_control_path, change("Original"), actor=TRUSTED)
    pending = propose_profile(
        pg_control_path, change("Pending"), actor=GENERAL, item_id=first["item_id"]
    )
    propose_profile(
        pg_control_path, change("Newer"), actor=TRUSTED, item_id=first["item_id"]
    )
    with pytest.raises(ProfileConflict):
        act_on_proposal(pg_control_path, pending["proposal_id"], "accept", actor=USER)
    with pytest.raises(ProfileConflict):
        act_on_proposal(pg_control_path, first["proposal_id"], "revert", actor=USER)
    assert stored(pg_control_path, first["item_id"])["body"] == "Newer"


def test_cannot_edit_hidden_or_demote_private(pg_control_path):
    pending = propose_profile(
        pg_control_path, change("Private", "private"), actor=TRUSTED
    )
    act_on_proposal(pg_control_path, pending["proposal_id"], "accept", actor=USER)
    for actor in (GENERAL, TRUSTED):
        with pytest.raises(PermissionError):
            propose_profile(
                pg_control_path,
                change("Demoted"),
                actor=actor,
                item_id=pending["item_id"],
            )
    demoted = propose_profile(
        pg_control_path,
        change("Reviewed demotion"),
        actor=USER,
        item_id=pending["item_id"],
    )
    assert demoted["status"] == "pending"
    act_on_proposal(pg_control_path, demoted["proposal_id"], "accept", actor=USER)


def test_concurrent_accept_and_transaction_rollback(pg_control_path):
    pending = propose_profile(pg_control_path, change(), actor=GENERAL)

    def accept():
        try:
            return act_on_proposal(
                pg_control_path, pending["proposal_id"], "accept", actor=USER
            )["status"]
        except ProfileConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: accept(), range(2))) == [
            "accepted",
            "conflict",
        ]
    with pytest.raises(ValueError):
        propose_profile(pg_control_path, change(), actor=TRUSTED, item_id="missing")
    with postgres_control_store(pg_control_path).connection() as con:
        assert con.execute("SELECT count(*) FROM profile_proposals").fetchone()[0] == 1


def test_registry_requires_operator(pg_control_path):
    with pytest.raises(PermissionError):
        register_agent(
            pg_control_path,
            "example-credential",
            "example-agent",
            "private",
            actor=TRUSTED,
        )
    credential, _ = PostgresCredentialStore(pg_control_path).issue(
        scope="profile", label="example-agent"
    )
    register_agent(
        pg_control_path, credential.id, "example-agent", "trusted", actor=USER
    )


@pytest.mark.parametrize(
    "extra",
    [
        dict(tier="invalid"),
        dict(layer="invalid"),
        dict(body=""),
        dict(expires_at="2026-10-01"),
        dict(agent_id="spoof"),
    ],
)
def test_proposal_validation(pg_control_path, extra):
    with pytest.raises(ValueError):
        propose_profile(pg_control_path, {**change(), **extra})


def test_http_authority_and_mcp_pending(pg_control_path):
    credentials = PostgresCredentialStore(pg_control_path)
    credential, profile_token = credentials.issue(
        scope="profile", label="example-agent"
    )
    _, token = credentials.issue(
        scope="host", label="example-host", host_id="example-host"
    )
    auth = AuthSettings(True, "example-operator-token", credentials=credentials)
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=SimpleNamespace(duckdb_path=pg_control_path),
        auth=auth,
    )

    def post(route, body, bearer=token):
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}{route}",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {bearer}"},
        )
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    try:
        assert (
            post(
                "/profile/agents",
                dict(
                    agent_id="example-agent",
                    credential_id=credential.id,
                    tier="trusted",
                ),
            )[0]
            == 403
        )
        assert (
            post(
                "/profile/agents",
                dict(
                    agent_id="example-agent",
                    credential_id=credential.id,
                    tier="trusted",
                ),
                "example-operator-token",
            )[0]
            == 200
        )
        assert post("/profile/proposals", change(), profile_token)[0] == 401
        assert post("/profile/proposals", change())[1]["status"] == "pending"
        code, pending = post("/profile/proposals", change("Sensitive", "private"))
        assert code == 200 and pending["status"] == "pending"
        route = f"/profile/proposals/{pending['proposal_id']}/accept"
        assert post(route, {})[0] == 403
        assert post(route, {}, "example-operator-token")[1]["status"] == "accepted"
        assert post(route, {}, "example-operator-token")[0] == 409
        assert (
            post("/profile/proposals", {**change(), "agent_id": "operator"})[0] == 400
        )
        assert post("/profile/proposals", {**change(), "body": "x" * 140000})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
    mcp = build_mcp_server(duckdb_path=pg_control_path)
    result = asyncio.run(
        mcp.call_tool("drover_profile_propose", change("MCP proposal"))
    )
    content = result[0] if isinstance(result, tuple) else result
    assert json.loads(content[0].text)["status"] == "pending"


def test_auth_disabled_has_no_approval(pg_control_path):
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=SimpleNamespace(duckdb_path=pg_control_path),
        auth=DISABLED,
    )
    pending = propose_profile(pg_control_path, change())
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/profile/proposals/{pending['proposal_id']}/accept",
            data=b"{}",
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(request)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_private_review_queue_is_user_only(pg_control_path):
    from drover.server.profile import list_proposals

    pending = propose_profile(
        pg_control_path, change("Private review", "private"), actor=TRUSTED
    )
    with pytest.raises(PermissionError):
        list_proposals(pg_control_path, actor=TRUSTED)
    result = list_proposals(pg_control_path, actor=USER)
    assert result["proposals"][0]["proposal_id"] == pending["proposal_id"]
    assert result["proposals"][0]["change"]["body"] == "Private review"
    with pytest.raises(ValueError):
        list_proposals(pg_control_path, actor=USER, limit=101)
