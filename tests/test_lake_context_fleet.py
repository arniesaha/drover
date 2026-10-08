"""Context/fleet selection uses only disposable catalogs and PG fixtures."""

from dataclasses import replace

import pytest
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from drover.server.lake.serving import configure_analytics


def test_context_missing_coverage_is_bound_and_legacy_remains_authoritative(
    verified_lake, monkeypatch
):
    from drover.config import AnalyticsConfig
    from drover.server.db import open_duckdb_connection
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    with open_duckdb_connection(path) as con:
        con.execute(
            "INSERT INTO context_containers(context_id,label,container_type) VALUES ('ctx','legacy context','research_thread')"
        )
    assert (
        tools.drover_context_brief(duckdb_path=path, context_id="ctx")["label"]
        == "legacy context"
    )
    configure_analytics(path, config)
    monkeypatch.setattr(tools, "_connect", lambda *a, **kw: pytest.fail("legacy read"))
    for tool, options in (
        (tools.drover_recent_contexts, {}),
        (tools.drover_context_brief, {"context_id": "ctx"}),
        (tools.drover_open_loops, {}),
        (tools.drover_resume_context, {"context_id": "ctx"}),
    ):
        result = tool(duckdb_path=path, **options)
        assert result["status"] == "unavailable"
        assert result["reason"] == "analytics_context_projection_unavailable"
        assert result["metadata"]["binding"]["epoch"] == config.epoch
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    failed = tools.drover_recent_contexts(duckdb_path=path)
    assert failed["status"] == "unavailable"
    assert "verification" in failed["reason"]
    configure_analytics(path, AnalyticsConfig())


def test_fleet_pg_routing_identity_epoch_and_bounds(verified_lake, monkeypatch):
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    legacy = tools.drover_fleet_status(duckdb_path=path)
    configure_analytics(path, config)
    assert tools.drover_fleet_status(duckdb_path=path)["count"] == legacy["count"] == 0
    registry = HarnessRegistry(path)
    registry.register_host(host_id="live", display_name="Test", kind="test")
    registry.create_session(
        host_id="live",
        harness="codex",
        command="codex",
        session_id="active",
        repo_owner="o",
        repo_name="r",
        branch="main",
    )
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET status='running' WHERE session_id='active'"
        )
    monkeypatch.setattr(tools, "_connect", lambda *a, **kw: pytest.fail("legacy fleet"))
    result = tools.drover_fleet_status(duckdb_path=path)
    assert result["count"] == 1
    assert result["active_sessions"][0]["repo_owner"] == "o"
    assert result["state_source"] == "control_plane.harness_sessions+harness_hosts"
    assert result["control_store"] == "postgres"
    assert result["authoritative"] is True
    assert result["store"] == "hub"
    assert result["store_authoritative"] is True
    # The MCP fleet response uses live control state. Lake binding metadata is
    # the separate analytical read-model contract, not a fleet response field.
    from drover.server.lake import read_models

    binding = read_models.read_model(path, "fleet")["metadata"]["binding"]
    configure_analytics(path, replace(config, epoch="renewed"))
    renewed = read_models.read_model(path, "fleet")
    assert renewed["metadata"]["binding"]["epoch"] == "renewed"
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET native_session_id='native-new' WHERE session_id='active'"
        )
    changed = read_models.read_model(path, "fleet")
    assert changed["metadata"]["binding"]["identities"] != binding["identities"]
    for kind in ("native_publication", "native_usage"):
        assert (
            changed["metadata"][kind]["coverage_binding"]
            == changed["metadata"]["binding"]
        )
        assert changed["metadata"][kind]["freshness"] == "unavailable"

    from drover.server.lake import read_models
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError, match="analytics_row_limit_exceeded"):
        read_models.read_model(path, "fleet", limit=0)
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    assert tools.drover_fleet_status(duckdb_path=path)["status"] == "unavailable"


