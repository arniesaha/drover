"""Drain-handoff migration contract for the Rust harness daemon."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.rust_phase2


def _binary() -> Path:
    binary = Path("target/release/drover-harnessd")
    if os.environ.get("HARNESSD_IMPL") != "rs" or not binary.exists():
        pytest.skip("requires a built Rust harnessd")
    return binary


def _run(binary: Path, home: Path, handoff: Path, *extra: str) -> str:
    completed = subprocess.run(
        [
            str(binary),
            "--host-id",
            "migration-host",
            "--host-token",
            "migration-token",
            "--import-jsonl",
            str(handoff),
            *extra,
        ],
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def test_rust_imports_drain_handoff_idempotently_and_dry_run_writes_no_records(tmp_path):
    binary = _binary()
    handoff = tmp_path / "import.jsonl"
    session = {
        "kind": "session",
        "session": {
            "session_id": "python-session-1",
            "harness": "codex",
            "mode": "structured",
            "cwd": "/work/project",
            "status": "errored",
            "native_session_id": "native-1",
            "created_at": "2026-01-02T03:04:05+00:00",
        },
    }
    event = {
        "kind": "event",
        "event_id": "python-event-1",
        "dedup_key": "python-dedup-1",
        "session_id": "python-session-1",
        "type": "status",
        "text": "drained",
        "payload": {"source": "python"},
        "seq": 7,
        "ts": "2026-01-02T03:04:06+00:00",
    }
    handoff.write_text("\n".join(json.dumps(row) for row in (session, event)) + "\n")

    home = tmp_path / "home"
    assert "sessions=1 events=1" in _run(binary, home, handoff)
    db = sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite")
    assert db.execute("SELECT session_id, data FROM sessions").fetchone()[0] == "python-session-1"
    assert db.execute("SELECT event_id, dedup_key, seq FROM events").fetchall() == [
        ("python-event-1", "python-dedup-1", 7)
    ]
    assert "events=0" in _run(binary, home, handoff)
    assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)

    dry_home = tmp_path / "dry-home"
    assert "sessions=1 events=1" in _run(binary, dry_home, handoff, "--import-dry-run")
    dry_db = sqlite3.connect(dry_home / ".drover/harnessd/harnessd.sqlite")
    assert dry_db.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
    assert dry_db.execute("SELECT count(*) FROM events").fetchone() == (0,)
