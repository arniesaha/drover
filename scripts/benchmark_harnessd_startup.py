#!/usr/bin/env python3
"""Measure fresh harnessd startup against a 50k-event local control spool.

Run from the repository root:

    uv run python scripts/benchmark_harnessd_startup.py

The fixture is deliberately a host-local DuckDB control registry: 200 sessions,
250 events each.  It prints time-to-/healthz and the maximum resident set
observed while the new harnessd process starts.  The temporary fixture is
removed unless ``--keep`` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

from drover.server.db import control_plane_path
from drover.server.harness.schema import bootstrap_harness_tables

EVENT_COUNT = 50_000
SESSION_COUNT = 200
HEALTHZ_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.05


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_registry(lake_path: Path) -> None:
    registry_path = control_plane_path(lake_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(registry_path))
    try:
        bootstrap_harness_tables(con)
        con.execute(
            """
            INSERT INTO harness_sessions (session_id, host_id, harness, command, status)
            SELECT 'session-' || i::VARCHAR, 'benchmark-host', 'codex', 'benchmark', 'running'
              FROM range(?) AS sessions(i)
            """,
            [SESSION_COUNT],
        )
        con.execute(
            """
            INSERT INTO harness_events (
              event_id, session_id, event_type, normalized_type,
              normalized_source, content_preview, payload_json, created_at, seq, dedup_key
            )
            SELECT 'event-' || i::VARCHAR,
                   'session-' || (i % ?)::VARCHAR,
                   'terminal.output', 'output', 'terminal', 'benchmark',
                   '{"text":"benchmark"}', now(), (i // ?)::INTEGER,
                   'dedup-' || i::VARCHAR
              FROM range(?) AS events(i)
            """,
            [SESSION_COUNT, SESSION_COUNT, EVENT_COUNT],
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _rss_kib(pid: int) -> int:
    completed = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    text = completed.stdout.strip()
    return int(text) if text else 0


def _wait_for_healthz(process: subprocess.Popen[bytes], port: int) -> tuple[float, int]:
    started = time.monotonic()
    peak_rss_kib = 0
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() - started < HEALTHZ_TIMEOUT_S:
        if process.poll() is not None:
            raise RuntimeError(f"harnessd exited with {process.returncode}")
        peak_rss_kib = max(peak_rss_kib, _rss_kib(process.pid))
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    return time.monotonic() - started, peak_rss_kib
        except (urllib.error.URLError, TimeoutError):
            time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"harnessd did not answer /healthz within {HEALTHZ_TIMEOUT_S}s")


def _write_config(path: Path, lake_path: Path, root: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[paths]",
                f"duckdb_path = {json.dumps(str(lake_path))}",
                f'parquet_dir = {json.dumps(str(root / "parquet"))}',
                f'incoming_dir = {json.dumps(str(root / "incoming"))}',
                f'worktrees_dir = {json.dumps(str(root / "worktrees"))}',
                "",
            ]
        )
    )


def measure(root: Path) -> dict[str, float | int]:
    lake_path = root / "drover.duckdb"
    config_path = root / "benchmark.toml"
    _seed_registry(lake_path)
    _write_config(config_path, lake_path, root)
    port = _free_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "drover.server.harness.cli",
            "--config",
            str(config_path),
            "--host-id",
            "benchmark-host",
            "--listen",
            f"127.0.0.1:{port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DROVER_CONTROL_PLANE_PIN": "0"},
    )
    try:
        healthz_seconds, peak_rss_kib = _wait_for_healthz(process, port)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return {
        "events": EVENT_COUNT,
        "sessions": SESSION_COUNT,
        "time_to_healthz_seconds": round(healthz_seconds, 3),
        "peak_rss_mib": round(peak_rss_kib / 1024, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="keep the fixture directory"
    )
    args = parser.parse_args()
    if args.keep:
        root = Path(tempfile.mkdtemp(prefix="drover-harnessd-startup-"))
        print(f"fixture={root}")
        print(json.dumps(measure(root), sort_keys=True))
        return
    with tempfile.TemporaryDirectory(prefix="drover-harnessd-startup-") as temporary:
        print(json.dumps(measure(Path(temporary)), sort_keys=True))


if __name__ == "__main__":
    main()