def test_native_freshness_metadata_is_identity_epoch_bound(verified_lake):
    from drover.server.db import control_plane_connection
    from drover.server.lake.read_models import read_model

    _, path, config = verified_lake
    configure_analytics(path, config)
    with control_plane_connection(path) as con:
        con.execute(
            "INSERT INTO native_usage_partition_watermarks(partition_date,source_activity_at,rolled_at) VALUES ('2026-10-01',now(),now())"
        )
    first = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)[
        "metadata"
    ]
    for kind in ("native_publication", "native_usage"):
        assert first[kind]["freshness"] == "unavailable"
        assert first[kind]["generation"] is None
        assert first[kind]["coverage_binding"] == first["binding"]
    configure_analytics(path, replace(config, epoch="changed"))
    second = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)[
        "metadata"
    ]
    assert second["native_usage"]["coverage_binding"]["epoch"] == "changed"
    assert first["native_usage"]["observed_at"] == second["native_usage"]["observed_at"]
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE native_usage_partition_watermarks SET source_activity_at='2026-01-01',rolled_at='2026-01-01'"
        )
    stale = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)[
        "metadata"
    ]
    assert stale["native_usage"]["freshness"] == "unavailable"
    assert stale["native_usage"]["generation"] is None
    spec = verified_lake[0]
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError, match="verification"):
        read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)


def test_fleet_response_bounds_and_retired_closed_exclusion(verified_lake):
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake.read_models import read_model
    from drover.server.lake.runtime import LakeError
    from drover.server.mcp import tools

    _, path, config = verified_lake
    configure_analytics(path, config)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="host", display_name="Host", kind="test")
    for sid in ("a", "b"):
        registry.create_session(
            host_id="host", harness="codex", command="codex", session_id=sid
        )
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET status='running' WHERE session_id IN ('a','b')"
        )
    with pytest.raises(LakeError, match="analytics_row_limit_exceeded"):
        read_model(path, "fleet", limit=1)
    with control_plane_connection(path) as con:
        con.execute("UPDATE harness_sessions SET ended_at=now() WHERE session_id='b'")
    result = tools.drover_fleet_status(duckdb_path=path)
    assert [r["session_id"] for r in result["active_sessions"]] == ["a"]
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET repo_owner=repeat('x',1100000) WHERE session_id='a'"
        )
    with pytest.raises(LakeError, match="analytics_byte_limit_exceeded"):
        read_model(path, "fleet")
    bounded_fleet = tools.drover_fleet_status(duckdb_path=path)
    from drover.server.mcp.contract import READ_CAPS, serialized_bytes

    assert bounded_fleet["truncated"] is True
    assert [r["session_id"] for r in bounded_fleet["active_sessions"]] == ["a"]
    assert (
        serialized_bytes(bounded_fleet)
        <= READ_CAPS["drover_fleet_status"].response_bytes
    )
    with control_plane_connection(path) as con:
        con.execute("UPDATE harness_sessions SET repo_owner='o' WHERE session_id='a'")
        con.execute("UPDATE harness_hosts SET retired_at=now() WHERE host_id='host'")
    assert tools.drover_fleet_status(duckdb_path=path)["count"] == 0


@pytest.mark.parametrize("change", ["epoch", "identity"])
def test_read_model_rejects_changed_binding_before_return(
    verified_lake, monkeypatch, change
):
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.lake import read_models
    from drover.server.lake.runtime import LakeError

    _, path, config = verified_lake
    configure_analytics(path, config)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="host", display_name="Test", kind="test")
    registry.create_session(
        host_id="host", harness="codex", command="codex", session_id="bound"
    )
    original = read_models.query

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "epoch":
            configure_analytics(path, replace(config, epoch="mid-read"))
        else:
            with control_plane_connection(path) as con:
                con.execute(
                    "UPDATE harness_sessions SET native_session_id='changed' WHERE session_id='bound'"
                )
        return result

    monkeypatch.setattr(read_models, "query", changed)
    with pytest.raises(LakeError, match="analytics_read_model_changed"):
        read_models.read_model(path, "fleet")


def test_repository_context_uses_bound_unavailable_coverage(verified_lake, monkeypatch):
    from drover.server.mcp import tools
    from drover.server.recall_bundle import RecallBundleService

    _, path, config = verified_lake
    configure_analytics(path, config)
    monkeypatch.setattr(
        tools, "_connect", lambda *a, **kw: pytest.fail("legacy context")
    )
    result = RecallBundleService(duckdb_path=path).recall_bundle("remember", repo="o/r")
    assert result["reason"] == "analytics_context_projection_unavailable"
    assert result["metadata"]["binding"]["epoch"] == config.epoch


