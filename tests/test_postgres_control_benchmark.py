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
