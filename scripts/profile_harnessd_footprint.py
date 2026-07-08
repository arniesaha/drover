#!/usr/bin/env python3
"""Measure drover-server harnessd versus the standalone drover-harnessd process."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _rss_kib(pid: int) -> int:
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return int(result.stdout.strip())


def _wait_for_health(url: str, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter with Drover installed/importable",
    )
    parser.add_argument("--sample-delay-s", type=float, default=1.0)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="drover-harnessd-footprint-"))
    try:
        cfg = tmp / "config.toml"
        cfg.write_text(f"""
[paths]
incoming_dir = "{tmp / 'incoming'}"
parquet_dir = "{tmp / 'parquet'}"
duckdb_path = "{tmp / 'drover.duckdb'}"
processed_retention_days = 7

[server]
otlp_grpc_port = 0
mcp_http_port = 0
metrics_http_port = 0

[agent]
agent_id = "footprint"
principal_id = "footprint"
""")
        env = {**os.environ, "PYTHONPATH": "src"}
        commands = {
            "standalone": [
                args.python,
                "-m",
                "drover.server.harness.cli",
                "--config",
                str(cfg),
                "--host-id",
                "footprint-standalone",
                "--listen",
                "127.0.0.1:18081",
            ],
            "drover_server_subcommand": [
                args.python,
                "-m",
                "drover.server.__main__",
                "--config",
                str(cfg),
                "harnessd",
                "--host-id",
                "footprint-server",
                "--listen",
                "127.0.0.1:18082",
            ],
        }
        results = {}
        for name, command in commands.items():
            with subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as proc:
                try:
                    port = 18081 if name == "standalone" else 18082
                    _wait_for_health(f"http://127.0.0.1:{port}/healthz", timeout_s=10)
                    time.sleep(args.sample_delay_s)
                    results[name] = {
                        "pid": proc.pid,
                        "rss_kib": _rss_kib(proc.pid),
                        "command": command,
                    }
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except Exception:
                            pass
                        proc.wait(timeout=5)
        print(json.dumps(results, indent=2, sort_keys=True))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