def test_fleet_parity_for_registry_backed_active_sessions(verified_lake):
    from datetime import datetime, timedelta, timezone

    from drover.config import AnalyticsConfig
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.mcp import tools

    _, path, config = verified_lake
    registry = HarnessRegistry(path)
    registry.register_host(host_id="test", display_name="Test", kind="test")
    now = datetime.now(timezone.utc)
    for index, sid in enumerate(("s", "other")):
        registry.create_session(
            host_id="test",
            harness="codex",
            command="codex",
            session_id=sid,
            repo_owner="o",
            repo_name="r",
            branch="main",
            started_at=now - timedelta(minutes=5 + index),
        )
        with control_plane_connection(path) as con:
            con.execute(
                "UPDATE harness_sessions SET status='running',last_activity=? WHERE session_id=?",
                [now - timedelta(minutes=index), sid],
            )
    legacy = tools.drover_fleet_status(duckdb_path=path)["active_sessions"]
    assert len(legacy) == 2
    configure_analytics(path, config)
    lake = tools.drover_fleet_status(duckdb_path=path)["active_sessions"]
    fields = (
        "session_id",
        "agent_id",
        "repo_owner",
        "repo_name",
        "branch",
        "started_at",
        "last_activity",
        "task_id",
    )

    def facts(rows):
        return [
            {
                k: (
                    datetime.fromisoformat(r[k]).astimezone(timezone.utc)
                    if k in ("started_at", "last_activity")
                    else r[k]
                )
                for k in fields
            }
            for r in rows
        ]

    assert facts(lake) == facts(legacy)
    assert all(r["task_id"] and r["host_liveness"] == "online" for r in lake)
    configure_analytics(path, AnalyticsConfig())
    assert tools.drover_fleet_status(duckdb_path=path)["active_sessions"] == legacy


def test_writer_certifies_summary_containers_without_legacy_mix(
    verified_lake, monkeypatch
):
    from datetime import datetime, timezone

    from memory_helpers import put_brief, put_summary

    from drover.server.context_writer import ContextContainerWriter, _key
    from drover.server.db import control_plane_connection
    from drover.server.lake import coverage
    from drover.server.mcp import tools

    _, path, config = verified_lake
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    put_summary(
        path,
        "s",
        project_key="o/r",
        summary_md="Implement recall",
        next_steps_md="Review",
        generated_at=stamp,
    )
    put_brief(
        path,
        "o/r",
        brief_md="Project continuity",
        source_session_id="s",
        generated_at=stamp,
    )
    configure_analytics(path, config)
    coverage.provision_coverage(path)
    writer = ContextContainerWriter(path)
    monkeypatch.setattr(
        "drover.server.context_writer.open_duckdb_connection",
        lambda *a, **kw: pytest.fail("legacy writer"),
    )
    assert writer.run_once()["created"] == 2
    assert tools.drover_recent_contexts(duckdb_path=path)["status"] == "unavailable"
    assert writer.run_once(apply=True)["applied"] == 2
    recent = tools.drover_recent_contexts(duckdb_path=path)
    assert len(recent["contexts"]) == 2
    brief = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("session", "s")
    )
    assert brief["label"] == "Implement recall"
    assert brief["redaction_policy"] == "session-summary-redacted"
    assert len(tools.drover_open_loops(duckdb_path=path)["open_loops"]) == 1
    resume = tools.drover_resume_context(
        duckdb_path=path, context_id=_key("project", "o/r")
    )
    assert resume["session_summaries"][0]["summary_md"] == "Implement recall"
    with control_plane_connection(path) as pg:
        before = pg.execute("SELECT COUNT(*) FROM lake_coverage_sources").fetchone()[0]
    assert writer.run_once()["unchanged"] == 2
    with control_plane_connection(path) as pg:
        assert (
            pg.execute("SELECT COUNT(*) FROM lake_coverage_sources").fetchone()[0]
            == before
        )
    assert writer.run_once(apply=True)["unchanged"] == 2
    assert (
        tools.drover_recent_contexts(duckdb_path=path)["contexts"] == recent["contexts"]
    )
