"""Parent-owned process boundary for operational advisory fact reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from drover.server.advisory.analyzers import AnalysisSnapshot
from drover.server.advisory.snapshot_codec import decode_snapshot
from drover.server.control_store import control_store_config, is_postgres_control_store
from drover.server.db import (
    control_plane_path,
    snapshot_scratch_root,
    supports_atomic_duckdb_clone,
)

SNAPSHOT_READER_BUDGET_SECONDS = 120.0


def supports_isolated_snapshot(source: Path) -> bool:
    """Require stable atomic clones for every DuckDB file the child reads."""
    source = source.resolve()
    try:
        if not supports_atomic_duckdb_clone(source):
            return False
        control_path = control_plane_path(source)
        if not is_postgres_control_store(source) and control_path.exists():
            return supports_atomic_duckdb_clone(control_path)
        return True
    except OSError:
        return False


def read_operational_snapshot_in_child(
    source: Path, analyzer_id: str, target_id: str, source_version: str
) -> AnalysisSnapshot:
    """Build facts on a clone so the parent's native heap cannot retain them."""
    source = source.resolve()
    config = control_store_config(source)
    request = {
        "source": str(source),
        "analyzer_id": analyzer_id,
        "target_id": target_id,
        "source_version": source_version,
        "control_store": asdict(config) if config is not None else None,
    }
    with tempfile.TemporaryDirectory(
        prefix="drover-advisory-", dir=snapshot_scratch_root(source)
    ) as directory:
        request["snapshot"] = str(Path(directory) / source.name)
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join(
            (source_root, environment.get("PYTHONPATH", ""))
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "drover.server.advisory.snapshot_reader"],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=SNAPSHOT_READER_BUDGET_SECONDS,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"advisory snapshot exceeded {SNAPSHOT_READER_BUDGET_SECONDS:g}s budget"
            ) from exc
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"advisory snapshot reader exited {completed.returncode} without a response"
        ) from exc
    if completed.returncode != 0 or not response.get("ok"):
        raise RuntimeError(f"advisory snapshot reader failed: {response.get('error')}")
    return decode_snapshot(response["snapshot"])
