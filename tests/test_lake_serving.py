"""Backend selection never guesses or silently falls back to stale history."""

from dataclasses import replace

import pytest
from test_control_outbox import postgres_control_store
from test_lake_exporter import export_lake, seed
from test_lake_runtime import lake_spec

from drover.config import AnalyticsConfig, default_config
from drover.server.lake.runtime import LakeError, lake_connection
from drover.server.lake.serving import (
    configure_analytics,
    open_history,
    selected_config,
)


def test_selection_defaults_and_validation(tmp_path):
    assert default_config().analytics.backend == "legacy"
    assert not default_config().analytics.exporter_enabled
    assert selected_config(tmp_path / "db").backend == "legacy"
    with pytest.raises(ValueError):
        AnalyticsConfig(backend="automatic")
    with pytest.raises(ValueError):
        AnalyticsConfig(exporter_enabled=True)


def test_legacy_authoritative_even_with_lake_options(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "legacy.db"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE agent_events(content VARCHAR)")
    con.execute("INSERT INTO agent_events VALUES ('legacy authoritative')")
    con.close()
    configure_analytics(path, AnalyticsConfig(data_root="/does/not/exist"))
    with open_history(path) as con:
        assert con.execute("SELECT content FROM agent_events").fetchone() == (
            "legacy authoritative",
        )


def test_unverified_lake_cannot_open_or_fallback(
    export_lake, postgres_control_store, tmp_path
):
    path, _ = postgres_control_store
    config = AnalyticsConfig(
        backend="ducklake",
        data_root=str(export_lake.data_root),
        extension_dir=str(export_lake.extension_dir),
        engine_sha256=export_lake.engine_sha256,
        catalog_dsn_env=export_lake.catalog_dsn_env,
        epoch="fixture",
        verification_sha256="0" * 64,
    )
    configure_analytics(path, config)
    with pytest.raises(LakeError, match="verification"):
        with open_history(path) as con:
            con.execute("SELECT * FROM agent_events")


@pytest.fixture
def verified_lake(lake_spec, postgres_control_store, tmp_path):
    import hashlib
    import tarfile
    from datetime import datetime, timezone

    import pyarrow as pa
    import pyarrow.parquet as pq
    from test_mcp_tools import _write_agent_events

    from drover.schema import bootstrap
    from drover.server.lake.rebuild import rebuild, verify

    path, parquet = postgres_control_store
    rows = [
        dict(
            id="older",
            session_id="s",
            agent_id="test",
            timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
            event_type="user_message",
            role="user",
            content="remember lake",
            dedup_key="k",
            raw_data="{}",
        ),
        dict(
            id="newer",
            session_id="s",
            agent_id="test",
            timestamp=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
            event_type="assistant_message",
            role="assistant",
            content="remember answer",
            dedup_key="k2",
            repo_owner="o",
            repo_name="r",
            branch="main",
            raw_data="{}",
        ),
        dict(
            id="loser",
            session_id="s",
            agent_id="test",
            timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
            event_type="assistant_message",
            role="assistant",
            content="duplicate loser",
            dedup_key="k2",
            raw_data="{}",
        ),
    ]
    rows.append(
        dict(
            id="raw-repo",
            session_id="raw-only",
            agent_id="test",
            timestamp=datetime(2026, 10, 1, 2, tzinfo=timezone.utc),
            event_type="assistant_message",
            role="assistant",
            content="remember raw attribution",
            dedup_key="raw-repo",
            raw_data='{"_repo_owner":"raw-owner","_repo_name":"raw-name"}',
        )
    )
    _write_agent_events(parquet, rows)
    for table, row in [
        ("provider_usage_snapshots", {"snapshot_id": "p"}),
        ("control_outbox_batches", {"event_id": "frozen"}),
    ]:
        folder = parquet / table
        folder.mkdir(exist_ok=True)
        pq.write_table(pa.Table.from_pylist([row]), folder / "part.parquet")
    bootstrap(parquet_dir=parquet, duckdb_path=path)

    archive = tmp_path / "fixture.tar"
    with tarfile.open(archive, "w") as tar:
        for table in (
            "agent_events",
            "provider_usage_snapshots",
            "control_outbox_batches",
        ):
            tar.add(parquet / table, arcname="parquet/" + table)
    spec = replace(lake_spec, data_root=tmp_path / "rebuilt")
    rebuild(archive, spec)
    verify(spec)
    digest = hashlib.sha256(
        (spec.data_root / "verification/serving-proof.json").read_bytes()
    ).hexdigest()
    config = AnalyticsConfig(
        backend="ducklake",
        data_root=str(spec.data_root),
        extension_dir=str(spec.extension_dir),
        engine_sha256=spec.engine_sha256,
        catalog_dsn_env=spec.catalog_dsn_env,
        epoch="test-epoch",
        verification_sha256=digest,
    )
    return spec, path, config


def test_tar_rebuild_restart_selection_reads_and_starts_exporter(verified_lake):
    """The rebuilt tar fixture is sufficient for a restart-selected lake hub."""
    import threading

    from drover.server.lake.exporter import provision_exporter
    from drover.server.lake.lifecycle import selected_exporter
    from drover.server.lake.serving import check_selected

    spec, path, config = verified_lake
    provision_exporter(spec)
    import hashlib

    from drover.server.lake.rebuild import verify

    verify(spec)
    config = replace(
        config,
        verification_sha256=hashlib.sha256(
            (spec.data_root / "verification/serving-proof.json").read_bytes()
        ).hexdigest(),
    )
    configure_analytics(path, config)
    check_selected(path)
    exporter = selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=config)
    )
    try:
        exporter.start(shutdown_event=threading.Event())
        assert exporter.health()["enabled"]
    finally:
        exporter.stop()


