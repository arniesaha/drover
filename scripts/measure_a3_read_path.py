#!/usr/bin/env python3
"""Time the A3 summarize read path, child by child, against a v2 lake. Read-only.

Drives the production ``SummarizerWorker._summarize_session`` read path
through the real query-child facade (``HistoryConnection`` -> ``query`` ->
``run_disposable``) and stops at the model call: no ledger, no control-plane
reads or writes. Every analytical child is timed; a child that trips its
deadline is replayed once in-process without one, to report its true cost.

    ~/.drover/v2-reader-run.sh uv run python scripts/measure_a3_read_path.py \\
        --data-root "/Volumes/M2 1/drover-lakes/v2" [--session ID]

Needs ``DROVER_LAKE_V2_READER_DSN``, ``DROVER_LAKE_EXTENSION_DIR`` and
``DROVER_LAKE_ENGINE_SHA256`` (the wrapper exports exactly those). With no
``--session`` it picks the largest session, exactly as the gate's A3 does.
Identities are empty: the hub's PostgreSQL identity snapshot is not read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace


class _Done(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--dsn-env", default="DROVER_LAKE_V2_READER_DSN")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Also check the bounded reads against unbounded ones (in-process).",
    )
    args = parser.parse_args()

    from drover.config import AnalyticsConfig
    from drover.server.lake import query_process
    from drover.server.lake.runtime import LakeError
    from drover.server.lake.serving import HistoryConnection
    from drover.server.summarizer import worker as worker_module

    proof = args.data_root / "verification" / "serving-proof.json"
    config = AnalyticsConfig(
        backend="ducklake",
        catalog_dsn_env=args.dsn_env,
        data_root=str(args.data_root),
        extension_dir=os.environ["DROVER_LAKE_EXTENSION_DIR"],
        engine_sha256=os.environ["DROVER_LAKE_ENGINE_SHA256"],
        verification_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),
    )
    children: list[dict] = []
    real_query = query_process.query

    def timed_query(
        spec, sql, params=None, *, limits=query_process.QueryLimits(), serving=None
    ):
        label = " ".join(sql.split())[:90]
        started = time.monotonic()
        try:
            result = real_query(spec, sql, params, limits=limits, serving=serving)
        except LakeError as exc:
            record = {"sql": label, "error": exc.code}
            record["seconds"] = round(time.monotonic() - started, 3)
            if exc.code == "analytics_deadline_exceeded":
                # True cost without the deadline: the same request, in-process.
                request = {
                    "spec": {
                        "catalog_dsn_env": spec.catalog_dsn_env,
                        "data_root": str(spec.data_root),
                        "extension_dir": str(spec.extension_dir),
                        "engine_sha256": spec.engine_sha256,
                    },
                    "sql": sql,
                    "params": params or [],
                    "limits": {**limits.__dict__},
                    "serving": serving,
                }
                replay = time.monotonic()
                try:
                    query_process._worker(request)
                    record["undeadlined_seconds"] = round(time.monotonic() - replay, 3)
                except LakeError as inner:
                    record["undeadlined_error"] = inner.code
                    record["undeadlined_seconds"] = round(time.monotonic() - replay, 3)
            children.append(record)
            print(json.dumps({"child": record}), flush=True)
            raise
        record = {
            "sql": label,
            "seconds": round(time.monotonic() - started, 3),
            "rows": len(result.get("rows", [])),
            "peak_rss_mb": round(result["peak_rss_bytes"] / 2**20, 1),
        }
        children.append(record)
        print(json.dumps({"child": record}), flush=True)
        return result

    import drover.server.lake.serving as serving_module

    serving_module.query = timed_query
    con = HistoryConnection(config)
    session = args.session
    if not session:
        # The gate's A3 picker (gate.GateRun.a3).
        row = con.execute(
            "SELECT session_id, count(*) AS n FROM lake.agent_events "
            "WHERE session_id NOT LIKE 'drover-gate-%' "
            "GROUP BY session_id ORDER BY n DESC LIMIT 1"
        ).fetchone()
        session = row[0]
        print(json.dumps({"largest_session": session, "events": row[1]}), flush=True)

    worker_module._open_summarizer_db = lambda _path: HistoryConnection(config)
    captured = {}
    real_window, real_facts = (
        worker_module._read_prompt_window,
        worker_module._read_tool_facts,
    )

    def capture_window(*a, **kw):
        captured["window"] = real_window(*a, **kw)
        return captured["window"]

    def capture_facts(*a, **kw):
        captured["facts"] = real_facts(*a, **kw)
        return captured["facts"]

    worker_module._read_prompt_window = capture_window
    worker_module._read_tool_facts = capture_facts

    def stop_at_model(prompt, **_kw):
        captured["prompt_bytes"] = len(prompt.encode())
        raise _Done()

    worker = worker_module.SummarizerWorker(
        duckdb_path=Path("/nonexistent"), _llm_call=stop_at_model
    )
    started = time.monotonic()
    outcome = "read_path_ok"
    try:
        worker._summarize_session(None, SimpleNamespace(subject_key=session))
    except _Done:
        pass
    except Exception as exc:  # noqa: BLE001
        outcome = f"{type(exc).__name__}: {exc}"
    total = time.monotonic() - started
    read = [c for c in children if "largest_session" not in c]
    if args.compare and "window" in captured:
        # The unbounded reads, in-process and without a deadline: the prompt
        # window must match exactly; tool facts over the newest N events.
        from drover.server.summarizer.derive import select_substantive_window

        def unbounded(spec, sql, params=None, *, limits=None, serving=None):
            reply = query_process._worker(
                {
                    "spec": {
                        "catalog_dsn_env": spec.catalog_dsn_env,
                        "data_root": str(spec.data_root),
                        "extension_dir": str(spec.extension_dir),
                        "engine_sha256": spec.engine_sha256,
                    },
                    "sql": sql,
                    "params": params or [],
                    "limits": {
                        **query_process.QueryLimits(
                            rows=10000, bytes=4 * 2**20
                        ).__dict__
                    },
                    "serving": serving,
                }
            )
            # A child's reply crosses as JSON; make the in-process one alike.
            return json.loads(json.dumps(reply, default=str))

        serving_module.query = unbounded
        con = HistoryConnection(config)
        whole = select_substantive_window(
            con, worker_module._session_agent_events_ctes(), session
        )
        key = [(e["id"], str(e["timestamp"]), e["content"]) for e in whole]
        got = [(e["id"], str(e["timestamp"]), e["content"]) for e in captured["window"]]
        unbounded_facts = worker_module._read_tool_facts(con, session, None)
        print(
            json.dumps(
                {
                    "window_matches_unbounded": key == got,
                    "window_len": [len(key), len(got)],
                    "tool_facts_match_unbounded_newest_n": unbounded_facts
                    == captured["facts"],
                    "files": len(captured["facts"][0]),
                    "tools": captured["facts"][1],
                },
                default=str,
            )[:600]
        )
    print(
        json.dumps(
            {
                "session_id": session,
                "outcome": outcome,
                "read_path_seconds": round(total, 3),
                "children": len(read),
                "slowest_child_seconds": max((c["seconds"] for c in read), default=0),
                "prompt_bytes": captured.get("prompt_bytes"),
            }
        )
    )
    return 0 if outcome == "read_path_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
