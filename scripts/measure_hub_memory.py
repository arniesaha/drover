"""Busy-hub synthetic RSS attribution probe (never reads production data).

uv sync --extra dev; python scripts/measure_hub_memory.py --prepare /tmp/hub-probe
DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1 DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=6GB \
  python scripts/measure_hub_memory.py /tmp/hub-probe --minutes 5

Starts the real local HTTP handler and alternates the two routes polled by the
app: ``/cockpit/overview`` and ``/analytics``. The fixture is created only by
``--prepare``; passing an existing directory to that command is refused. Each
measurement must use a fresh process.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import runpy
import threading
import time
import tracemalloc
import urllib.request
from pathlib import Path
from sys import platform

import duckdb
import psutil
import pyarrow as pa

from drover.server.advisory.service import InsightsService
from drover.server.cockpit.service import CockpitService
from drover.server.db import open_duckdb_connection, sql_path_literal
from drover.server.metrics import MetricsCollector
from drover.server.process_memory import (
    HUB_MEASUREMENT,
    hub_memory_bytes,
    process_footprint,
)
from drover.server.web.app import start_metrics_server


def prepare(root, sessions, spans, files):
    root.mkdir(parents=True, exist_ok=False)
    fixture = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "tests/test_cockpit_analytics.py")
    )
    con = fixture["_analytics_file_connection"](root / "store.duckdb")
    con.execute(f"""
        INSERT INTO sessions SELECT 'session-' || i, 'agent', NULL, now(), now(),
          'example', 'project-' || (i % 20), 'main', '{{}}' FROM range({sessions}) t(i);
        INSERT INTO spans_enriched SELECT 'span-' || i, 'session-' || (i % {sessions}),
          now(), now(), 1000, 'claude-code', 'anthropic', 'synthetic', 'synthetic',
          'example', 'project-' || (i % 20), 100, 80, 20, 0.01, 10, 0 FROM range({spans}) t(i);
    """)
    # Production-shaped file fanout, while preserving the analytics test schema.
    for table in ("sessions", "spans_enriched"):
        directory = root / table
        directory.mkdir(exist_ok=True)
        count = sessions if table == "sessions" else spans
        for index in range(files):
            first = index * count // files
            size = (index + 1) * count // files - first
            con.execute(
                f'COPY (SELECT * FROM {table} LIMIT {size} OFFSET {first}) TO {sql_path_literal(directory / (str(index) + ".parquet"))} (FORMAT PARQUET)'
            )
        con.execute(f"DROP TABLE {table}")
        con.execute(
            f'CREATE VIEW {table} AS SELECT * FROM read_parquet({sql_path_literal(str(directory / "*.parquet"))}, union_by_name=true)'
        )
    con.close()
    (root / "workload.json").write_text(
        json.dumps(dict(sessions=sessions, spans=spans, files_per_view=files))
    )


def _request(base: str, path: str) -> dict:
    started = time.monotonic()
    with urllib.request.urlopen(base + path, timeout=30) as response:
        payload = json.loads(response.read())
    return {
        "path": path,
        "status": response.status,
        "seconds": round(time.monotonic() - started, 3),
        "activity_status": payload["activity"]["status"],
    }


def measure(root, *, live_reader=False, minutes=1.0, interval_seconds=1.0):
    """Exercise the HTTP poll paths and return a JSON-serializable report.

    ``minutes=0`` is useful for the focused smoke test: it still makes one
    request to each endpoint. Production-style runs use a positive duration
    and sample before/after every request rather than treating a lifetime
    high-water mark as current RSS.
    """
    if minutes < 0 or interval_seconds < 0:
        raise ValueError("minutes and interval_seconds must be non-negative")
    logging.basicConfig(level=logging.INFO)
    tracemalloc.start(10)
    baseline = tracemalloc.take_snapshot()
    process = psutil.Process()
    stop = threading.Event()
    peaks = {"parent_rss": 0, "children_rss": 0}

    def monitor():
        while not stop.wait(0.01):
            peaks["parent_rss"] = max(peaks["parent_rss"], process.memory_info().rss)
            children = 0
            for child in process.children(recursive=True):
                try:
                    children += child.memory_info().rss
                except psutil.Error:
                    pass
            peaks["children_rss"] = max(peaks["children_rss"], children)

    thread = threading.Thread(target=monitor)
    thread.start()
    samples = []

    def sample(phase, con=None):
        tags = (
            dict(
                con.execute(
                    "SELECT tag, memory_usage_bytes FROM duckdb_memory()"
                ).fetchall()
            )
            if con
            else {}
        )
        current, peak = tracemalloc.get_traced_memory()
        hub_bytes = hub_memory_bytes(process.pid)
        footprint = process_footprint(process.pid) if platform == "darwin" else None
        samples.append(
            dict(
                phase=phase,
                rss=process.memory_info().rss,
                hub_measurement=HUB_MEASUREMENT,
                hub_memory=hub_bytes,
                **(
                    {
                        "physical_footprint": footprint[0],
                        "lifetime_peak_footprint": footprint[1],
                    }
                    if footprint is not None
                    else {}
                ),
                python_traced=current,
                python_peak=peak,
                arrow=pa.total_allocated_bytes(),
                duckdb=tags,
                external_cache=(
                    con.execute(
                        "SELECT coalesce(sum(nr_bytes),0) FROM duckdb_external_file_cache()"
                    ).fetchone()[0]
                    if con
                    else 0
                ),
                **peaks,
            )
        )

    server = None
    service = None
    requests = []
    try:
        sample("imports")
        # Retain the live instance as the runtime pin/background workers do.
        with open_duckdb_connection(root / "store.duckdb") as live:
            sample("startup_open", live)
            for table in ("sessions", "spans_enriched"):
                live.execute(
                    f'CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet({sql_path_literal(str(root / table / "*.parquet"))}, union_by_name=true)'
                )
            sample("startup_views", live)
            insights = InsightsService(root / "store.duckdb")
            from drover.schema import bootstrap_control_plane_store

            bootstrap_control_plane_store(root / "store.duckdb")
            service = CockpitService(
                duckdb_path=root / "store.duckdb",
                provider_usage=None,
                advisory_repository=insights.repository,
                spans_enabled=True,
            )
            if live_reader:
                service._isolated_reader_supported = False
            collector = MetricsCollector(
                duckdb_path=root / "store.duckdb",
                incoming_dir=root / "incoming",
                summarizer_report={},
            )
            collector.cockpit_service = service
            server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            deadline = time.monotonic() + minutes * 60
            index = 0
            # Always call both routes once. New day values avoid the cockpit
            # response cache, then a timed run simulates continued app polls.
            while index < 2 or time.monotonic() < deadline:
                path = (
                    f"/cockpit/overview?days={7 + index}"
                    if index % 2 == 0
                    else f"/analytics?days={7 + index}"
                )
                requests.append(_request(base, path))
                assert requests[-1]["activity_status"] == "ok", requests[-1]
                sample(f"request_{index}", live)
                if interval_seconds:
                    time.sleep(interval_seconds)
                index += 1
            sample("idle", live)
        sample("closed")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        stop.set()
        thread.join()
    after = tracemalloc.take_snapshot()
    return dict(
        synthetic=True,
        workload=json.loads((root / "workload.json").read_text()),
        duckdb_version=duckdb.__version__,
        isolated_reader=service is not None and service._isolated_reader_supported,
        spans_enabled=True,
        duration_minutes=minutes,
        endpoint_requests=requests,
        samples=samples,
        # A diff, not merely the largest currently-live Python allocations:
        # this identifies Python-side growth separately from native DuckDB RSS.
        python_growth_top=[
            dict(bytes=x.size_diff, count=x.count_diff, traceback=str(x.traceback))
            for x in after.compare_to(baseline, "lineno")[:10]
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--sessions", type=int, default=50000)
    parser.add_argument("--spans", type=int, default=1000000)
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument(
        "--live-reader",
        action="store_true",
        help="Exercise the non-clone foreground fallback",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=1.0,
        help="How long to poll after the required first request to each route.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="Delay between polls; zero is useful for a smoke run.",
    )
    args = parser.parse_args()
    if min(args.sessions, args.spans, args.files) < 1:
        parser.error("sessions, spans and files must be positive")
    if args.prepare:
        prepare(args.root.resolve(), args.sessions, args.spans, args.files)
    else:
        print(
            json.dumps(
                measure(
                    args.root.resolve(),
                    live_reader=args.live_reader,
                    minutes=args.minutes,
                    interval_seconds=args.interval_seconds,
                )
            )
        )
