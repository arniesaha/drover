"""Contracts for the isolated PostgreSQL control-plane benchmark."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "benchmark_postgres_control_plane.py"
)
SPEC = importlib.util.spec_from_file_location("postgres_control_benchmark", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCHMARK
SPEC.loader.exec_module(BENCHMARK)


@pytest.fixture
def benchmark_postgres_control_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create one isolated PostgreSQL store for benchmark-state contracts."""

    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("DROVER_TEST_POSTGRES_DSN is required for PostgreSQL integration")

    from drover.config import ControlStoreConfig
    from drover.schema import bootstrap_control_plane_store
    from drover.server.control_store import close_control_store, configure_control_store

    schema = f"drover_benchmark_test_{uuid4().hex}"
    control_path = tmp_path / "central-selector"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=2.0,
        statement_timeout_seconds=2.0,
        schema=schema,
    )
    monkeypatch.setenv("DROVER_TEST_POSTGRES_DSN", dsn)
    configure_control_store(control_path, config)
    bootstrap_control_plane_store(control_path)
    try:
        yield control_path, dsn, schema
    finally:
        close_control_store(control_path)
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_harness_response_requires_known_synthetic_identities_and_no_error() -> None:
    payload = {
        "hosts": [{"host_id": "benchmark-host-0"}],
        "sessions": [{"session_id": "benchmark-session-0"}],
    }

    BENCHMARK.validate_harness_response(
        payload,
        expected_host_ids={"benchmark-host-0"},
        expected_session_ids={"benchmark-session-0"},
    )

    with pytest.raises(BENCHMARK.BenchmarkContractError, match="error envelope"):
        BENCHMARK.validate_harness_response(
            {"error": "analytics worker unavailable", "hosts": [], "sessions": []},
            expected_host_ids={"benchmark-host-0"},
            expected_session_ids={"benchmark-session-0"},
        )

    with pytest.raises(BENCHMARK.BenchmarkContractError, match="expected host"):
        BENCHMARK.validate_harness_response(
            {"hosts": [], "sessions": [{"session_id": "benchmark-session-0"}]},
            expected_host_ids={"benchmark-host-0"},
            expected_session_ids={"benchmark-session-0"},
        )


def test_phase_summary_does_not_present_tiny_sample_p99_as_a_measurement() -> None:
    summary = BENCHMARK.summarize_phase(
        "worker_outage", [0.012, 0.015], errors=["timeout"]
    )

    assert summary["sample_count"] == 2
    assert summary["percentiles"] == {
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "status": "insufficient_samples",
    }
    assert summary["errors"] == {"timeout": 1}


def test_synthetic_child_environment_drops_real_provider_credentials(
    tmp_path: Path,
) -> None:
    environment = BENCHMARK.synthetic_child_environment(
        home=tmp_path / "home",
        dsn="postgresql://benchmark",
        path="/usr/bin:/bin",
        api_token="synthetic-api-token",
        api_to_worker_token="synthetic-api-worker-token",
        worker_to_api_token="synthetic-worker-api-token",
    )

    assert environment == {
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin:/bin",
        "DROVER_TEST_POSTGRES_DSN": "postgresql://benchmark",
        "DROVER_API_TOKEN": "synthetic-api-token",
        "DROVER_API_TO_ANALYTICS_TOKEN": "synthetic-api-worker-token",
        "DROVER_ANALYTICS_TO_API_TOKEN": "synthetic-worker-api-token",
    }


