"""Remaining canonical reads and writer retirement use private fixture lakes."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from test_control_outbox import postgres_control_store
from test_lake_runtime import lake_spec
from test_lake_serving import verified_lake

from drover.config import AnalyticsConfig
from drover.server.lake.runtime import LakeError
from drover.server.lake.serving import configure_analytics
from drover.server.lake.writer_gate import activate_retirement, legacy_derived_write


def test_legacy_writer_default_and_unverified_activation(tmp_path):
    path = tmp_path / "unused"
    with legacy_derived_write(path) as allowed:
        assert allowed
    with pytest.raises(LakeError, match="retirement_not_requested"):
        activate_retirement(path)
    assert not path.exists()


def test_retirement_renewal_and_verification_error_stay_closed(verified_lake):
    spec, path, config = verified_lake
    config = replace(config, retire_legacy_writers=True)
    configure_analytics(path, config)
    with pytest.raises(LakeError, match="retirement_not_activated"):
        with legacy_derived_write(path):
            pytest.fail("unverified writer")
    activate_retirement(path)
    with legacy_derived_write(path) as allowed:
        assert not allowed
    configure_analytics(path, replace(config, epoch="renewed-epoch"))
    with pytest.raises(LakeError, match="retirement_renewal_required"):
        with legacy_derived_write(path):
            pytest.fail("writer on changed epoch")
    activate_retirement(path)
    with legacy_derived_write(path) as allowed:
        assert not allowed
    proof = spec.data_root / "verification/serving-proof.json"
    proof.write_text("{}")
    with pytest.raises(LakeError, match="verification"):
        with legacy_derived_write(path):
            pytest.fail("writer on failed verification")


def test_activity_and_job_paths_never_open_legacy(verified_lake, monkeypatch):
    from drover.server.lake.read_models import read_model
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    configure_analytics(path, config)
    monkeypatch.setattr(tools, "_connect", lambda *a, **kw: pytest.fail("legacy read"))
    payload = read_model(
        path,
        "project_activity",
        project_key="o/r",
        days=30,
        now="2026-10-02T00:00:00+00:00",
        max_sessions=20,
    )
    assert payload["projects"][0]["project_key"] == "o/r"
    payload = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)
    assert payload["totals"]["session_count"] >= 2
    from memory_helpers import put_brief, put_summary

    put_summary(path, "s", project_key="o/r")
    put_brief(path, "o/r")
    recent = tools.drover_recent_sessions(duckdb_path=path, project_key="o/r")
    assert recent["sessions"][0]["session_id"] == "s"
    assert (
        tools.drover_project_brief(duckdb_path=path, project_key="o/r")["project_key"]
        == "o/r"
    )
    result = tools.drover_session_close(duckdb_path=path, session_id="s")
    assert result["status"] in {"queued", "already_queued", "requeued"}


def test_public_activity_and_task_fail_closed_without_cached_legacy(
    verified_lake, monkeypatch
):
    from drover.server.cockpit.analytics import AnalyticsFilters
    from drover.server.cockpit.service import CockpitService
    from drover.server.mcp import tools

    spec, path, config = verified_lake
    configure_analytics(path, config)
    service = CockpitService(
        provider_usage=None,
        duckdb_path=path,
        connect=lambda: pytest.fail("legacy connection"),
        cursor_secret=b"x" * 32,
    )
    section = service._activity(AnalyticsFilters(days=30))
    assert section["status"] == "ok", section
    assert section["data"]["totals"]["session_count"] >= 2
    service._activity_cache = (
        {"days": 30},
        {"status": "ok", "data": "stale"},
        float("inf"),
    )
    task = tools.drover_task_status(duckdb_path=path, task_id="missing")
    assert task["reason"] == "analytics_task_projection_unavailable"
    (spec.data_root / "verification/serving-proof.json").write_text("{}")
    section = service._activity(AnalyticsFilters(days=30))
    assert section["status"] == "unavailable"
    assert section["data"] is None
    assert "verification" in section["reason"]
    assert (
        tools.drover_task_status(duckdb_path=path, task_id="missing")["status"]
        == "unavailable"
    )
    assert (
        tools.drover_project_activity(duckdb_path=path, project_key="o/r")["status"]
        == "unavailable"
    )


def test_actual_derived_writers_are_retired_and_never_reenabled(
    verified_lake, monkeypatch
):
    from drover import schema
    from drover.server import control_exporter, memory_identity, native_usage_rollup

    spec, path, config = verified_lake
    configure_analytics(path, replace(config, retire_legacy_writers=True))
    activate_retirement(path)
    monkeypatch.setattr(
        native_usage_rollup,
        "_pending_partitions",
        lambda *a, **kw: pytest.fail("legacy scan"),
    )
    monkeypatch.setattr(
        memory_identity, "ensure_memory_schema", lambda *a: pytest.fail("legacy DDL")
    )
    monkeypatch.setattr(
        schema,
        "open_duckdb_connection",
        lambda *a, **kw: pytest.fail("legacy day writer"),
    )
    exporter = control_exporter.ControlOutboxExporter(
        control_path=path, analytical_path=path, parquet_dir=path.parent / "parquet"
    )
    monkeypatch.setattr(
        exporter, "_run_once_legacy", lambda **kw: pytest.fail("legacy exporter")
    )
    assert native_usage_rollup.rollup_pending_native_usage(path).partitions == 0
    assert memory_identity.refresh_memory_projection(None, {}, store_path=path) == []
    assert schema.backfill_agent_event_day_summary(path) == 0
    assert (
        schema.bootstrap(duckdb_path=path, parquet_dir=path.parent / "not-created")
        is None
    )
    assert not (path.parent / "not-created").exists()
    assert not exporter.run_once()["enabled"]
    configure_analytics(path, AnalyticsConfig())
    with pytest.raises(LakeError, match="renewal_required"):
        exporter.run_once()


def test_retirement_drains_inflight_writer_before_activation(tmp_path, monkeypatch):
    import threading

    from drover.server.lake import writer_gate

    path = tmp_path / "not-created"
    entered, release, activated = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    failures = []

    def writer():
        with legacy_derived_write(path) as allowed:
            assert allowed
            entered.set()
            assert release.wait(3)

    def retire():
        try:
            configure_analytics(
                path,
                AnalyticsConfig(
                    backend="ducklake",
                    retire_legacy_writers=True,
                    data_root=str(tmp_path / "lake"),
                    extension_dir=str(tmp_path / "extensions"),
                    engine_sha256="0" * 64,
                    verification_sha256="0" * 64,
                    catalog_dsn_env="UNUSED_TEST_CATALOG",
                    epoch="test",
                ),
            )
            writer_gate.activate_retirement(path)
            activated.set()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=writer)
    worker.start()
    assert entered.wait(3)
    monkeypatch.setattr(writer_gate, "check_selected", lambda path: None)
    retiring = threading.Thread(target=retire)
    retiring.start()
    assert not activated.wait(0.05)
    release.set()
    worker.join(3)
    retiring.join(3)
    assert not worker.is_alive() and not retiring.is_alive()
    assert activated.is_set() and not failures
    with legacy_derived_write(path) as allowed:
        assert not allowed
    assert not path.exists()


def test_legacy_cockpit_selection_remains_authoritative(tmp_path, monkeypatch):
    from drover.server.cockpit.analytics import AnalyticsFilters
    from drover.server.cockpit.service import CockpitService
    from drover.server.lake import read_models

    service = CockpitService(
        provider_usage=None, duckdb_path=tmp_path / "legacy", cursor_secret=b"x" * 32
    )
    payload = {
        "metadata": {"observed_at": None},
        "coverage": {},
        "totals": {"session_count": 4},
    }
    monkeypatch.setattr(service, "_activity_within_budget", lambda filters: payload)
    monkeypatch.setattr(
        read_models, "read_model", lambda *a, **kw: pytest.fail("implicit lake")
    )
    assert service._activity(AnalyticsFilters())["data"] == payload


def test_canonical_activity_and_job_parity(verified_lake):
    from dataclasses import asdict
    from datetime import date

    from drover.server.cockpit.analytics import (
        AnalyticsCursorCodec,
        AnalyticsFilters,
        activity_analytics,
    )
    from drover.server.db import attached_control_plane_snapshot, open_duckdb_connection
    from drover.server.lake.read_models import read_model
    from drover.server.lake.serving import open_history
    from drover.server.memory_requeue import canonical_sessions
    from drover.server.project_activity import project_activity
    from drover.server.summarizer.jobs import source_version_for_session

    spec, path, config = verified_lake
    filters = AnalyticsFilters(days=30, project_key="o/r")
    with open_duckdb_connection(path, read_only=True, role="diagnostic") as con:
        with attached_control_plane_snapshot(con, path):
            legacy = asdict(
                activity_analytics(
                    con,
                    filters,
                    cursor_codec=AnalyticsCursorCodec(b"x" * 32),
                    spans_enabled=False,
                )
            )
            activity = project_activity(
                con,
                memory_store_path=path,
                project_key="o/r",
                days=30,
                now=datetime(2026, 10, 2, tzinfo=timezone.utc),
            )
    sessions = canonical_sessions(path, since=date(2026, 10, 1))
    configure_analytics(path, config)
    lake = read_model(
        path, "cockpit", filters=asdict(filters), cursor_secret=(b"x" * 32).hex()
    )
    for key in ("session_count", "total_tokens", "cost_usd"):
        assert lake["totals"][key] == legacy["totals"][key]
    lake_activity = read_model(
        path,
        "project_activity",
        project_key="o/r",
        days=30,
        now="2026-10-02T00:00:00+00:00",
    )
    assert [
        (p["project_key"], p["session_count"]) for p in lake_activity["projects"]
    ] == [(p["project_key"], p["session_count"]) for p in activity["projects"]]
    assert canonical_sessions(path, since=date(2026, 10, 1)) == sessions

    # Hostile project key returns no rows without error
    hostile_activity = read_model(
        path,
        "project_activity",
        project_key="foo/bar' OR '1'='1",
        days=30,
        now="2026-10-02T00:00:00+00:00",
    )
    assert hostile_activity["projects"] == []
    with open_history(path) as history:
        assert len(source_version_for_session(history, "s")) == 64


@pytest.mark.parametrize("failure", ["verification", "renewal"])
def test_retirement_lifecycle_rechecks_and_stops_on_error(
    tmp_path, monkeypatch, failure
):
    import threading

    from drover.config import default_config
    from drover.server.lake import lifecycle, writer_gate

    path = tmp_path / "unused"
    config = AnalyticsConfig(
        backend="ducklake",
        retire_legacy_writers=True,
        exporter_enabled=True,
        data_root=str(tmp_path / "lake"),
        extension_dir=str(tmp_path / "extensions"),
        engine_sha256="0" * 64,
        verification_sha256="0" * 64,
        catalog_dsn_env="UNUSED_READER",
        exporter_dsn_env="UNUSED_EXPORTER",
        epoch="test",
    )
    configure_analytics(path, config)
    checks, batches = [], []
    released = threading.Event()

    def verify(path):
        checks.append(path)
        if len(checks) == 3 and failure == "verification":
            raise LakeError("lake_verification_required")

    class Exporter:
        def __init__(self, *, spec, **kwargs):
            self.spec = spec

        def __enter__(self):
            return self

        def __exit__(self, *args):
            released.set()

        def run_once(self):
            batches.append(1)
            if failure == "renewal":
                configure_analytics(path, replace(config, epoch="renewed"))
                activate_retirement(path)

    class History:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            pass

    monkeypatch.setattr(writer_gate, "check_selected", verify)
    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Exporter)
    monkeypatch.setattr(lifecycle, "HistoryConnection", History)
    monkeypatch.setattr(lifecycle, "catalog_identity", lambda spec: "same")
    exporter = lifecycle.selected_exporter(
        replace(default_config(), duckdb_path=path, analytics=config)
    )
    exporter.start(shutdown_event=threading.Event())
    assert released.wait(3)
    exporter.stop()
    assert batches == [1]
    assert {key: exporter.health()[key] for key in ("enabled", "last_error")} == {
        "enabled": False,
        "last_error": (
            "lake_verification_required"
            if failure == "verification"
            else "lake_retirement_config_mismatch"
        ),
    }
    assert not path.exists()


@pytest.mark.parametrize("backend", ["legacy", "ducklake"])
def test_watcher_commits_summary_intent_without_reading_selected_history(
    pg_control_path, tmp_path, monkeypatch, backend
):
    import json

    from drover.server import watcher
    from drover.server.db import control_plane_connection
    from drover.server.lake import serving

    config = (
        AnalyticsConfig()
        if backend == "legacy"
        else AnalyticsConfig(
            backend="ducklake",
            data_root=str(tmp_path / "unavailable-lake"),
            extension_dir=str(tmp_path / "extensions"),
            engine_sha256="0" * 64,
            catalog_dsn_env="UNUSED_TEST_CATALOG",
            epoch="test",
            verification_sha256="0" * 64,
        )
    )
    configure_analytics(pg_control_path, config)
    monkeypatch.setattr(
        serving,
        "open_history",
        lambda *a, **kw: pytest.fail("ingest read analytical history"),
    )
    path = tmp_path / "watcher.jsonl"
    path.write_text(
        json.dumps(
            dict(
                id="watcher-event",
                session_id="s",
                agent_id="host",
                timestamp="2026-10-04T12:00:00Z",
                event_type="user_message",
                message=dict(role="user", content="a substantive watcher message"),
            )
        )
        + "\n"
    )
    parquet = tmp_path / "unused-parquet"
    handler = watcher._Handler(parquet, pg_control_path)
    handler._maybe_ingest(path)
    assert not path.exists()
    assert (tmp_path / ".processed" / path.name).exists()
    assert not parquet.exists()
    with control_plane_connection(pg_control_path) as control:
        assert control.execute(
            "SELECT count(*) FROM control_outbox_events WHERE state='pending'"
        ).fetchone() == (1,)
        assert control.execute(
            "SELECT count(*) FROM pipeline_jobs WHERE job_kind='summarize_session' AND subject_key='s' AND status='pending'"
        ).fetchone() == (1,)


def test_project_activity_http_selects_before_legacy(tmp_path, monkeypatch):
    from drover.server import metrics
    from drover.server.lake import read_models
    from drover.server.metrics import MetricsCollector

    path = tmp_path / "unused"
    config = AnalyticsConfig(
        backend="ducklake",
        data_root=str(tmp_path / "lake"),
        extension_dir=str(tmp_path / "extensions"),
        engine_sha256="0" * 64,
        verification_sha256="0" * 64,
        catalog_dsn_env="UNUSED",
        epoch="test",
    )
    configure_analytics(path, config)
    collector = MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path / "incoming", summarizer_report={}
    )
    monkeypatch.setattr(
        metrics,
        "open_duckdb_connection",
        lambda *a, **kw: pytest.fail("legacy HTTP connection"),
    )

    def unavailable(*args, **kwargs):
        raise LakeError("lake_verification_required")

    monkeypatch.setattr(read_models, "read_model", unavailable)
    status, body = collector.render_project_activity_json(
        project_key="o/r", days=7, limit=20
    )
    assert status == 503 and "lake_verification_required" in body
    assert not path.exists()


def test_lake_activity_cursor_error_preserves_reload_contract(tmp_path, monkeypatch):
    from drover.server.cockpit.analytics import (
        AnalyticsFilters,
        AnalyticsSnapshotChangedError,
    )
    from drover.server.cockpit.service import CockpitService
    from drover.server.lake import read_models

    path = tmp_path / "unused"
    configure_analytics(
        path,
        AnalyticsConfig(
            backend="ducklake",
            data_root=str(tmp_path / "lake"),
            extension_dir=str(tmp_path / "extensions"),
            engine_sha256="0" * 64,
            verification_sha256="0" * 64,
            catalog_dsn_env="UNUSED",
            epoch="test",
        ),
    )

    def changed(*args, **kwargs):
        from drover.server.lake.runtime import LakeError

        raise LakeError("snapshot_changed")

    monkeypatch.setattr(read_models, "read_model", changed)
    service = CockpitService(duckdb_path=path, provider_usage=None)
    with pytest.raises(AnalyticsSnapshotChangedError):
        service._activity(AnalyticsFilters())
    from drover.server.lake.runtime import LakeError

    with pytest.raises(LakeError) as exc_info:
        # Validation runs before any store/credential access.
        monkeypatch.undo()
        read_models.read_model(path, "project_activity", project_key="invalid")
    assert exc_info.value.code == "invalid_project_key"


def test_native_freshness_metadata_does_not_certify_legacy_rollup(verified_lake):
    from drover.server.db import control_plane_connection
    from drover.server.lake.read_models import read_model

    _, path, config = verified_lake
    configure_analytics(path, config)
    with control_plane_connection(path) as con:
        con.execute(
            "INSERT INTO native_usage_partition_watermarks(partition_date,source_activity_at,rolled_at) VALUES ('2026-10-01','2026-10-01','2026-10-02')"
        )
    result = read_model(path, "cockpit", filters={"days": 30}, cursor_secret="00" * 32)
    metadata = result["metadata"]
    assert metadata["native_publication"]["freshness"] == "unavailable"
    assert metadata["native_usage"]["freshness"] == "unavailable"
    assert metadata["native_usage"]["observed_at"].startswith("2026-10-02")
    assert metadata["native_usage"]["reason"] == "lake_coverage_unverified"
