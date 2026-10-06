#!/usr/bin/env python3
"""Where does GET /cockpit/overview?days=30 spend its time? Read-only.

Replays the lake side of ``read_models.read_model(path, "cockpit")`` against a
v2 lake. Before 2026-10-06 that was three children: a pre ``_capture`` token
child, the cockpit read-model child and a post ``_capture`` token child; it is
now the read-model child alone (``--no-token-children``; the default replays
the old three-child shape for comparison). Each runs through the real ``run_disposable``
(admission flock, spawn, RSS polling). The cockpit child is this script in
``--worker`` mode: it runs the real ``query_process._worker`` with timers
patched around attach / reader fence / check_proof / views / every statement
of ``activity_analytics``. PostgreSQL control-plane tables are stubbed empty
(``_control_snapshot``): the prod control store is not reachable here.

    ~/.drover/v2-reader-run.sh uv run python scripts/probe_cockpit_overview.py \\
        --data-root "/Volumes/M2 1/drover-lakes/v2" [--iterations 5] \\
        [--identities N] [--variant base|lean_day_view] [--no-token-children]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_T0 = time.monotonic()

# A cheaper per-day relation for the cockpit's two live partitions. Same row
# set as ``agent_events WHERE date=day`` (source/control rule + identity
# anti-join), but the anti-join is an equi-join on a materialized id set and
# the JSON is parsed once per row instead of up to three times.
LEAN_DAY_MACRO = """CREATE OR REPLACE TEMP MACRO agent_events_for_date(day) AS TABLE
  WITH ids AS (
    SELECT harness_session_id AS sid FROM memory_session_identity
      WHERE native_session_id IS DISTINCT FROM harness_session_id
    UNION
    SELECT native_session_id FROM memory_session_identity
      WHERE native_session_id IS DISTINCT FROM harness_session_id
  ),
  raw AS (
    SELECT e.*, CASE WHEN json_valid(e.raw_data) THEN e.raw_data::JSON END AS _j
    FROM lake.agent_events e WHERE e.date = day
  )
  SELECT * EXCLUDE(timestamp, repo_owner, repo_name, _j),
    TRY_CAST(timestamp AS TIMESTAMPTZ) AS timestamp,
    COALESCE(repo_owner, json_extract_string(_j, '$._repo_owner')) AS repo_owner,
    COALESCE(repo_name, json_extract_string(_j, '$._repo_name')) AS repo_name,
    CASE WHEN dedup_key_source='outbox' OR json_extract_string(_j,'$.source')='control'
      THEN 'control' ELSE 'native' END AS source
  FROM raw
  WHERE dedup_key_source='outbox' OR json_extract_string(_j,'$.source')='control'
     OR session_id IS NULL OR session_id NOT IN (SELECT sid FROM ids WHERE sid IS NOT NULL)