def test_benchmark_runtime_config_uses_a_realistic_two_connection_pool_timeout(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "benchmark.toml"

    BENCHMARK._write_config(
        config_path,
        duckdb_path=tmp_path / "selector.duckdb",
        root=tmp_path / "runtime",
        schema="drover_benchmark_test",
        metrics_port=7080,
        api_port=7080,
        worker_port=7082,
        api_token="benchmark-test-token",
    )

    assert "pool_max_size = 2" in config_path.read_text(encoding="utf-8")
    assert "acquire_timeout_seconds = 2.0" in config_path.read_text(encoding="utf-8")


def test_benchmark_bootstrap_uses_the_same_two_second_pool_timeout() -> None:
    config = BENCHMARK.benchmark_control_store_config("drover_benchmark_test")

    assert config.pool_max_size == 2
    assert config.acquire_timeout_seconds == 2.0


def test_health_probe_accepts_the_runtime_plain_text_liveness_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PlainHealthResponse:
        status = 200

        def __enter__(self) -> "PlainHealthResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok\n"

    monkeypatch.setattr(
        BENCHMARK.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: PlainHealthResponse(),
    )

    assert BENCHMARK._request_status(7080, "/healthz", token="synthetic") == 200


def test_json_request_returns_an_expected_http_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = BENCHMARK.urllib.error.HTTPError(
        "http://127.0.0.1:7080/metrics",
        503,
        "worker unavailable",
        None,
        io.BytesIO(b'{"error":"analytics worker unavailable"}'),
    )
    monkeypatch.setattr(
        BENCHMARK.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    assert BENCHMARK._request_json(7080, "/metrics", token="synthetic") == (
        503,
        {"error": "analytics worker unavailable"},
    )


def test_worker_outage_requires_the_expected_nonempty_error_body() -> None:
    BENCHMARK.validate_worker_outage_response({"error": "analytics worker unavailable"})

    for payload in ({}, {"error": ""}, {"error": "wrong worker error"}):
        with pytest.raises(BENCHMARK.BenchmarkContractError, match="outage error"):
            BENCHMARK.validate_worker_outage_response(payload)


def test_p99_target_miss_is_reported_as_non_successful_evidence() -> None:
    phases = [
        {
            "name": "worker_outage",
            "percentiles": {"status": "measured", "p99_ms": 251.0},
        },
        {
            "name": "recovery",
            "percentiles": {"status": "measured", "p99_ms": 249.0},
        },
    ]

    report = {
        "phases": phases,
        "limits": {"p99_target_ms": 250},
    }
    assert BENCHMARK.finalize_benchmark_outcome(report) == 1
    assert report["outcome"] == "target_missed"
    assert report["stage"] == "complete"
    assert report["p99_target_misses"] == [{"name": "worker_outage", "p99_ms": 251.0}]


def test_postgres_size_evidence_captures_sanitized_server_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, one=None):
            self._one = one

        def fetchone(self):
            return self._one

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query, _params=None):
            text = str(query)
            if "pg_total_relation_size" in text:
                return Result((101,))
            if "pg_database_size" in text:
                return Result((202,))
            if "server_version" in text:
                return Result(("17.11", "16", "128MB"))
            raise AssertionError(text)

    class Psycopg:
        @staticmethod
        def connect(_dsn):
            return Connection()

    monkeypatch.setitem(sys.modules, "psycopg", Psycopg())
    evidence = BENCHMARK._postgres_sizes("postgresql://secret@host/db", "benchmark")

    assert evidence["benchmark_schema_physical_bytes"] == 101
    assert evidence["whole_disposable_database_bytes"] == 202
    assert evidence["postgres"] == {
        "server_version": "17.11",
        "max_connections": "16",
        "shared_buffers": "128MB",
    }


