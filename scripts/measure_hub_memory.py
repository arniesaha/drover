"""Busy-hub synthetic footprint attribution probe (never reads production data).

uv sync --extra dev; python scripts/measure_hub_memory.py --prepare /tmp/hub-probe
DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1 DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=4GB \
  python scripts/measure_hub_memory.py /tmp/hub-probe --minutes 5

Starts the real local HTTP handler and polls the cockpit routes plus the
advisory route used by the app: ``/cockpit/overview``, ``/analytics`` and
``/insights``. The fixture is created only by ``--prepare``; passing an
existing directory to that command is refused. Each measurement must use a
fresh process.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import runpy
import subprocess
import sys
import threading
import time
import tracemalloc
import urllib.request
from collections import defaultdict
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

# The production app loads both rankings across its selectable windows, then
# the overview and advisory lists. Keep this as data so the fixture cannot
# silently stop covering the reported shape.
PRODUCTION_ENDPOINTS = tuple(
    f"/analytics?days={days}&metric={metric}"
    for days in (7, 30, 90, 365)
    for metric in ("tokens", "sessions")
) + ("/cockpit/overview?days=365", "/insights")

# macOS budgets physical footprint, Linux RSS. The fixture is intentionally
# lower than the release gate: crossing this value means a hub-owned allocation
# escaped the disposable reader and needs investigation before rollout.
HUB_PEAK_BUDGET_BYTES = int(1.5 * 1024**3)


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
    _prepare_legacy_bootstrap_fixture(root, spans=spans, files=files)


def _prepare_legacy_bootstrap_fixture(root: Path, *, spans: int, files: int) -> None:
    """Create a separate, production-shaped source for the legacy view probe."""
    from drover.schema import bootstrap

    parquet = root / "legacy-bootstrap-parquet"
    store = root / "legacy-bootstrap.duckdb"
    bootstrap(parquet_dir=parquet, duckdb_path=store, bind_parquet_views=False)
    source = duckdb.connect()
    try:
        for index in range(files):
            first = index * spans // files
            size = (index + 1) * spans // files - first
            directory = parquet / "spans" / f"date=2026-10-{index % 20 + 1:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            source.execute(f"""
                COPY (
                  SELECT
                    'trace-' || i AS trace_id,
                    'span-' || i AS span_id,
                    'synthetic' AS name,
                    'drover' AS service_name,
                    TIMESTAMPTZ '2026-10-01 00:00:00+00' AS start_time,
                    TIMESTAMPTZ '2026-10-01 00:00:01+00' AS end_time,
                    1000.0 AS duration_ms,
                    'session-' || (i % 20000) AS session_id,
                    'agent' AS agent_id
                  FROM range({first}, {first + size}) t(i)
                ) TO {sql_path_literal(directory / f'part-{index}.parquet')} (FORMAT PARQUET)
                """)
    finally:
        source.close()


def _request(base: str, path: str) -> dict:
    started = time.monotonic()
    with urllib.request.urlopen(base + path, timeout=30) as response:
        payload = json.loads(response.read())
    return {
        "path": path,
        "status": response.status,
        "seconds": round(time.monotonic() - started, 3),
        **(
            {"activity_status": payload["activity"]["status"]}
            if "activity" in payload
            else {}
        ),
    }


def _growth_summary(samples: list[dict]) -> dict[str, dict[str, int]]:
    """Report bracketed per-route movement without claiming causation.

    Requests are sequential, so an endpoint's last ``after`` includes work
    done by the routes before it. The raw samples remain the attribution
    source; these aggregates make a multi-minute run practical to compare.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[sample["path"]].append(sample)
    return {
        path: {
            "calls": len(values),
            "rss_net_growth_bytes": values[-1]["rss_after"] - values[0]["rss_before"],
            "hub_net_growth_bytes": values[-1]["hub_after"] - values[0]["hub_before"],
            "hub_lifetime_net_growth_bytes": values[-1]["hub_lifetime_after"]
            - values[0]["hub_lifetime_before"],
            "rss_max_step_growth_bytes": max(
                value["rss_growth_bytes"] for value in values
            ),
            "hub_max_step_growth_bytes": max(
                value["hub_growth_bytes"] for value in values
            ),
            "hub_lifetime_max_step_growth_bytes": max(
                value["hub_lifetime_growth_bytes"] for value in values
            ),
        }
        for path, values in grouped.items()
    }


def _hub_lifetime_peak(pid: int) -> int:
    """Return this process's kernel lifetime memory high-water mark in bytes."""
    if platform == "darwin":
        return process_footprint(pid)[1]
    if pid != os.getpid():
        raise ValueError("Linux lifetime peak is only available for this process")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Darwin reports ru_maxrss in bytes, but macOS uses the more accurate
    # physical-footprint high-water mark above. Linux reports KiB.
    return peak * 1024 if platform.startswith("linux") else peak


