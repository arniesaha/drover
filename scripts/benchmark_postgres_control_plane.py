#!/usr/bin/env python3
"""Run a bounded, synthetic PostgreSQL control-plane benchmark.

The benchmark starts fresh API and analytics-role processes and never inherits
the caller's credential environment. It is an operator-run evidence tool, not
part of pytest or CI: it creates one disposable PostgreSQL schema and a
scratch-only local state tree, then removes both after writing its sanitized
JSON report.

Example:
    DROVER_TEST_POSTGRES_DSN=postgresql://127.0.0.1:55432/postgres \\
      .venv/bin/python scripts/benchmark_postgres_control_plane.py \\
      --output docs/internal/postgres-benchmark/result.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

MIN_PERCENTILE_SAMPLES = 100
DEFAULT_HOSTS = 4
DEFAULT_SESSIONS = 200
DEFAULT_EVENTS = 75_000
DEFAULT_PAYLOAD_BYTES = 2_300
DEFAULT_PHASE_REQUESTS = 160
DEFAULT_CLIENTS = 4
WRITER_THREADS = 4
INSERT_BATCH_SIZE = 100
DEFAULT_DRAIN_TIMEOUT_SECONDS = 1_800
EXPECTED_WORKER_OUTAGE_ERROR = "analytics worker unavailable"


class BenchmarkContractError(RuntimeError):
    """The workload did not demonstrate the bounded benchmark contract."""


def synthetic_child_environment(
    *,
    home: Path,
    dsn: str,
    path: str,
    api_token: str,
    api_to_worker_token: str,
    worker_to_api_token: str,
) -> dict[str, str]:
    """Return the complete allowlisted child environment.

    In particular this does not inherit provider, model, archive, cloud, or
    personal-session credentials from the invoking shell.
    """

    return {
        "HOME": str(home),
        "PATH": path,
        "DROVER_TEST_POSTGRES_DSN": dsn,
        "DROVER_API_TOKEN": api_token,
        "DROVER_API_TO_ANALYTICS_TOKEN": api_to_worker_token,
        "DROVER_ANALYTICS_TO_API_TOKEN": worker_to_api_token,
    }


def validate_harness_response(
    payload: object,
    *,
    expected_host_ids: set[str],
    expected_session_ids: set[str],
) -> None:
    """Reject HTTP 200 error envelopes and unexpected synthetic snapshots."""

    if not isinstance(payload, Mapping):
        raise BenchmarkContractError("/harness returned a non-object JSON payload")
    if "error" in payload:
        raise BenchmarkContractError("/harness returned an error envelope")
    hosts = payload.get("hosts")
    sessions = payload.get("sessions")
    if not isinstance(hosts, list) or not isinstance(sessions, list):
        raise BenchmarkContractError("/harness response lacks host or session arrays")
    host_ids = {
        item.get("host_id")
        for item in hosts
        if isinstance(item, Mapping) and isinstance(item.get("host_id"), str)
    }
    session_ids = {
        item.get("session_id")
        for item in sessions
        if isinstance(item, Mapping) and isinstance(item.get("session_id"), str)
    }
    if not expected_host_ids <= host_ids:
        raise BenchmarkContractError(
            "/harness response lacks an expected host identity"
        )
    if not expected_session_ids <= session_ids:
        raise BenchmarkContractError(
            "/harness response lacks an expected session identity"
        )


def validate_worker_outage_response(payload: object) -> None:
    """Require the API's explicit bounded worker-unavailable envelope."""
    if not isinstance(payload, Mapping):
        raise BenchmarkContractError("worker outage error body was not a JSON object")
    error = payload.get("error")
    if not isinstance(error, str) or error != EXPECTED_WORKER_OUTAGE_ERROR:
        raise BenchmarkContractError(
            "worker outage error body was not the expected analytics worker error"
        )


def _percentile_ms(samples: list[float], percentile: float) -> float:
    index = max(0, math.ceil(len(samples) * percentile) - 1)
    return round(samples[index] * 1_000, 3)


def summarize_phase(
    name: str, latencies: Iterable[float], *, errors: Iterable[str]
) -> dict[str, Any]:
    """Return explicit distributions for every attempted request.

    Failed attempts retain their elapsed time and are reported in the same
    phase. A percentile with fewer than 100 attempts is intentionally not a
    performance result.
    """

    values = sorted(float(value) for value in latencies)
    error_counts = Counter(str(error) for error in errors)
    if len(values) < MIN_PERCENTILE_SAMPLES:
        percentiles: dict[str, float | str | None] = {
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "status": "insufficient_samples",
        }
    else:
        percentiles = {
            "p50_ms": round(statistics.median(values) * 1_000, 3),
            "p95_ms": _percentile_ms(values, 0.95),
            "p99_ms": _percentile_ms(values, 0.99),
            "status": "measured",
        }
    return {
        "name": name,
        "sample_count": len(values),
        "errors": dict(sorted(error_counts.items())),
        "percentiles": percentiles,
    }


