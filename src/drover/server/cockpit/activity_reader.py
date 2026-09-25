"""One-shot cockpit reader: its DuckDB buffers leave with this process."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from drover.config import ControlStoreConfig
from drover.server.cockpit.analytics import (
    AnalyticsCursorCodec,
    AnalyticsFilters,
    AnalyticsSnapshotChangedError,
    activity_analytics,
)
from drover.server.control_store import configure_control_store
from drover.server.db import (
    attached_control_plane_snapshot,
    copy_duckdb_store_with_wal,
    open_duckdb_connection,
)


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def run(payload: dict) -> dict:
    source = Path(payload["source"])
    snapshot = Path(payload["snapshot"])
    control_store = payload.get("control_store")
    if control_store is not None:
        configure_control_store(source, ControlStoreConfig(**control_store))
    # Clone the stable database/WAL pair on the source volume. This includes
    # recent writes without forcing a checkpoint on the live instance (#363).
    copy_duckdb_store_with_wal(source, snapshot)
    con = open_duckdb_connection(
        snapshot,
        read_only=True,
        role="snapshot",
        settings_overrides={"threads": "1"},
    )
    try:
        with attached_control_plane_snapshot(con, source, include_wal=True):
            result = activity_analytics(
                con,
                AnalyticsFilters(**payload["filters"]),
                cursor_codec=AnalyticsCursorCodec(
                    bytes.fromhex(payload["cursor_secret"])
                ),
            )
        return {"ok": True, "result": asdict(result)}
    finally:
        con.close()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        response = run(payload)
    except BaseException as exc:  # noqa: BLE001 - report failure to parent
        response = {
            "ok": False,
            "kind": (
                "snapshot_changed"
                if isinstance(exc, AnalyticsSnapshotChangedError)
                else "value" if isinstance(exc, ValueError) else "error"
            ),
            "error": str(exc),
        }
    sys.stdout.write(json.dumps(response, default=_json_default))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
