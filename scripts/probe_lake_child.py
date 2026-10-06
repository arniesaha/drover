#!/usr/bin/env python3
"""Replay one captured lake query-child request under DuckDB setting variants.

Used by ``measure_lake_child_rss.py --variants``; not a production path. Runs
the real ``query_process._worker`` in this process with ``duckdb.connect``
config overridden by the variant, and reports the exact OS peak RSS
(``ru_maxrss``, no sampling) plus DuckDB's own peak buffer-manager memory for
the final (main) statement.

    probe_lake_child.py REQUEST.json '{"threads": 1, "memory_limit": "512MB"}'
"""

from __future__ import annotations

import json
import resource
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text())
    variant = json.loads(sys.argv[2])
    label = variant.pop("_label", None)
    if "_sql" in variant:
        # Localize cost: same CTEs/params, a different final statement.
        original = request["sql"]
        start = original.rindex("SELECT event_type")
        ctes = original[:start]
        projection = original[start + len("SELECT event_type,") :]
        projection = projection[: projection.index(" AS raw_data")]
        request["sql"] = (
            variant.pop("_sql")
            .replace("{ctes}", ctes)
            .replace("{projection}", projection)
        )
        request["params"] = request["params"][:1]
    profile = Path(tempfile.mkdtemp(prefix="drover-probe-")) / "profile.json"

    import duckdb

    real_connect = duckdb.connect

    def connect(*args, config=None, **kwargs):
        merged = {**(config or {}), **variant}
        con = real_connect(*args, config=merged, **kwargs)
        con.execute("PRAGMA enable_profiling='json'")
        con.execute(f"SET profiling_output='{profile}'")
        con.execute(
            "SET custom_profiling_settings='"
            + json.dumps(
                {
                    "SYSTEM_PEAK_BUFFER_MEMORY": "true",
                    "SYSTEM_PEAK_TEMP_DIR_SIZE": "true",
                    "LATENCY": "true",
                }
            )
            + "'"
        )
        return con

    duckdb.connect = connect
    from drover.server.lake import query_process
    from drover.server.lake.runtime import LakeError

    started = time.monotonic()
    try:
        reply = query_process._worker(request)
        outcome = {"rows": len(reply.get("rows", []))}
    except LakeError as exc:
        outcome = {"error": exc.code}
    except duckdb.OutOfMemoryException as exc:
        outcome = {"error": "duckdb_out_of_memory", "detail": str(exc)[:160]}
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024  # Linux reports KiB
    engine = {}
    try:
        engine = json.loads(profile.read_text())
    except (OSError, ValueError):
        pass
    print(
        json.dumps(
            {
                "label": label,
                "variant": variant,
                **outcome,
                "process_peak_rss_bytes": peak,
                "engine_peak_buffer_bytes": engine.get("system_peak_buffer_memory"),
                "engine_peak_temp_bytes": engine.get("system_peak_temp_dir_size"),
                "latency_seconds": engine.get("latency"),
                "wall_seconds": round(time.monotonic() - started, 3),
            }
        )
    )


if __name__ == "__main__":
    main()