def _measure_startup_view_peak_mode(root: Path, *, legacy_views: bool) -> dict:
    """One fresh-process startup measurement for the selected view-bind mode."""
    from drover.schema import bootstrap

    process = psutil.Process()
    store = root / "legacy-bootstrap.duckdb"
    parquet = root / "legacy-bootstrap-parquet"
    before = hub_memory_bytes(process.pid)
    lifetime_before = _hub_lifetime_peak(process.pid)
    bootstrap(parquet_dir=parquet, duckdb_path=store, bind_parquet_views=legacy_views)
    after = hub_memory_bytes(process.pid)
    lifetime_after = _hub_lifetime_peak(process.pid)
    return {
        "measurement": HUB_MEASUREMENT,
        "legacy_views": legacy_views,
        "current_before": before,
        "current_after": after,
        "lifetime_before": lifetime_before,
        "lifetime_after": lifetime_after,
    }


def measure_startup_view_peak(root: Path) -> dict:
    """Compare legacy binding and DuckLake's bounded startup in fresh children."""

    def run(mode: str) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(root),
                "--startup-view-peak-mode",
                mode,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    return {
        "measurement": HUB_MEASUREMENT,
        "legacy_view_bind": run("legacy"),
        "ducklake_bounded_startup": run("ducklake"),
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
    peaks = {
        "parent_rss": 0,
        "children_rss": 0,
        "parent_hub_memory": 0,
        "parent_lifetime_peak_hub_memory": 0,
    }

    def monitor():
        while not stop.wait(0.01):
            peaks["parent_rss"] = max(peaks["parent_rss"], process.memory_info().rss)
            try:
                peaks["parent_hub_memory"] = max(
                    peaks["parent_hub_memory"], hub_memory_bytes(process.pid)
                )
                peaks["parent_lifetime_peak_hub_memory"] = max(
                    peaks["parent_lifetime_peak_hub_memory"],
                    _hub_lifetime_peak(process.pid),
                )
            except OSError:
                pass
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
    path_growth = []
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
            insights = InsightsService(
                root / "store.duckdb", config_path=root / "config.toml"
            )
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
            collector.advisory_service = insights
            server = start_metrics_server(host="127.0.0.1", port=0, collector=collector)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            deadline = time.monotonic() + minutes * 60
            index = 0
            # Every cycle covers the same 7/30/90/365 ranking shape as the
            # production app, followed by its overview and advisory reads.
            # Every path has a distinct filter/cache key, so a warm result
            # cannot hide one of the analytical requests.
            while index < 1 or time.monotonic() < deadline:
                for path in PRODUCTION_ENDPOINTS:
                    before_rss = process.memory_info().rss
                    before_hub = hub_memory_bytes(process.pid)
                    before_hub_lifetime = _hub_lifetime_peak(process.pid)
                    request = _request(base, path)
                    if "activity_status" in request:
                        assert request["activity_status"] == "ok", request
                    after_rss = process.memory_info().rss
                    after_hub = hub_memory_bytes(process.pid)
                    after_hub_lifetime = _hub_lifetime_peak(process.pid)
                    requests.append(request)
                    path_growth.append(
                        {
                            "path": path.split("?", 1)[0],
                            "rss_before": before_rss,
                            "rss_after": after_rss,
                            "rss_growth_bytes": after_rss - before_rss,
                            "hub_before": before_hub,
                            "hub_after": after_hub,
                            "hub_growth_bytes": after_hub - before_hub,
                            "hub_lifetime_before": before_hub_lifetime,
                            "hub_lifetime_after": after_hub_lifetime,
                            "hub_lifetime_growth_bytes": (
                                after_hub_lifetime - before_hub_lifetime
                            ),
                        }
                    )
                    sample(f"request_{index}_{path.split('?', 1)[0][1:]}", live)
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
        path_growth=path_growth,
        path_growth_summary=_growth_summary(path_growth),
        # This is the budget metric: physical footprint on macOS and RSS on
        # Linux. The lifetime form preserves a short peak between requests.
        peak_hub_memory=max(
            [sample["hub_memory"] for sample in samples] + [peaks["parent_hub_memory"]]
        ),
        hub_peak_budget_bytes=HUB_PEAK_BUDGET_BYTES,
        lifetime_peak_hub_memory=max(
            [
                sample.get("lifetime_peak_footprint", sample["hub_memory"])
                for sample in samples
            ]
            + [peaks["parent_lifetime_peak_hub_memory"]]
        ),
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
        "--startup-view-peak",
        action="store_true",
        help="Measure removed legacy view binding against bounded DuckLake startup",
    )
    parser.add_argument(
        "--startup-view-peak-mode",
        choices=("legacy", "ducklake"),
        help=argparse.SUPPRESS,
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
        if args.startup_view_peak_mode:
            print(
                json.dumps(
                    _measure_startup_view_peak_mode(
                        args.root.resolve(),
                        legacy_views=args.startup_view_peak_mode == "legacy",
                    )
                )
            )
            raise SystemExit(0)
        if args.startup_view_peak:
            print(json.dumps(measure_startup_view_peak(args.root.resolve())))
            raise SystemExit(0)
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
