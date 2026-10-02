"""Busy-hub synthetic attribution probe (service calls, not a production RSS claim).

uv sync --extra dev; python scripts/measure_hub_memory.py --prepare /tmp/hub-probe
DROVER_DUCKDB_MEMORY_DIAGNOSTICS=1 DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT=6GB \
  python scripts/measure_hub_memory.py /tmp/hub-probe
Uses the optional span workload; each measurement must use a fresh process.
Use this script with compatible current sources, not pre-optional-span revisions.
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
from pathlib import Path

import duckdb
import psutil
import pyarrow as pa

from drover.server.advisory.service import InsightFilters, InsightsService
from drover.server.cockpit.analytics import AnalyticsFilters
from drover.server.cockpit.service import CockpitService
from drover.server.db import open_duckdb_connection, sql_path_literal


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


def measure(root, live_reader=False):
    logging.basicConfig(level=logging.INFO)
    tracemalloc.start(10)
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
        samples.append(
            dict(
                phase=phase,
                rss=process.memory_info().rss,
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
            for index in range(3):
                start = time.monotonic()
                result = service.overview(AnalyticsFilters(days=7 + index))
                assert result["activity"]["status"] == "ok", result["activity"]
                sample(f"cockpit_{index}", live)
                samples[-1]["seconds"] = time.monotonic() - start
                insights.list_insights(InsightFilters())
                sample(f"insights_{index}", live)
            sample("idle", live)
        sample("closed")
    finally:
        stop.set()
        thread.join()
    print(
        json.dumps(
            dict(
                synthetic=True,
                workload=json.loads((root / "workload.json").read_text()),
                duckdb_version=duckdb.__version__,
                isolated_reader=service._isolated_reader_supported,
                spans_enabled=True,
                samples=samples,
                python_top=[
                    dict(bytes=x.size, count=x.count)
                    for x in tracemalloc.take_snapshot().statistics("lineno")[:10]
                ],
            )
        )
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
    args = parser.parse_args()
    if min(args.sessions, args.spans, args.files) < 1:
        parser.error("sessions, spans and files must be positive")
    if args.prepare:
        prepare(args.root.resolve(), args.sessions, args.spans, args.files)
    else:
        measure(args.root.resolve(), args.live_reader)
