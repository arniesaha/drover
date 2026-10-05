"""Temporary legacy sink and S2 rollback replay, both fed by control events."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

from drover.server.control_outbox import (
    _publish_immutable_batch,
    export_projection,
    is_postgres_connection,
    payload_sha256,
)
from drover.server.db import control_plane_connection


def write_legacy_events(
    control, rows: list[dict], parquet_dir: Path, batch_id: str
) -> int:
    """Serialize sinks, dedupe touched partitions, and publish crash-safe files.

    Replay and the live exporter use the same advisory lock. A fixed batch/path
    plus durable exclusive publication makes restart safe even before an ack.
    """
    key = int.from_bytes(
        hashlib.sha256(str(Path(parquet_dir).resolve()).encode()).digest()[:8],
        "big",
        signed=True,
    )
    if is_postgres_connection(control):
        control.execute("SELECT pg_advisory_lock(?)", [key])
    try:
        for row in rows:
            payload = row.get("payload_json")
            expected = row.get("payload_sha256")
            if not isinstance(payload, str) or (
                expected and payload_sha256(payload) != expected
            ):
                raise RuntimeError(
                    f"legacy outbox payload hash mismatch: {row['event_id']}"
                )
        events = export_projection(control, rows)
        grouped = {}
        for row in events:
            grouped.setdefault((row["date"], row["agent_id"]), []).append(row)
        written = 0
        for (date, agent), partition in grouped.items():
            # Partition values are data, never unchecked filesystem components.
            if any(
                "/" in str(v) or "\\" in str(v) or str(v) in {".", ".."}
                for v in (date, agent)
            ):
                raise ValueError("invalid legacy partition value")
            directory = (
                Path(parquet_dir)
                / "agent_events"
                / f"date={date}"
                / f"agent_id={agent}"
            )
            paths = [str(p) for p in directory.glob("*.parquet")]
            with duckdb.connect() as con:
                existing = (
                    {
                        r[0]
                        for r in con.execute(
                            "SELECT dedup_key FROM read_parquet(?, union_by_name=true)",
                            [paths],
                        ).fetchall()
                    }
                    if paths
                    else set()
                )
            selected = []
            for row in partition:
                if row["dedup_key"] not in existing:
                    selected.append(
                        {
                            k: v
                            for k, v in row.items()
                            if k not in ("date", "agent_id", "source")
                        }
                    )
                    existing.add(row["dedup_key"])
            if selected:
                _publish_immutable_batch(
                    pa.Table.from_pylist(selected),
                    directory / f"outbox-{batch_id}.parquet",
                )
                written += len(selected)
        return written
    finally:
        if is_postgres_connection(control):
            control.execute("SELECT pg_advisory_unlock(?)", [key])


def replay_legacy(
    control_path: Path, parquet_dir: Path, *, since: float = 0, limit: int = 100
) -> int:
    """Replay retained deliveries, including pending and acknowledged rows.

    This offline rollback tool changes no outbox states or lake receipts.
    Archived legacy payloads and frozen lake inputs can supply cold envelopes.
    """
    import json

    from drover.server.control_outbox import _export_json, cut_batch_by_bytes

    after = ""
    written = 0
    stamp = datetime.fromtimestamp(since, timezone.utc)
    while True:
        with control_plane_connection(control_path) as control:
            cur = control.execute(
                """SELECT e.event_id, e.session_id, e.event_type,
                e.normalized_type, e.normalized_source, e.content_preview,
                e.created_at, e.seq, e.dedup_key, p.payload_json,
                COALESCE(p.payload_sha256, a.payload_sha256) AS payload_sha256,
                o.batch_id FROM control_outbox_events o JOIN harness_events e USING (event_id)
                LEFT JOIN harness_event_payloads p USING (event_id)
                LEFT JOIN harness_event_archives a USING (event_id)
                WHERE o.state <> 'rejected' AND o.committed_at >= ? AND e.event_id > ?
                ORDER BY e.event_id LIMIT ?""",
                [stamp, after, limit],
            )
            rows = [
                dict(zip([d[0] for d in cur.description], r, strict=True))
                for r in cur.fetchall()
            ]
            if not rows:
                return written
            for row in rows:
                if row["payload_json"] is None:
                    frozen = control.execute(
                        "SELECT input_json FROM lake_export_batches WHERE batch_id=?",
                        [row["batch_id"]],
                    ).fetchone()
                    if frozen:
                        raw = next(
                            r
                            for r in json.loads(frozen[0])["raw_rows"]
                            if r["event_id"] == row["event_id"]
                        )
                        row.update(raw)
                    else:
                        from drover.server.control_outbox import (
                            LocalVerifiedArchiveResolver,
                            published_batches,
                        )

                        batches = {b.batch_id for b in published_batches(control)}
                        row["payload_json"] = LocalVerifiedArchiveResolver(
                            parquet_dir, lambda: batches
                        ).resolve(
                            event_id=row["event_id"],
                            batch_id=row["batch_id"],
                            payload_sha256=row["payload_sha256"] or "",
                        )
                    if row["payload_json"] is None:
                        raise RuntimeError(
                            f"replay payload unavailable: {row['event_id']}"
                        )
            ids = cut_batch_by_bytes(
                [
                    (
                        r["event_id"],
                        len(
                            _export_json(
                                [r, export_projection(control, [r])[0], r["event_id"]]
                            ).encode()
                        )
                        + 32,
                    )
                    for r in rows
                ]
            )
            rows = rows[: len(ids)]
            batch_id = "replay-" + hashlib.sha256("\n".join(ids).encode()).hexdigest()
            written += write_legacy_events(control, rows, parquet_dir, batch_id)
            after = ids[-1]
