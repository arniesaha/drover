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
    assert result["status_source"] == "postgres_registry"
    binding = result["metadata"]["binding"]
    configure_analytics(path, replace(config, epoch="renewed"))
    renewed = tools.drover_fleet_status(duckdb_path=path)
    assert renewed["metadata"]["binding"]["epoch"] == "renewed"
    with control_plane_connection(path) as con:
        con.execute(
            "UPDATE harness_sessions SET native_session_id='native-new' WHERE session_id='active'"
        )
    changed = tools.drover_fleet_status(duckdb_path=path)
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
    assert (
        tools.drover_fleet_status(duckdb_path=path)["reason"]
        == "analytics_byte_limit_exceeded"
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


@pytest.fixture
def recent_event_seed(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import test_mcp_tools

    original = test_mcp_tools._write_agent_events
    now = datetime.now(timezone.utc)

    def write(parquet, rows):
        for index, row in enumerate(rows):
            row["timestamp"] = now - timedelta(minutes=4 - index)
        original(parquet, rows)

    monkeypatch.setattr(test_mcp_tools, "_write_agent_events", write)


@pytest.fixture
def fleet_lake(recent_event_seed, verified_lake):
    return verified_lake


def test_fleet_parity_for_registry_backed_active_sessions(fleet_lake):
    from datetime import datetime

    from drover.config import AnalyticsConfig
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.mcp import tools

    _, path, config = fleet_lake
    legacy = tools.drover_fleet_status(duckdb_path=path)["active_sessions"]
    assert len(legacy) == 2
    registry = HarnessRegistry(path)
    registry.register_host(host_id="test", display_name="Test", kind="test")
    for row in legacy:
        registry.create_session(
            host_id="test",
            harness="codex",
            command="codex",
            session_id=row["session_id"],
            repo_owner=row["repo_owner"],
            repo_name=row["repo_name"],
            branch=row["branch"],
            started_at=datetime.fromisoformat(row["started_at"]),
        )
        with control_plane_connection(path) as con:
            con.execute(
                "UPDATE harness_sessions SET status='running',last_activity=? WHERE session_id=?",
                [datetime.fromisoformat(row["last_event_at"]), row["session_id"]],
            )
    configure_analytics(path, config)
    lake = tools.drover_fleet_status(duckdb_path=path)["active_sessions"]
    fields = (
        "session_id",
        "agent_id",
        "repo_owner",
        "repo_name",
        "branch",
        "started_at",
        "last_event_at",
    )
    from datetime import timezone

    def facts(rows):
        return [
            {
                k: (
                    datetime.fromisoformat(r[k]).astimezone(timezone.utc)
                    if k in ("started_at", "last_event_at")
                    else r[k]
                )
                for k in fields
            }
            for r in rows
        ]

    assert facts(lake) == facts(legacy)
    assert all(r["event_count"] is None and r["task_id"] is None for r in lake)
    configure_analytics(path, AnalyticsConfig())
    assert tools.drover_fleet_status(duckdb_path=path)["active_sessions"] == legacy