def p99_target_misses(
    phases: Iterable[Mapping[str, Any]], *, target_ms: float
) -> list[dict[str, float | str]]:
    """Return measured phases over the declared p99 target without hiding data."""
    misses: list[dict[str, float | str]] = []
    for phase in phases:
        percentiles = phase.get("percentiles")
        p99_ms = (
            percentiles.get("p99_ms")
            if isinstance(percentiles, Mapping)
            and percentiles.get("status") == "measured"
            else None
        )
        if isinstance(p99_ms, (int, float)) and p99_ms > target_ms:
            name = phase.get("name")
            misses.append(
                {
                    "name": str(name) if isinstance(name, str) else "unknown",
                    "p99_ms": float(p99_ms),
                }
            )
    return misses


def finalize_benchmark_outcome(report: dict[str, Any]) -> int:
    """Record the p99 assessment and return the process result for the run."""
    report["p99_target_misses"] = p99_target_misses(
        report["phases"], target_ms=report["limits"]["p99_target_ms"]
    )
    report["stage"] = "complete"
    if report["p99_target_misses"]:
        report["outcome"] = "target_missed"
        return 1
    report["outcome"] = "passed"
    return 0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _scratch_token() -> str:
    """Generate a non-production token used only by one scratch benchmark run."""

    return f"synthetic-{uuid4().hex}"


def _request_json(
    port: int,
    path: str,
    *,
    token: str,
    timeout_seconds: float = 3.0,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, object]:
    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _request_status(
    port: int,
    path: str,
    *,
    token: str,
    timeout_seconds: float = 3.0,
    headers: Mapping[str, str] | None = None,
) -> int:
    """Read a response status without imposing a JSON body contract."""

    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()
        return response.status


