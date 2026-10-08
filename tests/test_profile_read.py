import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from drover.server.control_store import postgres_control_store
from drover.server.mcp.server import build_mcp_server
from drover.server.profile import ProfileActor, http_actor, read_profile
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import DISABLED, AuthSettings
from drover.server.web.credentials import PostgresCredentialStore

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def item(
    path,
    body,
    *,
    tier="general",
    layer="user",
    kind="preference",
    age=0,
    expired=False,
    item_id=None,
):
    with postgres_control_store(path).connection() as con:
        con.execute(
            "INSERT INTO profile_items "
            "(item_id, layer, kind, tier, body, provenance, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, '{}'::jsonb, ?, ?)",
            [
                item_id or body,
                layer,
                kind,
                tier,
                body,
                NOW - timedelta(days=age),
                NOW if expired else None,
            ],
        )


def test_tiers_and_scopes(pg_control_path):
    item(pg_control_path, "public-preference")
    item(pg_control_path, "trusted-secret-title", tier="trusted")
    item(pg_control_path, "private-secret-title", tier="private")
    for tier, visible, hidden in [
        ("general", ["public"], 2),
        ("trusted", ["public", "trusted"], 1),
        ("private", ["public", "trusted", "private"], 0),
    ]:
        result = read_profile(
            pg_control_path, actor=ProfileActor("example", tier), now=NOW
        )
        assert result["withheld_count"] == hidden
        for name in ("public", "trusted", "private"):
            assert (name + "-secret-title" in result["bundle"]) == (
                name in visible and name != "public"
            )
        assert result["token_upper_bound"] <= 1500
    assert (
        "public-preference"
        not in read_profile(pg_control_path, "decision", now=NOW)["bundle"]
    )
    with pytest.raises(ValueError):
        read_profile(pg_control_path, "invalid")


def test_staleness_expiry_boundaries(pg_control_path):
    for layer, days in [("work", 14), ("decision", 30)]:
        item(pg_control_path, f"{layer}-boundary", layer=layer, age=days)
        item(pg_control_path, f"{layer}-stale", layer=layer, age=days + 0.00001)
        item(
            pg_control_path,
            f"{layer}-hidden-stale",
            layer=layer,
            age=days + 1,
            tier="private",
        )
    item(pg_control_path, "expired", expired=True)
    item(pg_control_path, "standing", kind="rule", age=100)
    result = read_profile(pg_control_path, now=NOW)
    assert "boundary" in result["bundle"] and "stale" not in result["bundle"]
    assert "expired" not in result["bundle"] and "standing" in result["bundle"]
    assert result["withheld_count"] == 0


def test_budget_and_order(pg_control_path):
    item(pg_control_path, "界" * 1000, item_id="oversized", kind="rule")
    item(pg_control_path, "rule-first", item_id="a", kind="rule")
    item(pg_control_path, "small-preference", item_id="b")
    for i in range(20):
        item(
            pg_control_path, f"{i:02}: " + "x" * 200, item_id=f"work-{i}", layer="work"
        )
    first = read_profile(pg_control_path, now=NOW)
    assert first == read_profile(pg_control_path, now=NOW)
    assert first["truncated"]
    assert len(first["bundle"].encode()) <= 1500
    assert "界" not in first["bundle"]
    assert first["bundle"].index("rule-first") < first["bundle"].index(
        "small-preference"
    )
    assert first["bundle"].index("00:") < first["bundle"].index("01:")


def test_briefs_with_empty_and_unavailable_contexts(pg_control_path):
    with postgres_control_store(pg_control_path).connection() as con:
        con.execute(
            "INSERT INTO project_briefs (project_key, repo_owner, repo_name, brief_md, "
            "next_steps_md, last_activity_at) VALUES ('example/project', 'example', "
            "'project', 'Active implementation', 'Review', ?)",
            [NOW],
        )
    result = read_profile(pg_control_path, now=NOW)
    assert result["context_status"] == "unavailable"
    assert "Active implementation" in result["bundle"]
    import duckdb

    from drover.schema import _CONTEXT_CONTAINERS_DDL

    with duckdb.connect(str(pg_control_path)) as con:
        con.execute(_CONTEXT_CONTAINERS_DDL)
    result = read_profile(pg_control_path, now=NOW)
    assert result["context_status"] == "ok"
    assert "Review" in result["bundle"]