def test_canonical_read_parity_and_no_legacy_open(verified_lake, monkeypatch):
    from memory_helpers import put_summary

    from drover.server.mcp import tools
    from drover.server.summarizer.derive import select_substantive_window
    from drover.server.summarizer.worker import (
        _open_summarizer_db,
        _session_agent_events_ctes,
    )

    spec, path, config = verified_lake
    put_summary(path, "s", summary_md="remember lake summary", project_key="o/r")
    calls = [
        (tools.drover_session_replay, dict(session_id="s")),
        (tools.drover_session_summary, dict(session_id="s")),
        (tools.drover_search, dict(query="remember", session_id="s")),
        (tools.drover_files_touched, dict(session_id="s")),
        (tools.drover_recall, dict(query="remember", session_id="s")),
        (tools.drover_recall, dict(query="remember", repo_owner="o", repo_name="r")),
    ]
    expected = [function(duckdb_path=path, **kwargs) for function, kwargs in calls]
    with _open_summarizer_db(path) as con:
        legacy_window = select_substantive_window(
            con, _session_agent_events_ctes(), "s"
        )
    configure_analytics(path, config)

    def forbidden(*args, **kwargs):
        pytest.fail("selected lake must never open legacy analytics")

    monkeypatch.setattr(tools, "_connect", forbidden)

    def normalized(value):
        from datetime import datetime, timezone

        if isinstance(value, dict):
            return {
                key: (
                    datetime.fromisoformat(item).astimezone(timezone.utc).isoformat()
                    if key == "timestamp" and isinstance(item, str)
                    else normalized(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [normalized(item) for item in value]
        return value

    for (function, kwargs), result in zip(calls, expected):
        assert normalized(function(duckdb_path=path, **kwargs)) == normalized(result)
    with _open_summarizer_db(path) as con:
        fields = (
            "id",
            "session_id",
            "role",
            "content",
            "timestamp",
            "event_type",
            "raw_data",
            "agent_id",
        )
        project = lambda events: [
            {key: event.get(key) for key in fields} for event in events
        ]
        assert project(
            select_substantive_window(con, _session_agent_events_ctes(), "s")
        ) == project(legacy_window)


def test_catalog_change_and_proof_tamper_fail_closed(verified_lake):
    from drover.server.mcp.tools import drover_recall, drover_session_replay

    spec, path, config = verified_lake
    configure_analytics(path, config)
    assert drover_session_replay(duckdb_path=path, session_id="s")["status"] == "ok"
    with lake_connection(spec, read_only=False) as con:
        con.execute("DELETE FROM lake.agent_events WHERE id='newer'")
    for function, kwargs in [
        (drover_session_replay, dict(session_id="s")),
        (drover_recall, dict(query="remember")),
    ]:
        result = function(duckdb_path=path, **kwargs)
        assert result["status"] == "unavailable"
        assert result["analytics_backend"] == "ducklake"
        assert "events" not in result and not result.get("results")
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    with pytest.raises(LakeError, match="verification"):
        with open_history(path) as con:
            con.execute("SELECT 1")


def test_export_lifecycle_selected_by_ducklake_and_fails_closed(
    verified_lake, monkeypatch
):
    import threading

    from drover.server.lake.exporter import provision_exporter
    from drover.server.lake.fence import MutationFence
    from drover.server.lake.lifecycle import selected_exporter

    spec, path, config = verified_lake
    cfg = replace(default_config(), duckdb_path=path, analytics=config)
    assert selected_exporter(cfg).__class__.__name__ == "ExporterLifecycle"
    provision_exporter(spec)
    # Provisioning changes the snapshot; requires an explicit new verification.
    monkeypatch.setenv("DROVER_TEST_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config, exporter_enabled=True, exporter_dsn_env="DROVER_TEST_EXPORT_DSN"
    )
    configure_analytics(path, enabled)
    lifecycle = selected_exporter(replace(cfg, analytics=enabled))
    with pytest.raises(LakeError, match="verification"):
        lifecycle.start(shutdown_event=threading.Event())
    assert not lifecycle.health()["enabled"]
    with MutationFence(spec.dsn()):
        pass  # failure released the dedicated owner connection


def test_receipted_export_extends_verified_baseline(verified_lake, monkeypatch):
    import hashlib
    import threading
    import time

    from drover.server.lake.exporter import provision_exporter
    from drover.server.lake.lifecycle import selected_exporter
    from drover.server.lake.rebuild import verify
    from drover.server.mcp.tools import drover_session_replay

    spec, path, config = verified_lake
    provision_exporter(spec)
    verify(spec)
    digest = hashlib.sha256(
        (spec.data_root / "verification/serving-proof.json").read_bytes()
    ).hexdigest()
    monkeypatch.setenv("DROVER_TEST_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config,
        verification_sha256=digest,
        exporter_enabled=True,
        exporter_dsn_env="DROVER_TEST_EXPORT_DSN",
    )
    configure_analytics(path, enabled)
    seed(path, count=1)
    lifecycle = selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=enabled)
    )
    try:
        lifecycle.start(shutdown_event=threading.Event())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = drover_session_replay(
                duckdb_path=path, session_id="export-session"
            )
            if result["status"] == "ok":
                break
            time.sleep(0.1)
        assert result["status"] == "ok", result
        assert result["events"][0]["content"] == "full substantive payload 0"
        assert result["events"][0]["source"] == "control"
        assert lifecycle.health()["enabled"]
        replacement = selected_exporter(
            replace(default_config(), duckdb_path=path, analytics=enabled)
        )
        with pytest.raises(LakeError, match="mutation_fenced"):
            replacement.start(shutdown_event=threading.Event())
        # The PG alias must resolve to the same canonical control history.
        from drover.server.db import control_plane_connection

        with control_plane_connection(path) as control:
            control.execute(
                "UPDATE harness_sessions SET native_session_id='native-alias' WHERE session_id='export-session'"
            )
        alias = drover_session_replay(duckdb_path=path, session_id="native-alias")
        assert alias["session_id"] == "export-session"
        assert alias["events"] == result["events"]
    finally:
        lifecycle.stop()


def test_query_failure_is_explicit_unavailable_without_legacy_retry(
    verified_lake, monkeypatch
):
    from drover.server.lake import serving
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    configure_analytics(path, config)

    def failed(*args, **kwargs):
        raise LakeError("analytics_deadline_exceeded")

    monkeypatch.setattr(serving, "query", failed)
    monkeypatch.setattr(
        tools, "_connect", lambda *a, **kw: pytest.fail("legacy fallback")
    )
    for function, kwargs in [
        (tools.drover_session_replay, dict(session_id="s")),
        (tools.drover_search, dict(query="remember")),
        (tools.drover_recall, dict(query="remember")),
    ]:
        result = function(duckdb_path=path, **kwargs)
        assert {
            key: result[key]
            for key in ("status", "analytics_backend", "analytics_epoch", "reason")
        } == {
            "status": "unavailable",
            "analytics_backend": "ducklake",
            "analytics_epoch": "test-epoch",
            "reason": "analytics_deadline_exceeded",
        }
        assert result["store"] == "hub"
        assert result["data_watermark"] == {"timestamp": None, "basis": "unknown"}
        assert result["truncated"] is False


def test_verify_refuses_extra_unaccounted_partition(verified_lake):
    from drover.server.lake.rebuild import verify

    spec, path, config = verified_lake
    with lake_connection(spec, read_only=False) as con:
        con.execute(
            "INSERT INTO lake.agent_events BY NAME SELECT 'unverified' AS id, '2026-10-02' AS date"
        )
    with pytest.raises(LakeError, match="unexpected_partition"):
        verify(spec)


def test_proof_cannot_authorize_a_different_catalog_identity(verified_lake):
    import hashlib
    import json

    spec, path, config = verified_lake
    file = spec.data_root / "verification/serving-proof.json"
    proof = json.loads(file.read_text())
    proof["catalog_id"] = "a-different-catalog"
    file.write_text(json.dumps(proof))
    configure_analytics(
        path,
        replace(
            config, verification_sha256=hashlib.sha256(file.read_bytes()).hexdigest()
        ),
    )
    with pytest.raises(LakeError, match="verification"):
        with open_history(path) as con:
            con.execute("SELECT 1")


def test_legacy_exporter_selection_never_activates_lake(tmp_path, monkeypatch):
    from drover.server import control_exporter
    from drover.server.lake.lifecycle import selected_exporter

    calls = []
    monkeypatch.setattr(
        control_exporter,
        "ControlOutboxExporter",
        lambda **kwargs: calls.append(kwargs) or "legacy",
    )
    cfg = replace(
        default_config(),
        duckdb_path=tmp_path / "legacy",
        parquet_dir=tmp_path / "parquet",
    )
    assert selected_exporter(cfg) == "legacy"
    assert calls == [
        dict(
            control_path=cfg.duckdb_path,
            analytical_path=cfg.duckdb_path,
            parquet_dir=cfg.parquet_dir,
            acknowledgement_retention_days=cfg.control_store.outbox_retention_days,
        )
    ]


def test_lifecycle_checks_its_explicit_config_without_path_registration(
    verified_lake, monkeypatch
):
    import threading

    from drover.server.lake.exporter import provision_exporter
    from drover.server.lake.lifecycle import selected_exporter

    spec, path, config = verified_lake
    provision_exporter(spec)
    monkeypatch.setenv("DROVER_TEST_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config, exporter_enabled=True, exporter_dsn_env="DROVER_TEST_EXPORT_DSN"
    )
    # No registered lake backend: startup must still reject its stale proof.
    configure_analytics(path, AnalyticsConfig())
    exporter = selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=enabled)
    )
    with pytest.raises(LakeError, match="verification"):
        exporter.start(shutdown_event=threading.Event())
    assert exporter.health()["last_error"] == "lake_verification_required"