def _wait_for_process(
    process: subprocess.Popen[bytes],
    port: int,
    path: str,
    *,
    token: str,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_probe = "no probe attempted"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkContractError(
                f"runtime process exited before {path} was ready ({process.returncode})"
            )
        try:
            status = _request_status(
                port,
                path,
                token=token,
                headers=headers,
            )
            if status == 200:
                return
            last_probe = f"HTTP {status} without a successful readiness response"
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            try:
                body_kind = (
                    "JSON error envelope"
                    if isinstance(json.loads(raw_body), Mapping)
                    else "non-object JSON"
                )
            except (TypeError, ValueError):
                body_kind = "non-JSON body" if raw_body else "empty body"
            last_probe = f"HTTP {error.code} {body_kind}"
        except urllib.error.URLError as error:
            last_probe = f"URLError {type(error.reason).__name__}"
        except OSError as error:
            last_probe = f"{type(error).__name__} errno={error.errno}"
        except ValueError as error:
            last_probe = type(error).__name__
        time.sleep(0.05)
    raise BenchmarkContractError(
        f"runtime listener did not become ready at {path}; last probe: {last_probe}"
    )


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _write_config(
    path: Path,
    *,
    duckdb_path: Path,
    root: Path,
    schema: str,
    metrics_port: int,
    api_port: int,
    worker_port: int,
    api_token: str,
) -> None:
    quote = json.dumps
    incoming = root / "incoming"
    parquet = root / "parquet"
    incoming.mkdir(parents=True, exist_ok=True)
    parquet.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "[paths]",
                f"incoming_dir = {quote(str(incoming))}",
                f"parquet_dir = {quote(str(parquet))}",
                f"duckdb_path = {quote(str(duckdb_path))}",
                "",
                "[control_store]",
                'backend = "postgres"',
                'dsn_env = "DROVER_TEST_POSTGRES_DSN"',
                "pool_min_size = 1",
                "pool_max_size = 2",
                "acquire_timeout_seconds = 2.0",
                "statement_timeout_seconds = 2.0",
                f"schema = {quote(schema)}",
                "",
                "[server]",
                'metrics_host = "127.0.0.1"',
                f"metrics_http_port = {metrics_port}",
                "",
                "[auth]",
                "enabled = true",
                f"api_token = {quote(api_token)}",
                "",
                "[update]",
                "enabled = false",
                "",
                "[archive]",
                "enabled = false",
                "",
                "[advisory_content]",
                "enabled = false",
                "external_consent = false",
                "",
                "[analytics_boundary]",
                f'worker_url = "http://127.0.0.1:{worker_port}"',
                f'api_url = "http://127.0.0.1:{api_port}"',
                'api_to_worker_token_env = "DROVER_API_TO_ANALYTICS_TOKEN"',
                'worker_to_api_token_env = "DROVER_ANALYTICS_TO_API_TOKEN"',
                "connect_timeout_seconds = 0.04",
                "request_timeout_seconds = 0.25",
                "max_concurrent_requests = 2",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def benchmark_control_store_config(schema: str):
    """Return the bounded PostgreSQL pool shared by setup and child roles."""

    from drover.config import ControlStoreConfig

    return ControlStoreConfig(
        backend="postgres",
        dsn_env="DROVER_TEST_POSTGRES_DSN",
        pool_min_size=1,
        pool_max_size=2,
        acquire_timeout_seconds=2.0,
        statement_timeout_seconds=2.0,
        schema=schema,
    )


def _payload_for(index: int, payload_bytes: int) -> dict[str, object]:
    """Create deterministic varied envelopes without real event content."""

    digest = hashlib.sha256(f"drover-benchmark-{index}".encode()).hexdigest()
    text = f"synthetic benchmark event {index} {digest}"
    while len(text.encode()) < payload_bytes:
        digest = hashlib.sha256(digest.encode()).hexdigest()
        text += " " + digest
    return {
        "text": text,
        "usage": {"input_tokens": index % 97 + 1, "output_tokens": index % 71 + 1},
        "benchmark": {"ordinal": index, "digest": digest},
    }


def _build_records(
    *,
    start: int,
    count: int,
    sessions: list[str],
    payload_bytes: int,
) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    logical_payload_bytes = 0
    for ordinal in range(start, start + count):
        session_index = ordinal % len(sessions)
        sequence = ordinal // len(sessions) + 1
        payload = _payload_for(ordinal, payload_bytes)
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        logical_payload_bytes += len(encoded)
        is_terminal = ordinal >= start + count - len(sessions)
        if is_terminal:
            payload["turn_complete"] = True
        records.append(
            {
                "event_id": f"benchmark-event-{ordinal:06d}",
                "session_id": sessions[session_index],
                "event_type": "status" if is_terminal else "assistant_output",
                "payload": payload,
                "content_preview": f"synthetic benchmark event {ordinal}",
                "seq": sequence,
            }
        )
    return records, logical_payload_bytes


def _append_batches(
    control_path: Path,
    batches: list[list[dict[str, object]]],
    *,
    threads: int,
) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from drover.server.harness.registry import HarnessRegistry

    def append(records: list[dict[str, object]]) -> int:
        return HarnessRegistry(control_path).append_events_if_new(records)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return sum(pool.map(append, batches))


def _record_http_phase(
    name: str,
    *,
    port: int,
    token: str,
    attempts: int,
    clients: int,
    expected_host_ids: set[str],
    expected_session_ids: set[str],
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()
    latencies: list[float] = []
    errors: list[str] = []

    def request_one(_: int) -> None:
        started = time.monotonic()
        error: str | None = None
        try:
            status, payload = _request_json(port, "/harness", token=token)
            if status != 200:
                error = f"http_{status}"
            else:
                validate_harness_response(
                    payload,
                    expected_host_ids=expected_host_ids,
                    expected_session_ids=expected_session_ids,
                )
        except urllib.error.HTTPError as exc:
            error = f"http_{exc.code}"
        except urllib.error.URLError:
            error = "url_error"
        except TimeoutError:
            error = "timeout"
        except BenchmarkContractError as exc:
            # These messages describe fixed synthetic response contracts only.
            error = str(exc)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__
        finally:
            with lock:
                latencies.append(time.monotonic() - started)
                if error is not None:
                    errors.append(error)

    with ThreadPoolExecutor(max_workers=clients) as pool:
        list(pool.map(request_one, range(attempts)))
    return summarize_phase(name, latencies, errors=errors)


def record_checked_http_phase(report: dict[str, Any], name: str, **kwargs: Any) -> None:
    """Preserve a failed phase and stop before spending time draining its workload."""
    report["stage"] = name
    phase = _record_http_phase(name, **kwargs)
    report["phases"].append(phase)
    if phase["errors"]:
        raise BenchmarkContractError(f"HTTP phase {name} recorded errors")


def complete_benchmark_sessions(registry: Any, session_ids: list[str]) -> None:
    """Keep both known sentinels visible within the default completed-session cap."""
    for session_id in dict.fromkeys(
        session_ids[1:-1] + [session_ids[0], session_ids[-1]]
    ):
        registry.update_session_status(session_id, "completed")


def _archive_bytes(parquet_dir: Path) -> int:
    return sum(path.stat().st_size for path in parquet_dir.rglob("*.parquet"))


def _outbox_status_for_path(control_path: Path) -> dict[str, int]:
    from drover.server.control_outbox import outbox_status
    from drover.server.db import control_plane_connection

    with control_plane_connection(control_path) as connection:
        status = outbox_status(connection)
    return {
        key: int(status.get(key) or 0)
        for key in ("pending", "claimed", "published_unacknowledged", "acknowledged")
    }


def _wait_for_worker_progress(
    *,
    process: subprocess.Popen[bytes],
    control_path: Path,
    baseline_acknowledged: int,
    minimum_acknowledged: int,
) -> dict[str, int]:
    """Require the actual analytics process to make bounded export progress."""

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkContractError(
                f"analytics worker exited while draining ({process.returncode})"
            )
        status = _outbox_status_for_path(control_path)
        if status["acknowledged"] >= baseline_acknowledged + minimum_acknowledged:
            return status
        time.sleep(0.1)
    raise BenchmarkContractError(
        "analytics worker did not acknowledge its declared backlog"
    )


def _outbox_event_states(control_path: Path, event_ids: list[str]) -> dict[str, str]:
    from drover.server.db import control_plane_connection

    placeholders = ", ".join("?" for _ in event_ids)
    with control_plane_connection(control_path) as connection:
        rows = connection.execute(
            "SELECT event_id, state FROM control_outbox_events "
            f"WHERE event_id IN ({placeholders})",
            event_ids,
        ).fetchall()
    return {str(event_id): str(state) for event_id, state in rows}


def _wait_for_real_worker_drain(
    *,
    process: subprocess.Popen[bytes],
    control_path: Path,
    required_event_ids: list[str],
    timeout_seconds: float,
) -> dict[str, int]:
    """Wait for the configured runtime exporter, with periodic progress output."""

    deadline = time.monotonic() + timeout_seconds
    next_progress = 0.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkContractError(
                f"analytics worker exited while draining ({process.returncode})"
            )
        status = _outbox_status_for_path(control_path)
        states = _outbox_event_states(control_path, required_event_ids)
        if (
            len(states) == len(required_event_ids)
            and all(state == "acknowledged" for state in states.values())
            and status["pending"] == 0
            and status["claimed"] == 0
            and status["published_unacknowledged"] == 0
        ):
            return status
        now = time.monotonic()
        if now >= next_progress:
            print(
                "worker export progress " + json.dumps(status, sort_keys=True),
                flush=True,
            )
            next_progress = now + 30
        time.sleep(0.25)
    raise BenchmarkContractError(
        "configured analytics worker did not drain the export backlog"
    )


def _wait_for_real_worker_retention(
    *,
    process: subprocess.Popen[bytes],
    control_path: Path,
    timeout_seconds: float,
) -> int:
    """Wait for the runtime worker's declared 100-candidate retention passes."""

    from drover.server.db import control_plane_connection

    deadline = time.monotonic() + timeout_seconds
    next_progress = 0.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkContractError(
                f"analytics worker exited during retention ({process.returncode})"
            )
        with control_plane_connection(control_path) as connection:
            remaining = int(
                connection.execute(
                    "SELECT count(*) FROM harness_event_payloads"
                ).fetchone()[0]
            )
        if remaining == 0:
            return remaining
        now = time.monotonic()
        if now >= next_progress:
            print(
                f"worker retention progress remaining_hot_payloads={remaining}",
                flush=True,
            )
            next_progress = now + 30
        time.sleep(0.25)
    raise BenchmarkContractError("configured analytics worker did not finish retention")


def _postgres_sizes(dsn: str, schema: str) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(dsn) as connection:
        schema_bytes = connection.execute(
            """
            SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %s
               AND c.relkind IN ('r', 'm')
            """,
            [schema],
        ).fetchone()[0]
        database_bytes = connection.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        settings = connection.execute("""
            SELECT current_setting('server_version'),
                   current_setting('max_connections'),
                   current_setting('shared_buffers')
            """).fetchone()
    return {
        "benchmark_schema_physical_bytes": int(schema_bytes),
        "benchmark_schema_physical_bytes_method": (
            "sum pg_total_relation_size for tables and materialized views only"
        ),
        "whole_disposable_database_bytes": int(database_bytes),
        "postgres": {
            "server_version": str(settings[0]),
            "max_connections": str(settings[1]),
            "shared_buffers": str(settings[2]),
        },
    }


def _missing_exact_usage_sessions(
    control_path: Path, session_ids: list[str]
) -> list[str]:
    """Return sessions whose persisted harness usage is not exact and current."""

    if not session_ids:
        return []

    from drover.server.db import control_plane_connection

    placeholders = ", ".join("?" for _ in session_ids)
    with control_plane_connection(control_path) as connection:
        rows = connection.execute(
            f"""
            WITH event_watermarks AS (
              SELECT session_id,
                     count(*) AS event_count,
                     COALESCE(max(seq), 0) AS source_seq
                FROM harness_events
               WHERE session_id IN ({placeholders})
               GROUP BY session_id
            )
            SELECT event_watermarks.session_id
              FROM event_watermarks
              LEFT JOIN session_usage_sources usage
                ON usage.session_id = event_watermarks.session_id
               AND usage.source = ?
             WHERE usage.session_id IS NULL
                OR COALESCE(usage.exact, false) = false
                OR usage.source_seq <> event_watermarks.source_seq
                OR usage.source_event_count <> event_watermarks.event_count
             ORDER BY event_watermarks.session_id
            """,
            [*session_ids, "harness_events"],
        ).fetchall()
    return [str(row[0]) for row in rows]


def _wait_for_exact_usage_watermarks(
    *, control_path: Path, session_ids: list[str], timeout_seconds: float = 90.0
) -> float:
    """Require the running worker to persist exact current session usage."""

    started = time.monotonic()
    deadline = started + timeout_seconds
    while time.monotonic() < deadline:
        if not _missing_exact_usage_sessions(control_path, session_ids):
            return time.monotonic() - started
        time.sleep(0.25)
    missing = len(_missing_exact_usage_sessions(control_path, session_ids))
    raise BenchmarkContractError(
        f"analytics worker did not persist exact usage for {missing} session(s)"
    )


def _mark_terminal_recap_dependencies_done(control_path: Path) -> int:
    """Create synthetic recap receipts after real usage rollup.

    LLMs are intentionally disabled in the child environment, so this is the
    deterministic eligibility setup required to exercise the actual retention
    function without pretending a model completed a recap.
    """

    from drover.server.db import control_plane_connection

    with control_plane_connection(control_path) as connection:
        rows = connection.execute("""
            SELECT job.session_id, COALESCE(max(event.seq), 0) AS max_seq
              FROM live_recap_jobs job
              JOIN harness_events event ON event.session_id = job.session_id
             WHERE job.status = 'pending'
             GROUP BY job.session_id
             ORDER BY job.session_id
            """).fetchall()
        for session_id, source_seq in rows:
            connection.execute(
                "UPDATE live_recap_jobs SET desired_source_seq = ? WHERE session_id = ?",
                [source_seq, session_id],
            )
            connection.execute(
                """
                INSERT INTO live_session_recaps
                  (session_id, recap_text, source_seq, generated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (session_id) DO UPDATE SET
                  recap_text = excluded.recap_text,
                  source_seq = excluded.source_seq,
                  generated_at = excluded.generated_at
                """,
                [
                    session_id,
                    "synthetic benchmark completion",
                    source_seq,
                    datetime.now(timezone.utc),
                ],
            )
            connection.execute(
                "UPDATE live_recap_jobs SET status = 'done' WHERE session_id = ?",
                [session_id],
            )
    return len(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DROVER_TEST_POSTGRES_DSN", ""))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/internal/postgres-benchmark/result.json"),
    )
    parser.add_argument(
        "--work-root", type=Path, default=Path("docs/internal/postgres-benchmark/runs")
    )
    parser.add_argument("--hosts", type=int, default=DEFAULT_HOSTS)
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS)
    parser.add_argument("--payload-bytes", type=int, default=DEFAULT_PAYLOAD_BYTES)
    parser.add_argument("--phase-requests", type=int, default=DEFAULT_PHASE_REQUESTS)
    parser.add_argument("--clients", type=int, default=DEFAULT_CLIENTS)
    parser.add_argument(
        "--drain-timeout-seconds", type=float, default=DEFAULT_DRAIN_TIMEOUT_SECONDS
    )
    parser.add_argument("--keep-workdir", action="store_true")
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if not args.dsn:
        raise BenchmarkContractError("--dsn or DROVER_TEST_POSTGRES_DSN is required")
    if args.hosts < 1 or args.sessions < args.hosts or args.events < args.sessions:
        raise BenchmarkContractError(
            "hosts, sessions, and events must form a nonempty workload"
        )
    if args.payload_bytes < 512:
        raise BenchmarkContractError("payload-bytes must be at least 512")
    if args.phase_requests < MIN_PERCENTILE_SAMPLES:
        raise BenchmarkContractError(
            f"phase-requests must be at least {MIN_PERCENTILE_SAMPLES} for p99 evidence"
        )
    if not 1 <= args.clients <= 8:
        raise BenchmarkContractError("clients must be between 1 and 8")
    if args.drain_timeout_seconds < 60:
        raise BenchmarkContractError("drain-timeout-seconds must be at least 60")