def test_context_privacy_and_freshness(pg_control_path, monkeypatch):
    rows = [
        dict(
            context_id="a",
            label="Public work",
            container_type="code_project",
            redaction_policy="session-summary-redacted",
            last_touched_at=NOW,
        ),
        dict(
            context_id="b",
            label="Hidden personal title",
            container_type="personal_project",
            last_touched_at=NOW,
        ),
        dict(context_id="c", label="Stale", last_touched_at=NOW - timedelta(days=15)),
    ]
    monkeypatch.setattr("drover.server.profile._contexts", lambda _: (rows, "ok"))
    result = read_profile(pg_control_path, now=NOW)
    assert "Public work" in result["bundle"]
    assert "Hidden" not in json.dumps(result) and "Stale" not in result["bundle"]
    assert result["withheld_count"] == 1
    assert (
        "Hidden"
        in read_profile(
            pg_control_path, actor=ProfileActor("user", "private"), now=NOW
        )["bundle"]
    )


def test_verified_credential_registry(pg_control_path):
    store = PostgresCredentialStore(pg_control_path)
    cred, token = store.issue(scope="profile", label="example-agent")
    auth = AuthSettings(True, "example-operator-token", credentials=store)
    with postgres_control_store(pg_control_path).connection() as con:
        con.execute(
            "INSERT INTO profile_agents (agent_id, credential_id, tier, updated_by) "
            "VALUES ('example-agent', ?, 'trusted', 'operator')",
            [cred.id],
        )
    assert (
        http_actor(pg_control_path, auth, {"Authorization": f"Bearer {token}"}).tier
        == "trusted"
    )
    assert (
        http_actor(pg_control_path, auth, {"X-Agent": "example-agent"}).tier
        == "general"
    )
    assert http_actor(pg_control_path, DISABLED, {}).tier == "general"
    store.revoke(cred.id)
    assert (
        http_actor(pg_control_path, auth, {"Authorization": f"Bearer {token}"}).tier
        == "general"
    )


def test_http_and_mcp_reads(pg_control_path):
    item(pg_control_path, "private-marker", tier="private")
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=SimpleNamespace(duckdb_path=pg_control_path),
        auth=AuthSettings(True, "example-operator-token"),
    )
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/profile",
            headers={"Authorization": "Bearer example-operator-token"},
        )
        assert "private-marker" in json.load(urlopen(request))["bundle"]
    finally:
        server.shutdown()
        server.server_close()
    mcp = build_mcp_server(duckdb_path=pg_control_path)
    response = asyncio.run(mcp.call_tool("drover_profile", {}))
    content = response[0] if isinstance(response, tuple) else response
    result = json.loads(content[0].text)
    assert result["withheld_count"] == 1
    assert "private-marker" not in json.dumps(result)


def test_profile_mcp_preserves_rendered_freshness(pg_control_path):
    item(pg_control_path, "Standing rule", kind="rule", age=100)
    with postgres_control_store(pg_control_path).connection() as con:
        con.execute(
            "INSERT INTO project_briefs (project_key, repo_owner, repo_name, brief_md, "
            "generated_at) VALUES ('example/unrelated', 'example', 'unrelated', "
            "'Unrelated activity', ?)",
            [NOW],
        )
    mcp = build_mcp_server(duckdb_path=pg_control_path)
    response = asyncio.run(mcp.call_tool("drover_profile", {"scope": "user"}))
    content = response[0] if isinstance(response, tuple) else response
    result = json.loads(content[0].text)
    assert result["store"] == "hub"
    assert result["store_authoritative"] is True
    assert result["data_watermark"] == {
        "timestamp": (NOW - timedelta(days=100)).isoformat(),
        "basis": "rendered_profile_source_at",
    }


