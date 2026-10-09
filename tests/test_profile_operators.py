import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from drover.server.__main__ import main
from drover.server.control_store import postgres_control_store
from drover.server.profile import (
    ProfileActor,
    http_actor,
    issue_agent_credential,
    revoke_agent_credential,
    set_item_tier,
)
from drover.server.profile_cli import import_record, import_sources
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import PostgresCredentialStore

USER = ProfileActor("operator", "private", True)


def configure(monkeypatch, path):
    monkeypatch.setattr(
        "drover.server.__main__._resolve_config",
        lambda _: SimpleNamespace(duckdb_path=path),
    )


def invoke(*args):
    result = CliRunner().invoke(main, ["profile", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_review_promote_demote_and_revert(pg_control_path, tmp_path, monkeypatch):
    configure(monkeypatch, pg_control_path)
    source = tmp_path / "USER.md"
    source.write_text("# Rules\nUse tables.")
    record = import_sources(source)[0]
    imported = import_record(pg_control_path, record)
    invoke("review", imported["proposal_id"], "accept")
    promoted = invoke(
        "set-tier",
        imported["item_id"],
        "--tier",
        "general",
        "--reason",
        "Reviewed for sharing",
    )
    with postgres_control_store(pg_control_path).connection() as con:
        row = con.execute(
            "SELECT tier, revision, provenance FROM profile_items"
        ).fetchone()
        assert row[:2] == ("general", 2)
        assert row[2]["actor"] == "operator"
        assert row[2]["tier_change"] == {
            "from": "private",
            "to": "general",
            "reason": "Reviewed for sharing",
        }
        assert row[2]["import_classification"]["tier_reasons"] == ["default_private"]
    invoke("review", promoted["proposal_id"], "revert")
    invoke("set-tier", imported["item_id"], "--tier", "trusted", "--reason", "Reviewed")
    invoke("set-tier", imported["item_id"], "--tier", "private", "--reason", "Restrict")
    with postgres_control_store(pg_control_path).connection() as con:
        assert con.execute("SELECT tier, revision FROM profile_items").fetchone() == (
            "private",
            5,
        )
    with pytest.raises(PermissionError):
        set_item_tier(
            pg_control_path,
            imported["item_id"],
            "general",
            actor=ProfileActor(),
            reason="Untrusted",
        )
    with pytest.raises(ValueError):
        set_item_tier(
            pg_control_path, "missing", "private", actor=USER, reason="Reviewed"
        )


def test_issue_revoke_credentials(pg_control_path, monkeypatch):
    configure(monkeypatch, pg_control_path)
    issued = invoke("agents", "issue", "example-agent", "--tier", "trusted")
    store = PostgresCredentialStore(pg_control_path)
    auth = AuthSettings(True, "", credentials=store, legacy_token_enabled=False)
    headers = {"Authorization": f"Bearer {issued['token']}"}
    assert http_actor(pg_control_path, auth, headers) == ProfileActor(
        "example-agent", "trusted"
    )
    assert (
        http_actor(pg_control_path, auth, {"X-Agent": "example-agent"}).tier
        == "general"
    )
    with pytest.raises(ValueError, match="active credential"):
        issue_agent_credential(pg_control_path, "example-agent", "trusted", actor=USER)
    with postgres_control_store(pg_control_path).connection() as con:
        assert (
            con.execute("SELECT count(*) FROM control_credentials").fetchone()[0] == 1
        )
        verifier = con.execute("SELECT verifier FROM control_credentials").fetchone()[0]
        assert issued["token"] not in verifier
        assert (
            con.execute("SELECT updated_by FROM profile_agents").fetchone()[0]
            == "operator"
        )
    revoked = invoke("agents", "revoke", "example-agent")
    assert revoked["revoked"] is True and "token" not in revoked
    assert store.find_active(issued["token"]) is None
    assert http_actor(pg_control_path, auth, headers).tier == "general"
    invoke("agents", "revoke", "example-agent")  # Idempotent.
    reissued = invoke("agents", "issue", "example-agent")
    assert reissued["tier"] == "general"
    assert reissued["credential_id"] != issued["credential_id"]
    with pytest.raises(PermissionError):
        issue_agent_credential(
            pg_control_path, "other", "trusted", actor=ProfileActor()
        )
    with pytest.raises(PermissionError):
        revoke_agent_credential(pg_control_path, "example-agent", actor=ProfileActor())
    with pytest.raises(ValueError):
        issue_agent_credential(pg_control_path, "other", "private", actor=USER)
    with pytest.raises(ValueError):
        revoke_agent_credential(pg_control_path, "missing", actor=USER)
