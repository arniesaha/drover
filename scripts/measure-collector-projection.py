"""Disposable S2 measurement: event-copy bytes in the legacy analytical store.

Run on synthetic acceptance parquet only:
  uv run python scripts/measure-collector-projection.py --parquet-root <fixture>
No production stores/configuration are opened. Temporary DuckDBs are removed.
"""

import argparse
import json
import tempfile
from pathlib import Path

import duckdb

from drover.server.memory_identity import ensure_memory_schema


def measure(root, events=1000):
    with duckdb.connect() as source:
        source.read_parquet(
            str(root / "agent_events/**/*.parquet"),
            union_by_name=True,
            hive_partitioning=True,
        ).create_view("fixture")
        counts = source.execute(
            "SELECT date,count(*) FROM fixture GROUP BY date ORDER BY date"
        ).fetchall()
        # Distinct IDs preserve the event table PK even in duplicate-heavy fixtures.
        average_payload = source.execute(
            "SELECT avg(octet_length(encode(content))),avg(octet_length(encode(raw_data))) FROM fixture"
        ).fetchone()
        sample = source.execute(
            "SELECT * FROM fixture QUALIFY row_number() OVER(PARTITION BY id ORDER BY timestamp)=1 ORDER BY hash(id) LIMIT ?",
            [events],
        ).to_arrow_table()
    with tempfile.TemporaryDirectory(prefix="drover-s2-projection-") as directory:
        measurements = {}
        for mode in ("old_event_copy", "parquet_only"):
            path = Path(directory) / f"{mode}.duckdb"
            with duckdb.connect(str(path)) as con:
                ensure_memory_schema(con)
                con.execute("CHECKPOINT")
                before = path.stat().st_size
                if mode == "old_event_copy":
                    con.register("sample", sample)
                    target = {
                        r[0]
                        for r in con.execute(
                            "DESCRIBE control_memory_events"
                        ).fetchall()
                    }
                    columns = [
                        name
                        for name in sample.column_names
                        if name in target and name != "source"
                    ]
                    con.execute(
                        "INSERT INTO control_memory_events BY NAME SELECT "
                        + ",".join('"' + name + '"' for name in columns)
                        + ", 'native' AS source FROM sample"
                    )
                con.execute("CHECKPOINT")
                measurements[mode] = dict(
                    rows=con.execute(
                        "SELECT count(*) FROM control_memory_events"
                    ).fetchone()[0],
                    bytes_added=path.stat().st_size - before,
                )
    total = sum(n for _, n in counts)
    latest_six = sum(n for _, n in counts[-6:])
    # A workload assumption, not a claim about production's observed daily rate.
    scaled_six = round(5_200_000 * latest_six / total)
    return dict(
        sample_events=sample.num_rows,
        fixture_parquet_bytes=sum(
            p.stat().st_size for p in (root / "agent_events").rglob("*.parquet")
        ),
        average_content_bytes=average_payload[0],
        average_raw_data_bytes=average_payload[1],
        fixture_total_events=total,
        fixture_date_range=[str(counts[0][0]), str(counts[-1][0])],
        latest_six_partition_events=latest_six,
        six_day_events_assuming_fixture_temporal_shape_at_5_2m=scaled_six,
        measurements=measurements,
        estimated_six_day_old_copy_bytes=round(
            measurements["old_event_copy"]["bytes_added"] / sample.num_rows * scaled_six
        ),
        estimated_six_day_parquet_only_copy_bytes=0,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet-root", type=Path, required=True)
    parser.add_argument("--events", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.events <= 50000:
        parser.error("--events must be between 1 and 50000")
    print(json.dumps(measure(args.parquet_root, args.events), indent=2))