def test_lifecycle_rejects_distinct_reader_exporter_catalogs(
    verified_lake, monkeypatch
):
    import threading

    from drover.server.lake import lifecycle
    from drover.server.lake.exporter import provision_exporter

    spec, path, config = verified_lake
    provision_exporter(spec)
    monkeypatch.setenv("DROVER_TEST_EXPORT_DSN", spec.dsn())
    enabled = replace(
        config, exporter_enabled=True, exporter_dsn_env="DROVER_TEST_EXPORT_DSN"
    )
    monkeypatch.setattr(
        lifecycle, "catalog_identity", lambda spec: spec.catalog_dsn_env
    )
    exporter = lifecycle.selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=enabled)
    )
    with pytest.raises(LakeError, match="catalog_mismatch"):
        exporter.start(shutdown_event=threading.Event())
    assert not exporter.health()["enabled"]


def test_recall_bundle_selected_failure_cannot_consult_legacy_context(
    verified_lake, monkeypatch
):
    from drover.server import recall_bundle as module
    from drover.server.lake import serving

    spec, path, config = verified_lake
    configure_analytics(path, config)

    def failed(*args, **kwargs):
        raise LakeError("analytics_deadline_exceeded")

    monkeypatch.setattr(serving, "query", failed)
    monkeypatch.setattr(
        module, "drover_open_loops", lambda **kw: pytest.fail("unported legacy context")
    )
    result = module.RecallBundleService(duckdb_path=path).recall_bundle("remember")
    assert result["status"] == "unavailable"
    assert result["reason"] == "analytics_deadline_exceeded"


