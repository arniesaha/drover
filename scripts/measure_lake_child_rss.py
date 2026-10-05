#!/usr/bin/env python3
"""Measure analytical query-child peak RSS for the A3 summarize workload.

Scratch only: a throwaway initdb cluster, the committed production-shaped
control fixture and the deterministic 50k-event acceptance lake (its 40,000-
event session is the A3 subject). Each iteration summarizes that session with
a fresh source version through the production summarizer and query-child path,
recording every child's sampled peak RSS against the ``QueryLimits`` cap.

    uv run python scripts/measure_lake_child_rss.py \
        --extensions ~/.cache/drover-lake-ext --iterations 10

Prints one JSON line per iteration and a summary line; exits non-zero if any
child exceeded the cap or any summary job failed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests" / "acceptance"))


def run_variants(capture: Path, variants: list[dict]) -> None:
    """Replay the first window query and the first two pages per variant, 3x."""
    import subprocess

    probe = REPO / "scripts" / "probe_lake_child.py"
    requests = sorted(capture.glob("child-*.json"))[:3]
    for variant in variants:
        for request in requests:
            for repeat in range(3):
                result = subprocess.run(
                    [sys.executable, str(probe), str(request), json.dumps(variant)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                line = (result.stdout.strip().splitlines() or ["{}"])[-1]
                record = json.loads(line) if line.startswith("{") else {}
                record.update(child=request.stem, repeat=repeat + 1)
                if result.returncode:
                    record["stderr"] = result.stderr.strip()[-300:]
                print(json.dumps({"probe": record}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extensions", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/drover-gate-cache"))
    parser.add_argument(
        "--capture",
        type=Path,
        help="Also save each child's request JSON here (for offline replay).",
    )
    parser.add_argument(
        "--variants",
        type=Path,
        help="JSON list of DuckDB config overrides to replay captured children "
        "under (requires --capture).",
    )
    args = parser.parse_args()

    import psycopg
    from conftest import restore_prod_control_schema
    from lake_gate_rehearsal import _bindir, scratch_cluster
    from lake_generator import BIG_SESSION_ID, get_cached_lake
    from psycopg.conninfo import make_conninfo
    from served_lake import build_served_lake, drop_served_lake

    from drover.config import ControlStoreConfig
    from drover.server.control_store import configure_control_store
    from drover.server.lake import query_process, read_models, serving
    from drover.server.lake.serving import configure_analytics
    from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
    from drover.server.summarizer.jobs import enqueue_summary_generation
    from drover.server.summarizer.worker import SummarizerWorker

    cap = query_process.QueryLimits().rss_bytes
    legacy = get_cached_lake(scale="small", seed=42, cache_dir=args.cache)
    work = Path(tempfile.mkdtemp(prefix="drover-rss-probe-"))
    failures = 0
    peaks: list[int] = []
    with scratch_cluster(_bindir(), "rss") as admin:
        with psycopg.connect(admin, autocommit=True) as con:
            con.execute("CREATE DATABASE drover_rss_control")
        control_dsn = make_conninfo(admin, dbname="drover_rss_control")
        restore_prod_control_schema(control_dsn)
        os.environ["DROVER_RSS_PROBE_CONTROL_DSN"] = control_dsn
        control_path = work / "control.duckdb"
        configure_control_store(
            control_path,
            ControlStoreConfig(
                backend="postgres",
                dsn_env="DROVER_RSS_PROBE_CONTROL_DSN",
                pool_min_size=1,
                pool_max_size=4,
                acquire_timeout_seconds=5.0,
                statement_timeout_seconds=30.0,
            ),
        )
        served = build_served_lake(
            parquet_root=legacy,
            postgres_dsn=admin,
            extension_dir=args.extensions.expanduser(),
            workdir=work,
            label="rss",
        )
        configure_analytics(control_path, served.config)
        os.environ["DROVER_LAKE_EXTENSION_DIR"] = str(served.spec.extension_dir)
        os.environ["DROVER_LAKE_ENGINE_SHA256"] = served.spec.engine_sha256

        children: list[dict] = []
        real_query = query_process.query

        def observed(*a, **kw):
            entry = {"sql": " ".join(a[1].split())[:60]}
            children.append(entry)
            if args.capture:
                args.capture.mkdir(parents=True, exist_ok=True)
                spec = a[0]
                (args.capture / f"child-{len(children):02d}.json").write_text(
                    json.dumps(
                        {
                            "spec": {
                                "catalog_dsn_env": spec.catalog_dsn_env,
                                "data_root": str(spec.data_root),
                                "extension_dir": str(spec.extension_dir),
                                "engine_sha256": spec.engine_sha256,
                            },
                            "sql": a[1],
                            "params": a[2] if len(a) > 2 else kw.get("params"),
                            "limits": {"rows": 1000, "bytes": 1024 * 1024},
                            "serving": kw.get("serving"),
                        },
                        default=str,
                    )
                )
            try:
                result = real_query(*a, **kw)
                entry["peak_rss_bytes"] = result["peak_rss_bytes"]
                return result
            except Exception as exc:
                entry["error"] = getattr(exc, "code", type(exc).__name__)
                raise

        for module in (query_process, serving, read_models):
            module.query = observed
        try:
            for iteration in range(1, args.iterations + 1):
                children.clear()
                version = f"rss-probe-{uuid4().hex[:8]}"
                enqueue_summary_generation(control_path, BIG_SESSION_ID, version)
                worker = SummarizerWorker(
                    duckdb_path=control_path,
                    _llm_call=lambda *a, **kw: {
                        "summary_md": "probe",
                        "next_steps_md": "",
                        "open_questions": [],
                    },
                )
                started = time.monotonic()
                worker.drain_once()
                job = JobLedger(control_path).latest(SUMMARIZE_SESSION, BIG_SESSION_ID)
                measured = [
                    c["peak_rss_bytes"] for c in children if "peak_rss_bytes" in c
                ]
                peak = max(measured, default=0)
                errors = [c["error"] for c in children if "error" in c]
                ok = job.status == "succeeded" and not errors and peak <= cap
                failures += not ok
                if measured:
                    peaks.append(peak)
                print(
                    json.dumps(
                        {
                            "iteration": iteration,
                            "job": job.status,
                            "last_error": job.last_error,
                            "children": len(children),
                            "peak_rss_bytes": peak,
                            "peak_mib": round(peak / 1024**2, 1),
                            "median_child_mib": (
                                round(statistics.median(measured) / 1024**2, 1)
                                if measured
                                else None
                            ),
                            "child_errors": errors,
                            "cap_bytes": cap,
                            "seconds": round(time.monotonic() - started, 2),
                        }
                    ),
                    flush=True,
                )
            if args.capture and args.variants:
                run_variants(args.capture, json.loads(args.variants.read_text()))
        finally:
            from drover.server.control_store import close_control_store

            close_control_store(control_path)
            drop_served_lake(admin, served)
    print(
        json.dumps(
            {
                "iterations": args.iterations,
                "failures": failures,
                "cap_bytes": cap,
                "max_peak_mib": round(max(peaks, default=0) / 1024**2, 1),
                "min_peak_mib": round(min(peaks, default=0) / 1024**2, 1),
                "max_peak_fraction_of_cap": round(max(peaks, default=0) / cap, 3),
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