def test_profile_consumes_continuity_producer(pg_control_path, tmp_path):
    from memory_helpers import put_summary

    from drover.schema import bootstrap
    from drover.server.context_writer import ContextContainerWriter

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=pg_control_path)
    for session_id, content, project, activity in (
        ("example-code", "Produced code context", "example/project", NOW),
        ("example-general", "Unclassified private context", None, NOW),
        (
            "example-stale",
            "Stale produced context",
            "example/old",
            NOW - timedelta(days=15),
        ),
    ):
        put_summary(
            pg_control_path,
            session_id,
            project_key=project,
            summary_md=content,
            generated_at=NOW,
            ended_at=activity,
        )
    assert ContextContainerWriter(pg_control_path).run_once(apply=True)["applied"] == 3
    result = read_profile(pg_control_path, now=NOW)
    assert "Produced code context" in result["bundle"]
    assert "Unclassified private context" not in json.dumps(result)
    assert "Stale produced context" not in result["bundle"]
    assert result["withheld_count"] == 1
    assert result["data_watermark"]["timestamp"] == NOW.isoformat()


def test_rendered_oldest_age_ignores_hidden_stale_and_omitted(pg_control_path):
    item(pg_control_path, "Old rule", kind="rule", age=100)
    item(pg_control_path, "Recent preference", age=2)
    item(pg_control_path, "Hidden older rule", kind="rule", tier="private", age=200)
    item(pg_control_path, "x" * 2000, kind="rule", age=300)
    item(pg_control_path, "Stale work", layer="work", age=15)
    result = read_profile(pg_control_path, now=NOW)
    assert result["oldest_item_age_seconds"] == 100 * 86400
    assert (
        result["data_watermark"]["timestamp"] == (NOW - timedelta(days=2)).isoformat()
    )
    assert result["truncated"] is True
    assert "Hidden" not in json.dumps(result)


def test_empty_profile_freshness_is_unknown_on_both_transports(pg_control_path):
    with postgres_control_store(pg_control_path).connection() as con:
        con.execute(
            "INSERT INTO project_briefs (project_key, repo_owner, repo_name, brief_md, generated_at) "
            "VALUES ('example/unrelated', 'example', 'unrelated', 'Unrelated', ?)",
            [NOW],
        )
    http = read_profile(pg_control_path, "user", now=NOW)
    mcp = build_mcp_server(duckdb_path=pg_control_path)
    response = asyncio.run(mcp.call_tool("drover_profile", {"scope": "user"}))
    content = response[0] if isinstance(response, tuple) else response
    result = json.loads(content[0].text)
    for value in (http, result):
        assert value["oldest_item_age_seconds"] is None
        assert value["data_watermark"] == {"timestamp": None, "basis": "unknown"}


def test_future_item_age_clamps_to_zero(pg_control_path):
    item(pg_control_path, "Future preference", age=-1)
    assert read_profile(pg_control_path, now=NOW)["oldest_item_age_seconds"] == 0


def test_single_call_http_trusted_bundle_and_revocation(pg_control_path):
    from drover.server.profile import issue_agent_credential, revoke_agent_credential

    operator = ProfileActor("operator", "private", True)
    issued = issue_agent_credential(
        pg_control_path, "example-agent", "trusted", actor=operator
    )
    auth = AuthSettings(
        True,
        "",
        credentials=PostgresCredentialStore(pg_control_path),
        legacy_token_enabled=False,
    )
    item(pg_control_path, "General preference")
    item(pg_control_path, "Trusted preference", tier="trusted")
    item(pg_control_path, "Private preference", tier="private")
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=SimpleNamespace(duckdb_path=pg_control_path),
        auth=auth,
    )
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/profile",
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        with urlopen(request) as response:
            result = json.load(response)
        assert "General preference" in result["bundle"]
        assert "Trusted preference" in result["bundle"]
        assert "Private preference" not in json.dumps(result)
        assert result["withheld_count"] == 1
        assert result["token_upper_bound"] == len(result["bundle"].encode()) <= 1500
        assert result["oldest_item_age_seconds"] >= 0
        revoke_agent_credential(pg_control_path, "example-agent", actor=operator)
        from urllib.error import HTTPError

        with pytest.raises(HTTPError) as exc:
            urlopen(request)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
