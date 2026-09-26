"""One-shot advisory fact reader; its native query buffers die with it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from drover.config import ControlStoreConfig
from drover.server.advisory.snapshot_codec import encode_snapshot
from drover.server.advisory.worker import load_operational_snapshot
from drover.server.control_store import configure_control_store
from drover.server.db import copy_duckdb_store_with_wal


def run(payload: dict) -> dict:
    source = Path(payload["source"])
    snapshot = Path(payload["snapshot"])
    control_store = payload.get("control_store")
    if control_store is not None:
        configure_control_store(source, ControlStoreConfig(**control_store))
    copy_duckdb_store_with_wal(source, snapshot)
    result = load_operational_snapshot(
        snapshot,
        payload["analyzer_id"],
        payload["target_id"],
        payload["source_version"],
        control_source_path=source,
        include_control_wal=True,
        control_scratch_root=snapshot.parent,
    )
    return {"ok": True, "snapshot": encode_snapshot(result)}


def main() -> int:
    try:
        response = run(json.load(sys.stdin))
    except BaseException as exc:  # noqa: BLE001 - return a bounded failure to parent
        response = {"ok": False, "error": str(exc)}
    sys.stdout.write(json.dumps(response))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
