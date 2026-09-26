"""The short-lived advisory reader sees live WAL rows without owning live DB memory."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.advisory import snapshot_process
from drover.server.advisory.snapshot_codec import decode_snapshot
from drover.server.db import snapshot_scratch_root, supports_atomic_duckdb_clone


def test_reader_process_clones_live_wal_and_returns_typed_facts(tmp_path: Path) -> None:
    source = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=source)
    if not supports_atomic_duckdb_clone(source):
        pytest.skip("requires an atomic clone on the source volume")

    now = datetime.now(timezone.utc)
    # Keep the writer open so the new row is in the live WAL, not the DB file.
    with duckdb.connect(str(source)) as writer:
        writer.execute(
            """
            INSERT INTO provider_connections (
                provider, account_label, host_id, enabled, error_category,
                last_attempt_at, updated_at
            ) VALUES ('openai', 'personal', 'mac-mini', TRUE, NULL, ?, ?)
            """,
            [now, now],
        )
        with tempfile.TemporaryDirectory(
            prefix="advisory-reader-test-", dir=snapshot_scratch_root(source)
        ) as directory:
            scratch = Path(directory)
            request = {
                "source": str(source),
                "snapshot": str(scratch / source.name),
                "analyzer_id": "deterministic.connector_freshness",
                "target_id": "fleet",
                "source_version": "facts:v1",
                "control_store": None,
            }
            completed = subprocess.run(
                [sys.executable, "-m", "drover.server.advisory.snapshot_reader"],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            response = json.loads(completed.stdout)
            snapshot = decode_snapshot(response["snapshot"])

    assert snapshot.source_version == "facts:v1"
    assert any(item.provider == "openai" for item in snapshot.provider_connections)
    assert not scratch.exists()


def test_parent_removes_child_scratch_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "lake", duckdb_path=source)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(snapshot_process.subprocess, "run", timeout)
    with pytest.raises(TimeoutError, match="advisory snapshot exceeded"):
        snapshot_process.read_operational_snapshot_in_child(
            source, "deterministic.connector_freshness", "fleet", "facts:v1"
        )

    assert not list(snapshot_scratch_root(source).glob("drover-advisory-*"))
