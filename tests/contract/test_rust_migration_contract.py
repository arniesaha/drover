"""Drain-handoff migration contract for the Rust harness daemon."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _binary() -> Path:
    binary = Path("target/release/drover-harnessd")
    if os.environ.get("HARNESSD_IMPL") != "rs":
        pytest.skip("requires a built Rust harnessd")
    assert binary.exists(), "Rust migration gate requires a built binary"
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


def test_rust_imports_drain_handoff_idempotently_and_dry_run_writes_no_records(
    tmp_path,
):
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
    assert (
        db.execute("SELECT session_id, data FROM sessions").fetchone()[0]
        == "python-session-1"
    )
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


def test_rust_invalid_handoff_is_atomic_even_in_dry_run(tmp_path):
    binary = _binary()
    handoff = tmp_path / "bad.jsonl"
    handoff.write_text(
        json.dumps(
            {
                "kind": "session",
                "session": {"session_id": "s", "harness": "codex", "status": "running"},
            }
        )
        + '\n{"kind":"event","event_id":"missing-fields"}\n'
    )
    for suffix, extra in [("real", []), ("dry", ["--import-dry-run"])]:
        home = tmp_path / suffix
        with pytest.raises(subprocess.CalledProcessError):
            _run(binary, home, handoff, *extra)
        with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
            assert db.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM events").fetchone() == (0,)


def test_rust_import_preserves_drain_state_lifecycle_and_quarantine(tmp_path):
    binary = _binary()
    handoff = tmp_path / "drain.jsonl"
    event = {
        "kind": "event",
        "event_id": "e",
        "dedup_key": "dedup",
        "session_id": "s",
        "seq": 19,
        "type": "status",
        "ts": "2026-10-19T00:00:00+00:00",
        "payload": {"original": "retained"},
    }
    records = [
        {
            "kind": "session",
            "session": {
                "session_id": "s",
                "harness": "codex",
                "mode": "structured",
                "cwd": str(tmp_path),
                "status": "running",
                "pid": 99999,
                "native_session_id": "native",
                "retention_policy": "keep",
                "lifecycle_generation": 4,
            },
        },
        event,
        {
            "kind": "lifecycle",
            "record": {
                "operation_id": "op",
                "session_id": "s",
                "state": "pending",
                "generation": 4,
            },
        },
        {
            "kind": "quarantine",
            "record": {
                "event_id": "e",
                "session_id": "s",
                "reason": "session_owned_by_another_host",
            },
        },
    ]
    handoff.write_text("".join(json.dumps(row) + "\n" for row in records))
    home = tmp_path / "home"
    _run(binary, home, handoff)
    with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
        data = json.loads(db.execute("SELECT data FROM sessions").fetchone()[0])
        assert data["status"] == "errored"
        assert data.get("pid") is None
        assert data["native_session_id"] == "native"
        assert data["retention_policy"] == "keep"
        assert data["lifecycle_generation"] == 4
        assert db.execute(
            "SELECT next_seq FROM session_sequences WHERE session_id='s'"
        ).fetchone() == (20,)
        assert db.execute("SELECT reason FROM event_quarantine").fetchone() == (
            "session_owned_by_another_host",
        )
        assert (
            json.loads(
                db.execute(
                    "SELECT data FROM handoff_records WHERE kind='lifecycle'"
                ).fetchone()[0]
            )["generation"]
            == 4
        )


def test_rust_imports_real_python_drain_export(
    tmp_path, monkeypatch, request, fake_hub
):
    import sys

    from .test_harnessd_contract import request as call

    binary = _binary()
    monkeypatch.setenv("HARNESSD_IMPL", "py")
    daemon = request.getfixturevalue("daemon")
    fake_hub.script("/harness/events", 500, {})
    status, session = call(
        daemon,
        "/sessions",
        payload={"harness": "codex", "mode": "structured", "cwd": str(daemon.root)},
    )
    assert status == 201
    assert call(daemon, "/drain", payload={"timeout": 5}) == (
        200,
        {"draining": True, "quiescent": True},
    )
    assert call(daemon, "/sessions", payload={"harness": "shell"})[0] == 503
    daemon.process.terminate()
    daemon.process.wait(timeout=10)
    registry = daemon.root / "data/drover.registry.duckdb"
    import hashlib

    before = hashlib.sha256(registry.read_bytes()).hexdigest()
    handoff = tmp_path / "python-export.jsonl"
    subprocess.run(
        [
            sys.executable,
            "scripts/export_harnessd_handoff.py",
            "--registry",
            str(registry),
            "--host-id",
            "contract-host",
            "--output",
            str(handoff),
        ],
        check=True,
        capture_output=True,
    )
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == before
    assert handoff.stat().st_mode & 0o777 == 0o600
    home = tmp_path / "rust-home"
    _run(binary, home, handoff)
    with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
        data = json.loads(
            db.execute(
                "SELECT data FROM sessions WHERE session_id=?", [session["session_id"]]
            ).fetchone()[0]
        )
        assert data["harness"] == "codex"
        assert data["status"] == "errored"
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] > 0


def _event_handoff(tmp_path, events=7):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "kind": "event",
                    "event_id": f"e-{i}",
                    "dedup_key": f"d-{i}",
                    "session_id": "rejected",
                    "type": "status",
                    "payload": {"original": i},
                    "seq": i,
                    "ts": "2026-01-02T03:04:06+00:00",
                }
            )
            + "\n"
            for i in range(events)
        )
    )
    return path


def _start(binary, home, fake_hub):
    process = subprocess.Popen(
        [
            str(binary),
            "--host-id",
            "migration-host",
            "--host-token",
            "contract-token",
            "--listen",
            "127.0.0.1:0",
            "--central-url",
            fake_hub.url,
        ],
        env={**os.environ, "HOME": str(home)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    line = process.stdout.readline()
    assert line.startswith("LISTENING"), line
    return process, "http://" + line.split()[1]


@pytest.mark.parametrize(
    "status,reason,count",
    [
        (200, "session_missing_after_registration_window", 7),
        (403, "session_owned_by_another_host", 41),
    ],
)
def test_rust_ownership_receipt_retains_originals_and_stops_restart_replay(
    tmp_path, fake_hub, status, reason, count
):
    from .support import wait_until

    binary = _binary()
    home = tmp_path / "home"
    _run(binary, home, _event_handoff(tmp_path, count))
    fake_hub.script("/harness/events", status, {"rejected": {"rejected": reason}})
    process, _ = _start(binary, home, fake_hub)
    dbpath = home / ".drover/harnessd/harnessd.sqlite"
    try:

        def receipts():
            with sqlite3.connect(dbpath) as db:
                return (
                    db.execute("SELECT count(*) FROM event_quarantine").fetchone()[0]
                    == count
                )

        wait_until(receipts)
        with sqlite3.connect(dbpath) as db:
            assert db.execute(
                "SELECT count(*) FROM events WHERE pushed_at IS NULL"
            ).fetchone() == (count,)
            assert json.loads(
                db.execute(
                    "SELECT payload FROM events ORDER BY seq LIMIT 1"
                ).fetchone()[0]
            ) == {"original": 0}
        assert (
            next(r for r in fake_hub.requests if r["path"] == "/harness/events")[
                "body"
            ]["events"][0]["event_id"]
            == "e-0"
        )
    finally:
        process.terminate()
        process.wait(timeout=10)
    before = len([r for r in fake_hub.requests if r["path"] == "/harness/events"])
    process, _ = _start(binary, home, fake_hub)
    try:
        import time

        time.sleep(0.7)
        assert (
            len([r for r in fake_hub.requests if r["path"] == "/harness/events"])
            == before
        )
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize(
    "status,body", [(403, {"pending": ["rejected"]}), (403, {"error": "forbidden"})]
)
def test_rust_pending_and_auth_backoff_do_not_acknowledge(
    tmp_path, fake_hub, status, body
):
    import time

    from .support import wait_until

    binary = _binary()
    home = tmp_path / "home"
    _run(binary, home, _event_handoff(tmp_path))
    fake_hub.script("/harness/events", status, body)
    process, url = _start(binary, home, fake_hub)
    try:
        wait_until(
            lambda: any(r["path"] == "/harness/events" for r in fake_hub.requests)
        )
        time.sleep(0.7)
        assert (
            len([r for r in fake_hub.requests if r["path"] == "/harness/events"]) == 1
        )
        with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
            assert db.execute(
                "SELECT count(*) FROM events WHERE pushed_at IS NULL"
            ).fetchone() == (7,)
            assert db.execute("SELECT count(*) FROM event_quarantine").fetchone() == (
                0,
            )
        if "pending" in body:
            from types import SimpleNamespace

            from .test_harnessd_contract import request

            assert (
                request(
                    SimpleNamespace(url=url, token="contract-token"),
                    "/sessions",
                    payload={
                        "harness": "codex",
                        "mode": "structured",
                        "cwd": str(tmp_path),
                    },
                )[0]
                == 201
            )
            wait_until(
                lambda: len(
                    [r for r in fake_hub.requests if r["path"] == "/harness/events"]
                )
                >= 2
            )
            assert all(
                e["session_id"] != "rejected"
                for e in [
                    r for r in fake_hub.requests if r["path"] == "/harness/events"
                ][1]["body"]["events"]
            )
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_rust_retired_heartbeat_prevents_event_delivery(tmp_path, fake_hub):
    binary = _binary()
    home = tmp_path / "home"
    _run(binary, home, _event_handoff(tmp_path))
    fake_hub.script("/harness/hosts", 409, {"error": "host retired"})
    process, _ = _start(binary, home, fake_hub)
    process.wait(timeout=5)
    assert process.returncode == 0
    assert not [r for r in fake_hub.requests if r["path"] == "/harness/events"]


def test_rust_singleton_413_sends_identity_tombstone_and_retains_original(
    tmp_path, fake_hub
):
    from .support import wait_until

    binary = _binary()
    home = tmp_path / "home"
    _run(binary, home, _event_handoff(tmp_path, 1))
    fake_hub.script("/harness/events", 413, {})
    process, _ = _start(binary, home, fake_hub)
    try:

        def delivered():
            with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
                return (
                    db.execute("SELECT pushed_at FROM events").fetchone()[0] is not None
                )

        wait_until(delivered)
        batches = [
            r["body"]["events"]
            for r in fake_hub.requests
            if r["path"] == "/harness/events"
        ]
        assert len(batches) == 2
        for key in ["event_id", "session_id", "seq", "ts", "type"]:
            assert batches[0][0][key] == batches[1][0][key]
        assert batches[1][0]["payload"]["truncated"] is True
        assert (
            batches[1][0]["payload"]["reason"]
            == "event exceeds harness request byte limit"
        )
        with sqlite3.connect(home / ".drover/harnessd/harnessd.sqlite") as db:
            assert json.loads(
                db.execute("SELECT payload FROM events").fetchone()[0]
            ) == {"original": 0}
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_rust_upgrades_phase2_v0_ledger_without_resetting_sequence(tmp_path):
    binary = _binary()
    home = tmp_path / "home"
    dbpath = home / ".drover/harnessd/harnessd.sqlite"
    dbpath.parent.mkdir(parents=True)
    with sqlite3.connect(dbpath) as db:
        db.executescript(
            "CREATE TABLE events(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,session_id TEXT NOT NULL,seq INTEGER NOT NULL,type TEXT NOT NULL,text TEXT,payload TEXT NOT NULL,turn_id TEXT,ts TEXT NOT NULL,dedup_key TEXT UNIQUE NOT NULL,pushed_at TEXT); INSERT INTO events(event_id,session_id,seq,type,payload,ts,dedup_key) VALUES('old','s',99,'status','{}','2026-01-01','old-key');"
        )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    _run(binary, home, empty)
    with sqlite3.connect(dbpath) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)
        assert db.execute(
            "SELECT next_seq FROM session_sequences WHERE session_id='s'"
        ).fetchone() == (100,)
        assert db.execute("SELECT event_id,dedup_key FROM events").fetchall() == [
            ("old", "old-key")
        ]