def test_process_readiness_failure_reports_the_last_http_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess:
        returncode = None

        def poll(self) -> None:
            return None

    error = BENCHMARK.urllib.error.HTTPError(
        "http://127.0.0.1:7080/healthz",
        401,
        "unauthorized",
        None,
        io.BytesIO(b'{"error":"authentication required"}'),
    )
    monkeypatch.setattr(
        BENCHMARK,
        "_request_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(BENCHMARK.BenchmarkContractError, match="HTTP 401"):
        BENCHMARK._wait_for_process(
            RunningProcess(), 7080, "/healthz", token="synthetic", timeout_seconds=0.01
        )


def test_usage_validator_accepts_sessions_already_current_in_postgres(
    benchmark_postgres_control_store: tuple[Path, str, str],
) -> None:
    """The worker may roll usage before the benchmark's final check runs."""

    control_path, _dsn, _schema = benchmark_postgres_control_store
    from drover.server.db import control_plane_connection
    from drover.server.harness.registry import HarnessRegistry
    from drover.server.harness.usage_rollup import rollup_pending_sessions

    registry = HarnessRegistry(control_path)
    registry.register_host(
        host_id="benchmark-host-0", display_name="Synthetic host", kind="test"
    )
    registry.create_session(
        host_id="benchmark-host-0",
        harness="codex",
        command="synthetic",
        session_id="benchmark-session-000",
    )
    registry.append_event(
        session_id="benchmark-session-000",
        event_id="benchmark-usage-event",
        event_type="assistant_output",
        seq=1,
        payload={"usage": {"input_tokens": 1, "output_tokens": 2}},
    )
    with control_plane_connection(control_path) as connection:
        assert rollup_pending_sessions(connection).rolled == 1
        assert rollup_pending_sessions(connection).rolled == 0

    assert (
        BENCHMARK._missing_exact_usage_sessions(control_path, ["benchmark-session-000"])
        == []
    )


def test_completed_benchmark_sentinels_remain_in_default_archive_window(
    benchmark_postgres_control_store,
):
    from drover.server.harness.registry import HarnessRegistry

    control_path, _, _ = benchmark_postgres_control_store
    registry = HarnessRegistry(control_path)
    registry.register_host(host_id="fixture-host", display_name="Fixture", kind="test")
    sessions = [f"fixture-session-{index:03d}" for index in range(25)]
    for session_id in sessions:
        registry.create_session(
            host_id="fixture-host",
            harness="codex",
            command="synthetic",
            session_id=session_id,
        )
    BENCHMARK.complete_benchmark_sessions(registry, sessions)
    visible = {
        session.session_id for session in registry.list_sessions(archived_limit=20)
    }
    assert {sessions[0], sessions[-1]} <= visible
    assert len(visible) == 20
    assert all(session.status == "completed" for session in registry.list_sessions())


def test_invalid_http_phase_stops_before_background_drain(monkeypatch):
    failed = BENCHMARK.summarize_phase("outage", [0.001] * 100, errors=["identity"])
    monkeypatch.setattr(BENCHMARK, "_record_http_phase", lambda *args, **kwargs: failed)
    report = {"phases": [], "stage": "outage"}
    with pytest.raises(BENCHMARK.BenchmarkContractError, match="outage.*errors"):
        BENCHMARK.record_checked_http_phase(report, "outage")
    assert report["phases"] == [failed]


def test_background_progress_probe_retries_one_query_canceled_then_progresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg_errors = pytest.importorskip("psycopg.errors")
    probe = BENCHMARK.BackgroundProgressProbe()
    status_calls = 0

    def status(_control_path: Path) -> dict[str, int]:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            raise psycopg_errors.QueryCanceled("temporary statement deadline")
        return {
            "pending": 0,
            "claimed": 0,
            "published_unacknowledged": 0,
            "acknowledged": 1,
        }

    class RunningProcess:
        returncode = None

        def poll(self) -> None:
            return None

    monkeypatch.setattr(BENCHMARK, "_outbox_status_for_path", status)
    monkeypatch.setattr(BENCHMARK.time, "sleep", lambda _seconds: None)

    assert (
        BENCHMARK._wait_for_worker_progress(
            process=RunningProcess(),
            control_path=Path("synthetic-control"),
            baseline_acknowledged=0,
            minimum_acknowledged=1,
            progress_probe=probe,
        )["acknowledged"]
        == 1
    )
    assert probe.error_counts == {"postgres_query_canceled": 1}
    assert probe.consecutive_failures == 0
    assert BENCHMARK.BACKGROUND_PROGRESS_PROBE_INTERVAL_SECONDS == 1.0


def test_background_progress_probe_retries_control_store_busy_then_progresses() -> None:
    from drover.server.postgres_control_store import ControlStoreBusy

    probe = BENCHMARK.BackgroundProgressProbe()
    attempts = 0

    def sampled() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ControlStoreBusy("pool exhausted")
        return "sampled"

    assert probe.sample(sampled) is None
    assert probe.sample(sampled) == "sampled"
    assert probe.error_counts == {"control_store_busy": 1}
    assert probe.consecutive_failures == 0


def test_background_progress_probe_fails_after_three_consecutive_transients() -> None:
    from drover.server.postgres_control_store import ControlStoreBusy

    probe = BENCHMARK.BackgroundProgressProbe()

    assert (
        probe.sample(lambda: (_ for _ in ()).throw(ControlStoreBusy("first"))) is None
    )
    assert (
        probe.sample(lambda: (_ for _ in ()).throw(ControlStoreBusy("second"))) is None
    )
    with pytest.raises(
        BENCHMARK.BenchmarkContractError,
        match="background progress probe failed three consecutive times",
    ):
        probe.sample(lambda: (_ for _ in ()).throw(ControlStoreBusy("third")))
    assert probe.error_counts == {"control_store_busy": 3}


def test_background_progress_probe_does_not_swallow_nontransient_faults() -> None:
    probe = BENCHMARK.BackgroundProgressProbe()

    with pytest.raises(RuntimeError, match="unexpected query fault"):
        probe.sample(
            lambda: (_ for _ in ()).throw(RuntimeError("unexpected query fault"))
        )
    assert probe.error_counts == {}


def test_failure_report_preserves_probe_counters_and_safe_fixed_reason() -> None:
    probe = BENCHMARK.BackgroundProgressProbe()
    probe.error_counts.update({"postgres_query_canceled": 2, "control_store_busy": 1})
    report = {"stage": "worker_export_drain"}

    BENCHMARK.record_benchmark_failure(
        report,
        BENCHMARK.BenchmarkContractError(
            BENCHMARK.BACKGROUND_PROGRESS_PROBE_FAILURE_REASON
        ),
        progress_probe=probe,
    )

    assert report["background_progress_probe_errors"] == {
        "control_store_busy": 1,
        "postgres_query_canceled": 2,
    }
    assert report["failure"] == {
        "stage": "worker_export_drain",
        "type": "BenchmarkContractError",
        "reason": "background progress probe failed three consecutive times",
    }

    unsafe_report = {"stage": "worker_export_drain"}
    BENCHMARK.record_benchmark_failure(
        unsafe_report,
        BENCHMARK.BenchmarkContractError("postgresql://sensitive@localhost"),
        progress_probe=probe,
    )
    assert "reason" not in unsafe_report["failure"]
    assert (
        unsafe_report["background_progress_probe_errors"]
        == report["background_progress_probe_errors"]
    )
