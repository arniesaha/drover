"""Regression contracts for availability and real exporter/maintenance handoff."""

import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from test_lake_exporter import export_lake, write_serving_proof
from test_lake_runtime import lake_spec


def test_unservable_lake_starts_only_explicit_recovery_control(monkeypatch):
    from drover.config import AnalyticsConfig, ControlStoreConfig, default_config
    from drover.server import __main__ as entry
    from drover.server import process_memory
    from drover.server.lake import serving
    from drover.server.lake.runtime import LakeError

    cfg = replace(
        default_config(),
        control_store=replace(
            default_config().control_store,
            backend="postgres",
            dsn_env="TEST_CONTROL_DSN",
        ),
        analytics=AnalyticsConfig(
            backend="ducklake",
            catalog_dsn_env="READER_DSN",
            data_root="/lake",
            extension_dir="/extensions",
            engine_sha256="0" * 64,
            verification_sha256="1" * 64,
        ),
    )
    monkeypatch.setattr(entry, "_resolve_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(process_memory, "configure_memory_guard", lambda *_a: None)
    for name in ("FootprintWatchdog", "StartupFootprintSampler"):
        monkeypatch.setattr(entry, name, lambda: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(
        serving,
        "check_selected",
        lambda *_a: (_ for _ in ()).throw(LakeError("lake_verification_file_limit")),
    )
    called = []
    monkeypatch.setattr(entry, "_run_api_role", lambda **kw: called.append(kw))
    monkeypatch.setattr(
        entry, "bootstrap", lambda **kw: pytest.fail("analytics must not bootstrap")
    )
    result = CliRunner().invoke(entry.main, ["run", "--role", "all"])
    assert result.exit_code == 0, result.output
    assert called[0]["analytics_failure"] == "lake_verification_file_limit"
    # The dedicated analytics role must still fail closed, not become control.
    called.clear()
    monkeypatch.setenv(cfg.analytics_boundary.api_to_worker_token_env, "test-internal")
    monkeypatch.setenv(cfg.analytics_boundary.worker_to_api_token_env, "test-return")
    result = CliRunner().invoke(entry.main, ["run", "--role", "analytics"])
    assert result.exit_code != 0
    assert called == []


def test_maintenance_waits_for_finished_batch_and_released_owner(monkeypatch, tmp_path):
    from drover.server.lake import lifecycle, task_projection

    held = threading.Event()
    in_batch = threading.Event()
    finish_batch = threading.Event()
    maintenance_entered = threading.Event()
    shutdown = threading.Event()

    class Owner:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            held.set()
            return self

        def __exit__(self, *a):
            held.clear()

    cfg = SimpleNamespace(
        duckdb_path=tmp_path / "db",
        analytics=SimpleNamespace(retire_legacy_writers=False),
    )
    worker = lifecycle.ExporterLifecycle(cfg)
    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", Owner)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *_a, **_k: None)
    monkeypatch.setattr(lifecycle, "_check_export_catalog", lambda *_a: None)
    monkeypatch.setattr(
        task_projection, "refresh_if_provisioned", lambda *_a, **_k: None
    )
    monkeypatch.setattr(worker, "_checkpoint", lambda *_a: None)

    def batch(_owner):
        in_batch.set()
        assert finish_batch.wait(3)
        return None

    monkeypatch.setattr(worker, "_export_batch", batch)
    runner = threading.Thread(target=worker._supervise, args=(shutdown,))
    runner.start()
    assert in_batch.wait(3)
    errors = []

    def maintain():
        try:
            with worker.pause_for_maintenance(timeout_seconds=3):
                assert not held.is_set()
                maintenance_entered.set()
        except BaseException as exc:
            errors.append(exc)

    maintenance = threading.Thread(target=maintain)
    maintenance.start()
    try:
        assert not maintenance_entered.wait(0.05)
        finish_batch.set()
        assert maintenance_entered.wait(3)
        maintenance.join(3)
        assert not errors
        assert worker._restarts == 0
    finally:
        finish_batch.set()
        worker._stop.set()
        runner.join(3)
        maintenance.join(3)
    assert not runner.is_alive()
    assert not held.is_set()


def test_scheduler_records_failed_handoff_without_compacting(monkeypatch, tmp_path):
    from drover.server.lake.maintenance import CompactionPolicy, LakeCompactionScheduler
    from drover.server.lake.runtime import LakeError, LakeSpec

    (tmp_path / "verification").mkdir()

    @contextmanager
    def refusal():
        raise LakeError("lake_exporter_maintenance_pause_deadline")
        yield

    @contextmanager
    def admit():
        yield True

    scheduler = LakeCompactionScheduler(
        reader_spec=LakeSpec("READER", tmp_path, tmp_path, "test"),
        maintenance_dsn_env="MAINT",
        policy=CompactionPolicy(),
        max_runtime_seconds=1800,
        maintenance_admission=SimpleNamespace(admit=admit),
        planned_stop_active=lambda: False,
        exporter_backlog_high=lambda: False,
        exporter_pause=refusal,
        execute=lambda *_a, **_k: pytest.fail("writer has not released ownership"),
    )
    monkeypatch.setattr(scheduler, "refresh_health", lambda: {"referenced_files": 6000})
    with pytest.raises(LakeError, match="lake_exporter_maintenance_pause_deadline"):
        scheduler.tick()
    assert (
        scheduler.health()["last_error"] == "lake_exporter_maintenance_pause_deadline"
    )
    assert (tmp_path / "verification/maintenance-status.json").is_file()


def test_serving_retune_preserves_logical_rows_and_receipted_proof(export_lake):
    import hashlib
    import json

    import psycopg

    from drover.server.lake.maintenance import _fingerprint, compact_lake
    from drover.server.lake.runtime import lake_connection
    from drover.server.lake.serving_proof import check_proof

    with lake_connection(export_lake, read_only=False) as con:
        for table in (
            "agent_events",
            "activity_daily",
            "control_outbox_batches",
            "export_event_versions",
            "export_batch_receipts",
        ):
            con.execute(
                "CALL lake.set_option('data_inlining_row_limit', 0, table_name => ?)",
                [table],
            )
        con.execute(
            "INSERT INTO lake.agent_events (id,date) VALUES ('preserved', '2026-11-21')"
        )
    write_serving_proof(export_lake)
    with psycopg.connect(export_lake.dsn()) as pg:
        role = pg.execute("select current_user").fetchone()[0]
    with lake_connection(export_lake) as con:
        before = _fingerprint(con, "agent_events")
    report = compact_lake(export_lake, retune_exporter_role=role)
    assert report["exporter_storage_retuned"] is True
    proof = export_lake.data_root / "verification/serving-proof.json"
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    with lake_connection(export_lake) as con:
        assert _fingerprint(con, "agent_events") == before
        check_proof(export_lake, con, digest)
        options = con.execute("FROM lake.options()").fetchall()
        assert any("30 days" in str(row) for row in options)
    journal = json.loads((proof.parent / "maintenance-receipts.json").read_text())
    assert journal[-1]["tables"] == journal[-1]["after_tables"]


def test_bad_shared_lake_refuses_update_before_any_drain(monkeypatch, tmp_path):
    from drover.server.harness import updater
    from drover.server.lake import serving
    from drover.server.lake.runtime import LakeError

    host = updater.HostUpdater.__new__(updater.HostUpdater)
    host._lock = threading.Lock()
    host._pending = "0.1.4"
    host._state = None
    host._cfg = SimpleNamespace(
        update_runtime_root=None,
        analytics=SimpleNamespace(backend="ducklake"),
        duckdb_path=tmp_path / "selected",
    )
    host._layout = SimpleNamespace(verify_runtime=lambda _: True)
    refused = []
    host._record_refusal = lambda version, reason: refused.append((version, reason))
    monkeypatch.setattr(updater, "runtime_dependency_preflight", lambda _: [])
    monkeypatch.setattr(
        serving,
        "check_selected",
        lambda _: (_ for _ in ()).throw(LakeError("lake_verification_file_limit")),
    )
    monkeypatch.setattr(
        updater,
        "drain_for_restart",
        lambda _: pytest.fail("must preserve running service"),
    )
    assert host.maybe_activate() is False
    assert refused == [("0.1.4", "lake_preflight")]
    assert host._pending == "0.1.4"