def main() -> int:
    args = _parse_args()
    _validate_args(args)

    from drover.schema import bootstrap_control_plane_store
    from drover.server.control_migration import initialize_empty_control_store
    from drover.server.control_store import close_control_store, configure_control_store
    from drover.server.harness.registry import HarnessRegistry

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work_root))
    schema = f"drover_benchmark_{uuid4().hex}"
    api_port, worker_port, worker_metrics_port = (
        _free_loopback_port(),
        _free_loopback_port(),
        _free_loopback_port(),
    )
    api_token = _scratch_token()
    api_to_worker_token = _scratch_token()
    worker_to_api_token = _scratch_token()
    lake_guard = run_root / "api-lake-denied"
    lake_guard.write_text("API benchmark role must not open a lake", encoding="utf-8")
    api_lake = lake_guard / "analytics.duckdb"
    worker_lake = run_root / "worker" / "analytics.duckdb"
    worker_lake.parent.mkdir()
    api_config = run_root / "api.toml"
    worker_config = run_root / "worker.toml"
    _write_config(
        api_config,
        duckdb_path=api_lake,
        root=run_root / "api-runtime",
        schema=schema,
        metrics_port=api_port,
        api_port=api_port,
        worker_port=worker_port,
        api_token=api_token,
    )
    _write_config(
        worker_config,
        duckdb_path=worker_lake,
        root=run_root / "worker-runtime",
        schema=schema,
        metrics_port=worker_metrics_port,
        api_port=api_port,
        worker_port=worker_port,
        api_token=api_token,
    )
    home = run_root / "home"
    home.mkdir()
    environment = synthetic_child_environment(
        home=home,
        dsn=args.dsn,
        path=os.environ.get("PATH", ""),
        api_token=api_token,
        api_to_worker_token=api_to_worker_token,
        worker_to_api_token=worker_to_api_token,
    )
    control_config = benchmark_control_store_config(schema)
    control_path = run_root / "central-selector"
    api_process: subprocess.Popen[bytes] | None = None
    worker_process: subprocess.Popen[bytes] | None = None
    api_log = (run_root / "api.log").open("wb")
    worker_log = (run_root / "worker.log").open("wb")
    fixture_started = time.monotonic()
    report: dict[str, Any] = {
        "benchmark": "postgres_control_plane_synthetic_http",
        "source": "generated only",
        "outcome": "running",
        "stage": "bootstrap",
        "phases": [],
        "limits": {
            "p99_target_ms": 250,
            "minimum_percentile_samples": MIN_PERCENTILE_SAMPLES,
            "api_worker_pool_max_size": 2,
            "writer_threads": WRITER_THREADS,
            "runtime_exporter": {
                "batch_size": 100,
                "flush_age_seconds": 5,
                "poll_seconds": 1,
                "retention_limit": 100,
            },
            "real_worker_drain_timeout_seconds": args.drain_timeout_seconds,
            "fleet_path": "/harness",
            "completed_session_sentinels": "most recently completed within default archive window",
        },
    }
    try:
        configure_control_store(control_path, control_config)
        bootstrap_control_plane_store(control_path)
        initialize_empty_control_store(control_path)
        registry = HarnessRegistry(control_path)
        host_ids = [f"benchmark-host-{index}" for index in range(args.hosts)]
        session_ids = [
            f"benchmark-session-{index:03d}" for index in range(args.sessions)
        ]
        for index, host_id in enumerate(host_ids):
            registry.register_host(
                host_id=host_id,
                display_name=f"Synthetic benchmark host {index}",
                kind="test",
                status="online",
            )
        for index, session_id in enumerate(session_ids):
            registry.create_session(
                host_id=host_ids[index % len(host_ids)],
                harness="codex",
                command="synthetic-benchmark",
                session_id=session_id,
                mode="structured",
            )

        records, logical_payload_bytes = _build_records(
            start=0,
            count=args.events,
            sessions=session_ids,
            payload_bytes=args.payload_bytes,
        )
        batches = [
            records[index : index + INSERT_BATCH_SIZE]
            for index in range(0, len(records), INSERT_BATCH_SIZE)
        ]
        seeded_at = time.monotonic()
        report["fixture_setup_seconds"] = round(seeded_at - fixture_started, 3)
        report["startup_initial_data_events"] = 0

        report["stage"] = "api_startup"
        api_started = time.monotonic()
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(api_config),
                "run",
                "--role",
                "api",
            ],
            stdin=subprocess.DEVNULL,
            stdout=api_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        _wait_for_process(api_process, api_port, "/healthz", token=api_token)
        startup_seconds = time.monotonic() - api_started
        report["startup_seconds"] = round(startup_seconds, 3)
        report["stage"] = "startup_http"
        record_checked_http_phase(
            report,
            "startup",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0]},
            expected_session_ids={session_ids[0]},
        )

        report["stage"] = "worker_startup"
        worker_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(worker_config),
                "run",
                "--role",
                "analytics",
                "--no-otlp",
                "--no-mcp",
                "--no-summarizer",
                "--no-briefs",
                "--no-embeddings",
            ],
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        _wait_for_process(
            worker_process,
            worker_port,
            "/_internal/analytics/health",
            token=api_token,
            headers={"X-Drover-Api-To-Analytics": api_to_worker_token},
        )

        writer_error: list[BaseException] = []

        def seed() -> None:
            try:
                inserted = _append_batches(
                    control_path, batches, threads=WRITER_THREADS
                )
                if inserted != args.events:
                    raise BenchmarkContractError(
                        f"expected {args.events} inserted events, got {inserted}"
                    )
            except BaseException as exc:  # surfaced after the HTTP phase
                writer_error.append(exc)

        writer = threading.Thread(target=seed, name="synthetic-benchmark-writer")
        writer.start()
        report["stage"] = "normal_concurrent_ingest"
        record_checked_http_phase(
            report,
            "normal_concurrent_ingest",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        writer.join(timeout=300)
        if writer.is_alive():
            raise BenchmarkContractError("concurrent synthetic ingest did not finish")
        if writer_error:
            raise writer_error[0]
        report["concurrent_ingest_seconds"] = round(time.monotonic() - seeded_at, 3)
        report["full_scale_committed_events"] = args.events
        report["stage"] = "normal_steady_after_ingest"
        record_checked_http_phase(
            report,
            "normal_steady_after_ingest",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        normal_backlog = _outbox_status_for_path(control_path)
        normal_after = _wait_for_worker_progress(
            process=worker_process,
            control_path=control_path,
            baseline_acknowledged=normal_backlog["acknowledged"],
            minimum_acknowledged=100,
        )
        report["analytics_worker_normal_progress"] = {
            "before": normal_backlog,
            "after": normal_after,
            "acknowledged_delta": normal_after["acknowledged"]
            - normal_backlog["acknowledged"],
        }

        report["stage"] = "worker_outage"
        _stop_process(worker_process)
        worker_process = None
        outage_backlog = _outbox_status_for_path(control_path)
        outage_records, outage_logical_payload_bytes = _build_records(
            start=args.events,
            count=100,
            sessions=session_ids,
            payload_bytes=args.payload_bytes,
        )
        for record in outage_records:
            record["event_type"] = "assistant_output"
            record["payload"].pop("turn_complete", None)
        outage_committed = _append_batches(
            control_path,
            [outage_records],
            threads=1,
        )
        if outage_committed != len(outage_records):
            raise BenchmarkContractError(
                "worker-outage synthetic ingest did not commit"
            )
        outage_event_ids = [str(record["event_id"]) for record in outage_records]
        record_checked_http_phase(
            report,
            "worker_outage",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        outage_status, outage_payload = _request_json(
            api_port, "/metrics", token=api_token
        )
        if outage_status != 503:
            raise BenchmarkContractError("worker outage did not produce HTTP 503")
        validate_worker_outage_response(outage_payload)

        report["stage"] = "worker_recovery"
        worker_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(worker_config),
                "run",
                "--role",
                "analytics",
                "--no-otlp",
                "--no-mcp",
                "--no-summarizer",
                "--no-briefs",
                "--no-embeddings",
            ],
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        _wait_for_process(
            worker_process,
            worker_port,
            "/_internal/analytics/health",
            token=api_token,
            headers={"X-Drover-Api-To-Analytics": api_to_worker_token},
        )
        record_checked_http_phase(
            report,
            "worker_recovery",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        recovery_after = _wait_for_worker_progress(
            process=worker_process,
            control_path=control_path,
            baseline_acknowledged=outage_backlog["acknowledged"],
            minimum_acknowledged=100,
        )
        report["analytics_worker_recovery_progress"] = {
            "before": outage_backlog,
            "after": recovery_after,
            "acknowledged_delta": recovery_after["acknowledged"]
            - outage_backlog["acknowledged"],
        }

        # The configured default-role exporter remains the only drain. Its
        # bounded deadline and settings are reported with the actual progress.
        record_checked_http_phase(
            report,
            "worker_export",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        report["stage"] = "worker_export_drain"
        export_drain_started = time.monotonic()
        export_after = _wait_for_real_worker_drain(
            process=worker_process,
            control_path=control_path,
            required_event_ids=outage_event_ids,
            timeout_seconds=args.drain_timeout_seconds,
        )
        report["analytics_worker_full_export_drain"] = {
            "elapsed_seconds": round(time.monotonic() - export_drain_started, 3),
            "after": export_after,
            "outage_added_events": len(outage_event_ids),
            "outage_added_events_committed": outage_committed,
            "outage_added_events_acknowledged": len(outage_event_ids),
        }
        complete_benchmark_sessions(registry, session_ids)
        report["stage"] = "usage_validation"
        usage_wait_seconds = _wait_for_exact_usage_watermarks(
            control_path=control_path, session_ids=session_ids
        )
        recap_rows = _mark_terminal_recap_dependencies_done(control_path)
        if recap_rows != args.sessions:
            raise BenchmarkContractError(
                "synthetic terminal sessions lacked recap dependencies"
            )
        record_checked_http_phase(
            report,
            "retention",
            port=api_port,
            token=api_token,
            attempts=args.phase_requests,
            clients=args.clients,
            expected_host_ids={host_ids[0], host_ids[-1]},
            expected_session_ids={session_ids[0], session_ids[-1]},
        )
        report["stage"] = "retention"
        retention_started = time.monotonic()
        remaining_hot_payloads = _wait_for_real_worker_retention(
            process=worker_process,
            control_path=control_path,
            timeout_seconds=args.drain_timeout_seconds,
        )
        report["analytics_worker_retention"] = {
            "elapsed_seconds": round(time.monotonic() - retention_started, 3),
            "remaining_hot_payloads": remaining_hot_payloads,
            "runtime_retention_limit": 100,
        }
        _stop_process(worker_process)
        worker_process = None
        report["usage_rollup"] = {
            "exact_current_sessions": args.sessions,
            "worker_wait_seconds": round(usage_wait_seconds, 3),
            "synthetic_recap_dependencies_completed": recap_rows,
        }
        report["fixture"] = {
            "hosts": args.hosts,
            "sessions": args.sessions,
            "events": args.events + outage_committed,
            "payload_target_bytes": args.payload_bytes,
            "logical_payload_bytes": logical_payload_bytes
            + outage_logical_payload_bytes,
            "varied_deterministic_envelopes": True,
            "ingest_path": "direct HarnessRegistry batch writes under PostgreSQL contention",
            "ingest_path_limit": "not HTTP or host-relay ingestion throughput",
        }
        report["sizes"] = {
            **_postgres_sizes(args.dsn, schema),
            "immutable_archive_bytes": _archive_bytes(
                run_root / "worker-runtime" / "parquet"
            ),
        }
        report["platform"] = {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        }
        report["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["limitations"] = [
            "Synthetic loopback evidence only; it does not prove production capacity, TLS, WAL, power-loss, or cutover behavior.",
            "The API role uses a denied scratch lake path and all child credential environments are synthetic allowlists.",
            "Retention uses real export, usage rollup, manifest verification, and prune paths; synthetic recap receipts stand in for disabled LLM recap generation.",
            "Worker recovery evidence includes acknowledgement of events committed only while the worker was down, followed by a real default-worker export and retention drain.",
            "The PostgreSQL schema size is the workload-specific measurement; whole disposable database size may include unrelated temporary catalog history.",
        ]
        if any(phase["errors"] for phase in report["phases"]):
            raise BenchmarkContractError(
                "one or more benchmark HTTP phases recorded errors"
            )
        if any(
            phase["percentiles"]["status"] != "measured" for phase in report["phases"]
        ):
            raise BenchmarkContractError(
                "one or more benchmark phases lacked p99 samples"
            )
        outcome_status = finalize_benchmark_outcome(report)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return outcome_status
    except BaseException as exc:
        report["outcome"] = "failed"
        report["failure"] = {
            "stage": str(report.get("stage", "bootstrap")),
            "type": type(exc).__name__,
        }
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        raise
    finally:
        _stop_process(worker_process)
        _stop_process(api_process)
        api_log.close()
        worker_log.close()
        close_control_store(control_path)
        try:
            import psycopg

            with psycopg.connect(args.dsn, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            if not args.keep_workdir:
                shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkContractError as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