def test_recall_bundle_unscoped_history_parity_and_repository_gate(
    verified_lake, monkeypatch
):
    from datetime import datetime, timezone

    from drover.server import recall_bundle as module
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    service = module.RecallBundleService(
        duckdb_path=path, clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    )
    expected = service.recall_bundle("remember", since="2026-10-01")
    configure_analytics(path, config)
    monkeypatch.setattr(
        tools, "_connect", lambda *a, **kw: pytest.fail("legacy context")
    )
    actual = service.recall_bundle("remember", since="2026-10-01")
    for bundle in (actual, expected):
        for item in bundle["drover_context"]["keyword_matches"]:
            item["source_timestamp"] = (
                datetime.fromisoformat(item["source_timestamp"])
                .astimezone(timezone.utc)
                .isoformat()
            )
    metadata = actual.pop("metadata")
    assert metadata["binding"]["epoch"] == config.epoch
    assert metadata["native_publication"]["freshness"] == "unavailable"
    assert actual == expected
    # Earlier day includes all three winners regardless of host timezone.
    actual = service.recall_bundle("remember", since="2026-09-30")
    assert len(actual["drover_context"]["keyword_matches"]) == 3
    assert (
        service.recall_bundle("remember", repo="o/r")["reason"]
        == "analytics_context_projection_unavailable"
    )