"""


# --------------------------------------------------------------------------- worker


def worker(request_path: str, timings_path: str) -> None:
    timings: dict = {"phases": {}, "statements": []}
    phases = timings["phases"]

    def add(name, seconds):
        phases[name] = round(phases.get(name, 0.0) + seconds, 4)

    t = time.monotonic()
    from contextlib import contextmanager

    from drover.server.lake import (
        fence,
        query_process,
        read_models,
        runtime,
        serving_proof,
    )

    add("imports", time.monotonic() - t)
    probe = json.loads(Path(request_path).read_text()).get("probe", {})

    real_verify = runtime.verify_runtime

    def verify(spec):
        s = time.monotonic()
        try:
            return real_verify(spec)
        finally:
            add("verify_runtime(sha256 engine+ext)", time.monotonic() - s)

    runtime.verify_runtime = verify

    real_lake_connection = query_process.lake_connection

    class FastIdentities:
        """Prototype: one set-based INSERT instead of executemany row by row."""

        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a):
            s = time.monotonic()
            try:
                return self._con.execute(sql, *a)
            finally:
                if "proof_end" in marks and "model_start" not in marks:
                    timings.setdefault("setup_statements", []).append(
                        [" ".join(sql.split())[:70], round(time.monotonic() - s, 4)]
                    )

        def executemany(self, sql, rows):
            s = time.monotonic()
            try:
                return self._executemany(sql, rows)
            finally:
                timings.setdefault("setup_statements", []).append(
                    ["executemany " + sql[:40], round(time.monotonic() - s, 4)]
                )

        def _executemany(self, sql, rows):
            if sql.startswith("INSERT INTO memory_session_identity") and probe.get(
                "fast_identities"
            ):
                # One VARCHAR parameter: Python->DuckDB value conversion is
                # the cost of executemany (and of list parameters), not the insert.
                self._con.execute(
                    "INSERT INTO memory_session_identity SELECT r[1], r[2], r[3], r[4]::TIMESTAMPTZ "
                    "FROM (SELECT unnest(CAST(?::JSON AS VARCHAR[][])) AS r)",
                    [json.dumps(rows)],
                )
                return self._con
            return self._con.executemany(sql, rows)

        def __getattr__(self, name):
            return getattr(self._con, name)

    @contextmanager
    def lake_connection(spec, **kw):
        s = time.monotonic()
        with real_lake_connection(spec, **kw) as con:
            add("connect+LOAD+ATTACH(total incl verify)", time.monotonic() - s)
            yield FastIdentities(con)

    query_process.lake_connection = lake_connection

    real_fence = fence.reader_fence

    @contextmanager
    def reader_fence(dsn):
        s = time.monotonic()
        with real_fence(dsn):
            add("reader_fence(pg advisory)", time.monotonic() - s)
            yield

    fence.reader_fence = reader_fence

    for name in ("catalog_identity", "_check_referenced_files"):
        real = getattr(serving_proof, name)

        def wrapped(*a, _real=real, _name=name, **kw):
            s = time.monotonic()
            try:
                return _real(*a, **kw)
            finally:
                add(f"check_proof.{_name}", time.monotonic() - s)

        setattr(serving_proof, name, wrapped)

    real_check = serving_proof.check_proof
    marks = {}

    def check_proof(*a, **kw):
        s = time.monotonic()
        try:
            return real_check(*a, **kw)
        finally:
            marks["proof_end"] = time.monotonic()
            add("check_proof(total)", marks["proof_end"] - s)

    serving_proof.check_proof = check_proof

    def control_snapshot(con, request):
        from drover.server.db import _POSTGRES_ANALYTICS_SNAPSHOT_TABLES

        s = time.monotonic()
        add("identity+views (after proof)", s - marks["proof_end"])
        timings["identity_checksum"] = [
            str(v)
            for v in con.execute(
                "SELECT count(*), sum(hash(harness_session_id, native_session_id, summary_session_id, started_at))"
                " FROM memory_session_identity"
            ).fetchone()
        ]
        s = time.monotonic()
        for table, columns in _POSTGRES_ANALYTICS_SNAPSHOT_TABLES.items():
            definitions = ",".join(f'"{n}" {k}' for n, k in columns)
            con.execute(f"CREATE TEMP TABLE {table} ({definitions})")
        add("control_snapshot(stubbed, empty)", time.monotonic() - s)
        if probe.get("simulate_daily"):
            # The prod lake's activity_daily is empty (selective import does
            # not fill it); emulate a fully populated rollup for the window in
            # a temp table so the 30-day plan is the steady-state one.
            s = time.monotonic()
            import inspect
            import re as _re

            from drover.server.lake import activity_daily as ad

            body = inspect.getsource(ad.refresh_activity_daily)
            select = body[
                body.index("SELECT\n          date,") : body.index(
                    "GROUP BY 1, 2, 3, 4, 5, 6"
                )
                + 25
            ]
            select = _re.sub(
                r"WHERE date IN \(\{placeholders\}\)", "WHERE date >= ?", select
            )
            con.execute(
                f"CREATE TEMP TABLE activity_daily AS {select}",
                [probe["simulate_daily"]],
            )
            add("SIM build temp activity_daily (NOT a prod cost)", time.monotonic() - s)
        if probe.get("variant") == "lean_day_view":
            con.execute(LEAN_DAY_MACRO)
        marks["model_start"] = time.monotonic()
        unavailable = {
            "freshness": "unavailable",
            "observed_at": None,
            "reason": "probe",
        }
        return (
            {
                "native_publication": dict(unavailable),
                "native_usage": dict(unavailable),
            },
            None,
            None,
            None,
        )

    read_models._control_snapshot = control_snapshot

    class Proxy:
        def __init__(self, con):
            self._con = con
            self._last = None

        def execute(self, sql, params=None):
            s = time.monotonic()
            if (
                probe.get("variant") == "lean_day_view"
                and "CREATE TEMP MACRO agent_events_for_date" in sql
            ):
                sql = "SELECT 1"  # keep the lean macro installed in control_snapshot
            if probe.get("simulate_daily"):
                sql = sql.replace(
                    "FROM lake.activity_daily", "FROM temp.activity_daily"
                )
                sql = sql.replace("FROM activity_daily d", "FROM temp.activity_daily d")
            (
                self._con.execute(sql, params)
                if params is not None
                else self._con.execute(sql)
            )
            self._last = {"sql": " ".join(sql.split())[:110], "s": time.monotonic() - s}
            timings["statements"].append(self._last)
            return self

        def _fetch(self, name):
            s = time.monotonic()
            out = getattr(self._con, name)()
            if self._last is not None:
                self._last["s"] += time.monotonic() - s
            return out

        def fetchone(self):
            return self._fetch("fetchone")

        def fetchall(self):
            return self._fetch("fetchall")

        def __getattr__(self, name):
            return getattr(self._con, name)

    real_run_model = read_models.run_model

    def run_model(con, request, limits):
        s = time.monotonic()
        try:
            return real_run_model(Proxy(con), request, limits)
        finally:
            add("run_model(total)", time.monotonic() - s)
            add(
                "run_model(activity sql, excl stub)",
                time.monotonic() - marks["model_start"],
            )

    read_models.run_model = run_model

    t = time.monotonic()
    try:
        reply = query_process._worker(json.loads(Path(request_path).read_text()))
    except Exception as exc:  # noqa: BLE001
        reply = {
            "error": getattr(exc, "code", type(exc).__name__ + ": " + str(exc)[:200])
        }
    add("_worker(total)", time.monotonic() - t)
    for row in timings["statements"]:
        row["s"] = round(row["s"], 4)
    timings["child_wall_from_interpreter_start"] = round(time.monotonic() - _T0, 4)
    if "payload" in reply:
        data = reply["payload"]
        timings["result"] = {
            "sessions": data["totals"]["session_count"],
            "projects": len(data["projects"]),
            "snapshot_version": data["snapshot_version"][:16],
        }
    Path(timings_path).write_text(json.dumps(timings))
    print(json.dumps(reply, default=str))


# --------------------------------------------------------------------------- parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root", type=Path, default=Path("/Volumes/M2 1/drover-lakes/v2")
    )
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--identities", type=int, default=0)
    parser.add_argument("--variant", default="base", choices=["base", "lean_day_view"])
    parser.add_argument("--no-token-children", action="store_true")
    parser.add_argument(
        "--select1", type=int, default=0, help="also time N bare SELECT 1 children"
    )
    parser.add_argument(
        "--sql", help="run one ad-hoc read-only SELECT through a serving child"
    )
    parser.add_argument(
        "--simulate-daily",
        action="store_true",
        help="emulate a populated activity_daily (temp table, untimed in prod terms)",
    )
    parser.add_argument(
        "--no-deadline",
        action="store_true",
        help="run the cockpit child without run_disposable (no 5s deadline/admission)",
    )
    parser.add_argument(
        "--fast-identities",
        action="store_true",
        help="prototype: set-based identity INSERT in the cockpit child",
    )
    args = parser.parse_args()
    from datetime import date, timedelta

    simulate_from = (
        (date.today() - timedelta(days=args.days + 2)).isoformat()
        if args.simulate_daily
        else None
    )

    from drover.server.lake import query_process
    from drover.server.lake.runtime import LakeError, LakeSpec

    root = args.data_root
    digest = hashlib.sha256(
        (root / "verification/serving-proof.json").read_bytes()
    ).hexdigest()
    spec = LakeSpec(
        "DROVER_LAKE_V2_READER_DSN",
        root,
        Path(os.environ["DROVER_LAKE_EXTENSION_DIR"]),
        os.environ["DROVER_LAKE_ENGINE_SHA256"],
    )
    identities = [
        [f"probe-h-{i:06d}", f"probe-n-{i:06d}", None, "2026-01-01 00:00:00+00"]
        for i in range(args.identities)
    ]

    def token_child():
        s = time.monotonic()
        serving = {
            "verification_sha256": digest,
            "identities": identities,
            "task_projection": "token",
        }
        if args.fast_identities:  # same child, through the patched worker
            error = cockpit_child(serving)[2]
        else:
            try:
                query_process.query(spec, "SELECT 1", serving=serving)
                error = None
            except LakeError as exc:
                error = exc.code
        if error:
            print(
                json.dumps(
                    {"token_child_error": error, "s": round(time.monotonic() - s, 3)}
                ),
                flush=True,
            )
        return time.monotonic() - s

    def cockpit_child(serving=None):
        serving = serving or {
            "verification_sha256": digest,
            "identities": identities,
            "operation": "cockpit",
            "binding": {"epoch": None, "identities": "probe"},
            "options": {"filters": {"days": args.days}, "cursor_secret": "00" * 32},
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="drover-probe-cockpit-") as scratch:
            request = Path(scratch) / "request.json"
            timings = Path(scratch) / "timings.json"
            request.write_text(
                json.dumps(
                    {
                        "spec": {
                            "catalog_dsn_env": spec.catalog_dsn_env,
                            "data_root": str(root),
                            "extension_dir": str(spec.extension_dir),
                            "engine_sha256": spec.engine_sha256,
                        },
                        "sql": "SELECT 1",
                        "params": [],
                        "limits": query_process.asdict(query_process.QueryLimits()),
                        "serving": serving,
                        "spill_directory": str(Path(scratch) / "spill"),
                        "probe": {
                            "variant": args.variant,
                            "simulate_daily": simulate_from,
                            "fast_identities": args.fast_identities,
                        },
                    }
                )
            )
            error = None
            if args.no_deadline:
                import subprocess

                done = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(request),
                        str(timings),
                    ],
                    cwd=scratch,
                    capture_output=True,
                    text=True,
                )
                reply = json.loads(done.stdout.strip().splitlines()[-1])
                inner = json.loads(timings.read_text()) if timings.exists() else {}
                return time.monotonic() - started, None, reply.get("error"), inner
            try:
                reply = query_process.run_disposable(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(request),
                        str(timings),
                    ],
                    admission_path=Path(tempfile.gettempdir())
                    / f"drover-lake-{os.getuid()}"
                    / (
                        hashlib.sha256(str(root.resolve()).encode()).hexdigest()
                        + ".lock"
                    ),
                    limits=query_process.QueryLimits(),
                    cwd=Path(scratch),
                    started_at=started,
                )
                rss = round(reply["peak_rss_bytes"] / 2**20)
            except LakeError as exc:
                error, rss = exc.code, None
            inner = json.loads(timings.read_text()) if timings.exists() else {}
        return time.monotonic() - started, rss, error, inner

    if args.sql:
        s = time.monotonic()
        reply = query_process.query(
            spec,
            args.sql,
            serving={"verification_sha256": digest, "identities": identities},
            limits=query_process.QueryLimits(rows=10000, bytes=4 * 1024**2),
        )
        print(
            json.dumps(
                {
                    "s": round(time.monotonic() - s, 3),
                    "columns": reply["columns"],
                    "rows": reply["rows"][:60],
                },
                default=str,
            )
        )
        return 0
    for _ in range(args.select1):
        s = time.monotonic()
        query_process.query(
            spec,
            "SELECT 1",
            serving={"verification_sha256": digest, "identities": identities},
        )
        print(
            json.dumps({"select1_child_s": round(time.monotonic() - s, 3)}), flush=True
        )

    totals = []
    for i in range(args.iterations):
        s = time.monotonic()
        pre = 0.0 if args.no_token_children else token_child()
        wall, rss, error, inner = cockpit_child()
        post = 0.0 if args.no_token_children else token_child()
        # The simulated rollup build is not a production cost: take it out.
        total = (
            time.monotonic()
            - s
            - (inner.get("phases") or {}).get(
                "SIM build temp activity_daily (NOT a prod cost)", 0.0
            )
        )
        totals.append(total)
        stmts = sorted(inner.get("statements", []), key=lambda r: -r["s"])[:6]
        print(
            json.dumps(
                {
                    "iter": i,
                    "variant": args.variant,
                    "identities": args.identities,
                    "total_lake_s": round(total, 3),
                    "pre_capture_child_s": round(pre, 3),
                    "cockpit_child_s": round(wall, 3),
                    "post_capture_child_s": round(post, 3),
                    "cockpit_rss_mb": rss,
                    "error": error,
                    "child_interp_wall_s": inner.get(
                        "child_wall_from_interpreter_start"
                    ),
                    "phases": inner.get("phases"),
                    "top_statements": stmts[:2],
                    "result": inner.get("result"),
                    "identity_checksum": inner.get("identity_checksum"),
                    "setup_statements": inner.get("setup_statements"),
                }
            ),
            flush=True,
        )
    totals.sort()
    print(
        json.dumps(
            {
                "summary": {
                    "variant": args.variant,
                    "identities": args.identities,
                    "fast_identities": args.fast_identities,
                    "token_children": not args.no_token_children,
                    "simulate_daily": args.simulate_daily,
                    "median": round(totals[len(totals) // 2], 3),
                    "min": round(totals[0], 3),
                    "max(p95 of 5)": round(totals[-1], 3),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