def test_verification_lost_fence_never_publishes_proof(verified_lake, monkeypatch):
    from drover.server.lake import fence, partition_rebuild, serving_proof

    spec, path, config = verified_lake
    owners = []

    class LosingFence(fence.MutationFence):
        def __enter__(self):
            result = super().__enter__()
            owners.append(self)
            return result

    monkeypatch.setattr(fence, "MutationFence", LosingFence)

    def lose_owner(spec):
        owners[0].connection.close()
        return {}

    monkeypatch.setattr(partition_rebuild, "_verify_partitioned", lose_owner)
    monkeypatch.setattr(
        serving_proof,
        "write_proof",
        lambda *a, **kw: pytest.fail("proof after fence loss"),
    )
    with pytest.raises(LakeError, match="fence_lost"):
        partition_rebuild.verify_partitioned(spec)


def test_missing_referenced_file_blocks_even_unrelated_history(verified_lake):
    from drover.server.mcp.tools import drover_session_replay

    spec, path, config = verified_lake
    configure_analytics(path, config)
    with lake_connection(spec) as con:
        file = con.execute(
            "SELECT data_file FROM ducklake_list_files('lake','provider_usage_snapshots')"
        ).fetchone()[0]
    from pathlib import Path

    referenced = Path(file)
    held = referenced.with_suffix(".held")
    referenced.rename(held)
    try:
        assert (
            drover_session_replay(duckdb_path=path, session_id="s")["status"]
            == "unavailable"
        )
    finally:
        held.rename(referenced)


def test_raw_json_repository_attribution_parity(verified_lake, monkeypatch):
    from memory_helpers import put_summary

    from drover.server.mcp import tools

    spec, path, config = verified_lake
    put_summary(
        path,
        "raw-only",
        summary_md="remember raw attribution",
        project_key="raw-owner/raw-name",
    )
    calls = [
        (tools.drover_search, dict(query="remember", repo="raw-owner/raw-name")),
        (
            tools.drover_recall,
            dict(query="remember", repo_owner="raw-owner", repo_name="raw-name"),
        ),
    ]
    expected = [function(duckdb_path=path, **kwargs) for function, kwargs in calls]
    configure_analytics(path, config)
    monkeypatch.setattr(
        tools, "_connect", lambda *a, **kw: pytest.fail("legacy attribution")
    )
    for (function, kwargs), result in zip(calls, expected):
        actual = function(duckdb_path=path, **kwargs)
        if function is tools.drover_search:
            from datetime import datetime, timezone

            for value in (actual, result):
                for row in value["results"]:
                    row["timestamp"] = (
                        datetime.fromisoformat(row["timestamp"])
                        .astimezone(timezone.utc)
                        .isoformat()
                    )
        assert actual == result
        assert actual["results"]


def test_summarize_session_paginates_canonical_events(verified_lake):
    import hashlib
    from datetime import datetime, timedelta, timezone

    from drover.collect.sources import write_events_jsonl
    from drover.models import AgentEvent
    from drover.server.ingest import ingest_file
    from drover.server.lake.exporter import LakeOutboxExporter, provision_exporter
    from drover.server.lake.rebuild import verify
    from drover.server.lake.serving import configure_analytics
    from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
    from drover.server.summarizer.worker import SummarizerWorker

    spec, path, config = verified_lake
    provision_exporter(spec)
    verify(spec)
    config = replace(
        config,
        verification_sha256=hashlib.sha256(
            (spec.data_root / "verification/serving-proof.json").read_bytes()
        ).hexdigest(),
    )
    configure_analytics(path, config)
    incoming = path.parent / "pagination"
    events = [
        AgentEvent(
            id=f"many-{i}",
            session_id="s",
            agent_id="test",
            timestamp=datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
            + timedelta(milliseconds=i),
            event_type="tool_call",
            raw_data={"tool_name": "test_tool", "input": {}},
        )
        for i in range(1005)
    ]
    write_events_jsonl(events, incoming, run_id="pagination", source_id="test")
    source = next(incoming.glob("*.jsonl"))
    assert (
        ingest_file(
            source, parquet_dir=path.parent / "unused", duckdb_path=path
        ).inserted
        == 1005
    )
    with LakeOutboxExporter(control_path=path, spec=spec, batch_size=1000) as exporter:
        assert exporter.run_once()["acknowledged"] == 1000
        assert exporter.run_once()["acknowledged"] == 5

    # We need to test worker._summarize_session.
    # It requires a leased job.
    ledger = JobLedger(path)
    ledger.enqueue(SUMMARIZE_SESSION, "s")
    claimed = ledger.claim(SUMMARIZE_SESSION, limit=1, worker_id="pagination-test")
    assert claimed
    job = claimed[0]

    class FakeBackend:
        model = "fake"

        def summarize(self, prompt):
            return {
                "summary_md": "paginated ok",
                "next_steps_md": "",
                "open_questions": [],
                "last_user_prompt": "",
                "last_assistant": "",
            }

    worker = SummarizerWorker(duckdb_path=path)
    # This should succeed without raising LakeError("analytics_row_limit_exceeded")
    result = worker._summarize_session(ledger, job, backend=FakeBackend())
    assert result is True


def test_cockpit_activity_scopes_pg_snapshot(verified_lake):
    from drover.server.cockpit.analytics import AnalyticsFilters
    from drover.server.cockpit.service import CockpitService
    from drover.server.lake.serving import configure_analytics

    spec, path, config = verified_lake
    configure_analytics(path, config)

    from drover.server.db import control_plane_connection

    with control_plane_connection(path) as pg:
        # Native collector identities belong to their native namespace. Older
        # rows must also stay outside the bounded 7-day PG snapshot copy.
        pg.execute(
            """INSERT INTO harness_hosts (host_id,display_name,kind,status,capabilities_json)
                      VALUES ('h','collector','collector','online','{}')"""
        )
        pg.execute(
            """INSERT INTO harness_sessions (session_id, host_id, harness, command, status, started_at, last_activity)
               SELECT 'old-' || i, 'h', 'c', 'collector', 'completed', '2020-01-01'::DATE, '2020-01-01'::DATE
               FROM generate_series(0,10004) AS t(i)"""
        )
        # 1 new row that is within the 7 day window
        pg.execute(
            """INSERT INTO harness_sessions (session_id, host_id, harness, command, status, started_at, last_activity)
               VALUES ('new-1', 'h', 'c', 'collector', 'running', current_timestamp, current_timestamp)"""
        )

    service = CockpitService(provider_usage=None, duckdb_path=path)
    result = service.overview(AnalyticsFilters(days=7))
    assert result["activity"]["status"] == "ok"
    assert "reason" not in result["activity"]
