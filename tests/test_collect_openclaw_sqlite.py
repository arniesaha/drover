"""OpenClaw SQLite transcript store source (drover.collect.openclaw_sqlite).

The fixture is synthetic: it recreates only the tables the source reads, in the
shape current OpenClaw releases use, plus one secrets table that must never be
touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from drover.collect import openclaw_sqlite
from drover.collect.__main__ import _build_sources
from drover.collect.__main__ import main as collect_main
from drover.collect.openclaw_sqlite import (
    OpenClawSqliteSource,
    connect_read_only,
    discover_agent_databases,
)
from drover.collect.sources import OpenClawSource
from drover.dedup import make_dedup_key
from drover.parsers import parse_openclaw_sessions

SECRET_MARKER = "sk-fixture-secret-do-not-import"
SESSION_A = "0a000000-0000-4000-8000-00000000000a"
SESSION_B = "0b000000-0000-4000-8000-00000000000b"
BASE_MS = 1790000000000

_SCHEMA = """
CREATE TABLE session_nodes (
  session_key TEXT NOT NULL PRIMARY KEY,
  current_session_id TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  created_via TEXT,
  parent_session_key TEXT,
  spawned_by TEXT,
  label TEXT,
  display_name TEXT
) STRICT;
CREATE TABLE conversations (
  conversation_id TEXT NOT NULL PRIMARY KEY,
  channel TEXT NOT NULL,
  account_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  peer_id TEXT NOT NULL,
  delivery_target TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE session_windows (
  session_id TEXT NOT NULL PRIMARY KEY,
  session_key TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  chat_type TEXT,
  channel TEXT,
  account_id TEXT,
  primary_conversation_id TEXT,
  model_provider TEXT,
  model TEXT,
  parent_session_key TEXT,
  spawned_by TEXT,
  display_name TEXT,
  FOREIGN KEY (session_key) REFERENCES session_nodes(session_key) ON DELETE CASCADE
) STRICT;
CREATE TABLE session_conversations (
  session_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'primary',
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, conversation_id, role)
) STRICT;
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_json TEXT,
  created_at INTEGER NOT NULL,
  event_zstd BLOB,
  event_utf8_bytes INTEGER,
  navigation_json TEXT,
  PRIMARY KEY (session_id, seq),
  CHECK (
    (event_json IS NOT NULL AND event_zstd IS NULL)
    OR (event_json IS NULL AND event_zstd IS NOT NULL)
  )
) STRICT;
CREATE TABLE schema_meta (
  meta_key TEXT NOT NULL PRIMARY KEY,
  role TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  agent_id TEXT,
  app_version TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE auth_profile_store (
  store_key TEXT NOT NULL PRIMARY KEY,
  store_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
"""


def _iso(offset_s: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(BASE_MS / 1000 + offset_s, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _records(prefix: str, *, cwd: str = "/tmp/drover-fixture") -> list[dict]:
    """A header, a user turn, an assistant turn with a tool call, a tool result."""
    return [
        {"type": "session", "version": 3, "id": f"{prefix}-hdr", "cwd": cwd},
        {
            "type": "message",
            "id": f"{prefix}-1",
            "timestamp": _iso(1),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"hello from {prefix}"}],
            },
        },
        {
            "type": "message",
            "id": f"{prefix}-2",
            "parentId": f"{prefix}-1",
            "timestamp": _iso(2),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "exec",
                        "arguments": {"command": "ls"},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": f"{prefix}-3",
            "timestamp": _iso(3),
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "exec",
                "content": [{"type": "text", "text": "README.md"}],
            },
        },
    ]


def _db_path(state_dir: Path, agent: str = "main") -> Path:
    path = state_dir / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _create_store(state_dir: Path, agent: str = "main") -> Path:
    path = _db_path(state_dir, agent)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta VALUES ('agent', 'agent', 9, ?, '2026.9.9', ?, ?)",
        (agent, BASE_MS, BASE_MS),
    )
    conn.execute(
        "INSERT INTO auth_profile_store VALUES ('default', ?, ?)",
        (json.dumps({"token": SECRET_MARKER}), BASE_MS),
    )
    conn.commit()
    conn.close()
    return path


def _add_session(
    path: Path,
    session_id: str,
    *,
    key: str,
    channel: Optional[str] = "telegram",
    label: Optional[str] = None,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO session_nodes (session_key, current_session_id, entry_json, "
        "updated_at, created_via, label) VALUES (?, ?, ?, ?, 'channel', ?)",
        (key, session_id, json.dumps({"authToken": SECRET_MARKER}), BASE_MS, label),
    )
    conversation_id = f"conv-{session_id[:2]}"
    conn.execute(
        "INSERT INTO conversations VALUES (?, 'telegram', 'acct', 'direct', "
        "'peer-private', 'target-private', ?, ?)",
        (conversation_id, BASE_MS, BASE_MS),
    )
    conn.execute(
        "INSERT INTO session_windows (session_id, session_key, created_at, "
        "updated_at, chat_type, channel, model_provider, model) "
        "VALUES (?, ?, ?, ?, 'direct', ?, 'anthropic', 'claude-test')",
        (session_id, key, BASE_MS, BASE_MS, channel),
    )
    conn.execute(
        "INSERT INTO session_conversations VALUES (?, ?, 'primary', ?, ?)",
        (session_id, conversation_id, BASE_MS, BASE_MS),
    )
    conn.commit()
    conn.close()


def _append(path: Path, session_id: str, records: list[Any]) -> None:
    conn = sqlite3.connect(path)
    start = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM transcript_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    for offset, record in enumerate(records, start=1):
        seq = start + offset
        if isinstance(record, bytes):
            conn.execute(
                "INSERT INTO transcript_events (session_id, seq, created_at, "
                "event_zstd, event_utf8_bytes) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, BASE_MS + seq * 1000, record, len(record)),
            )
        else:
            text = record if isinstance(record, str) else json.dumps(record)
            conn.execute(
                "INSERT INTO transcript_events (session_id, seq, event_json, "
                "created_at) VALUES (?, ?, ?, ?)",
                (session_id, seq, text, BASE_MS + seq * 1000),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """One agent store with two sessions of four records each."""
    root = tmp_path / "openclaw-state"
    path = _create_store(root)
    _add_session(path, SESSION_A, key="agent:main:main", label="Fixture topic")
    _add_session(path, SESSION_B, key="agent:main:telegram:dm", channel=None)
    _append(path, SESSION_A, _records("a"))
    _append(path, SESSION_B, _records("b"))
    return root


def _source(state_dir: Path, **kwargs: Any) -> OpenClawSqliteSource:
    kwargs.setdefault("busy_timeout_ms", 20)
    kwargs.setdefault("retry_delay_s", 0.01)
    return OpenClawSqliteSource(state_dir=state_dir, **kwargs)


def _sessions(cursor: dict, agent: str = "main") -> dict:
    return cursor["agents"][agent]["sessions"]


# --- mapping ---


def test_discovers_agent_databases_under_state_dir(tmp_path: Path) -> None:
    _create_store(tmp_path, "main")
    _create_store(tmp_path, "research")
    found = discover_agent_databases(tmp_path)
    assert [agent for agent, _ in found] == ["main", "research"]
    assert discover_agent_databases(tmp_path / "missing") == []


def test_initial_read_maps_to_openclaw_event_shape(state_dir: Path) -> None:
    batch = _source(state_dir).collect({})

    assert batch.diagnostics == []
    # The session header only feeds session state, exactly like the JSONL source.
    assert [e.id for e in batch.events] == ["a-1", "a-2", "a-3", "b-1", "b-2", "b-3"]
    assert _sessions(batch.cursor) == {SESSION_A: 4, SESSION_B: 4}

    user, assistant, tool_result = batch.events[:3]
    assert user.session_id == SESSION_A
    assert user.agent_id == "openclaw"
    assert user.event_type == "message"
    assert user.timestamp.isoformat() == _iso(1).replace("Z", "+00:00")
    assert user.message is not None and user.message.role == "user"
    assert user.message.content == [{"type": "text", "text": "hello from a"}]
    assert user.tool_calls is None

    raw = user.raw_data
    assert raw["harness"] == "openclaw"
    assert raw["agent_id"] == "main"
    assert raw["session_uuid"] == SESSION_A
    assert raw["session_key"] == "agent:main:main"
    assert raw["channel"] == "telegram"
    assert raw["topic"] == "Fixture topic"
    assert raw["harness_version"] == "2026.9.9"
    assert raw["cwd"] == "/tmp/drover-fixture"
    assert raw["openclaw_store"] == {
        "kind": "sqlite",
        "seq": 2,
        "chat_type": "direct",
        "model_provider": "anthropic",
        "model": "claude-test",
        "created_via": "channel",
    }

    assert assistant.tool_calls is not None
    assert [(c.tool_name, c.input) for c in assistant.tool_calls] == [
        ("exec", {"command": "ls"})
    ]
    assert tool_result.message is not None
    assert tool_result.message.role == "toolResult"

    # session_windows.channel is NULL for B; the primary conversation supplies it.
    assert batch.events[3].raw_data["channel"] == "telegram"


def test_sqlite_events_dedupe_against_the_jsonl_source(
    state_dir: Path, tmp_path: Path
) -> None:
    """A session shipped from JSONL before the migration must not double up."""
    jsonl = tmp_path / "a.jsonl"
    records = _records("a")
    records[0] = {**records[0], "id": SESSION_A}  # JSONL names the session there
    jsonl.write_text("\n".join(json.dumps(r) for r in records))

    def keys(events: list) -> list[str]:
        return [
            make_dedup_key(
                e.timestamp.isoformat(),
                e.agent_id,
                e.session_id,
                e.event_type,
                json.dumps(e.message.content if e.message else None),
            )
            for e in events
        ]

    from_sqlite = [
        e for e in _source(state_dir).collect({}).events if e.session_id == SESSION_A
    ]
    assert keys(from_sqlite) == keys(parse_openclaw_sessions(str(jsonl)))


def test_secret_tables_and_columns_are_never_imported(state_dir: Path) -> None:
    batch = _source(state_dir).collect({})
    dumped = "\n".join(e.model_dump_json() for e in batch.events)
    assert batch.events
    assert SECRET_MARKER not in dumped
    assert "peer-private" not in dumped
    assert "target-private" not in dumped


# --- incremental reads and watermark resume ---


def test_incremental_read_returns_only_new_rows(state_dir: Path) -> None:
    src = _source(state_dir)
    first = src.collect({})
    assert src.collect(first.cursor).events == []

    path = _db_path(state_dir)
    more = _records("a2")[1:3]
    _append(path, SESSION_A, more)

    second = src.collect(first.cursor)
    assert [e.id for e in second.events] == ["a2-1", "a2-2"]
    assert _sessions(second.cursor) == {SESSION_A: 6, SESSION_B: 4}
    # Resuming mid-session still replays the header for session-level fields.
    assert second.events[0].raw_data["cwd"] == "/tmp/drover-fixture"
    assert src.collect(second.cursor).events == []


def test_small_batches_and_run_budget_resume_without_gaps_or_duplicates(
    state_dir: Path,
) -> None:
    src = _source(state_dir, batch_size=2, max_events_per_run=2)
    cursor: dict = {}
    seen: list[str] = []
    runs = 0
    while True:
        # A fresh JSON round trip per run stands in for a collector restart.
        batch = src.collect(json.loads(json.dumps(cursor)))
        if not batch.events:
            break
        assert len(batch.events) <= 2
        seen.extend(e.id for e in batch.events)
        cursor = batch.cursor
        runs += 1
    assert runs == 3
    assert seen == ["a-1", "a-2", "a-3", "b-1", "b-2", "b-3"]


def test_failure_partway_keeps_progress_and_resumes(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = openclaw_sqlite._Reader.events_after

    def flaky(self, session_id: str, seq: int, limit: int):
        if session_id == SESSION_B:
            raise sqlite3.OperationalError("database is locked")
        return real(self, session_id, seq, limit)

    monkeypatch.setattr(openclaw_sqlite._Reader, "events_after", flaky)
    src = _source(state_dir)
    first = src.collect({})
    assert [e.id for e in first.events] == ["a-1", "a-2", "a-3"]
    assert _sessions(first.cursor) == {SESSION_A: 4}
    assert any("busy" in d for d in first.diagnostics)

    monkeypatch.setattr(openclaw_sqlite._Reader, "events_after", real)
    second = src.collect(first.cursor)
    assert [e.id for e in second.events] == ["b-1", "b-2", "b-3"]


def test_undecodable_rows_are_skipped_and_do_not_stall(state_dir: Path) -> None:
    path = _db_path(state_dir)
    _append(path, SESSION_A, ["{not json", "[1, 2]", _records("a2")[1]])
    src = _source(state_dir)
    batch = src.collect({})
    assert [e.id for e in batch.events if e.session_id == SESSION_A] == [
        "a-1",
        "a-2",
        "a-3",
        "a2-1",
    ]
    assert _sessions(batch.cursor)[SESSION_A] == 7
    assert any("skipped 2 transcript row" in d for d in batch.diagnostics)


def test_records_without_id_or_timestamp_get_stable_fallbacks(state_dir: Path) -> None:
    path = _db_path(state_dir)
    _append(path, SESSION_A, [{"type": "custom", "customType": "note"}])
    src = _source(state_dir)
    first = src.collect({}).events[3]
    again = src.collect({}).events[3]
    assert first.id == f"openclaw:main:{SESSION_A}:5"
    assert first.timestamp.timestamp() == (BASE_MS + 5000) / 1000
    assert (first.id, first.timestamp) == (again.id, again.timestamp)


def test_rewritten_transcript_is_reread(state_dir: Path) -> None:
    src = _source(state_dir)
    first = src.collect({})
    path = _db_path(state_dir)
    conn = sqlite3.connect(path)
    conn.execute(
        "DELETE FROM transcript_events WHERE session_id = ? AND seq > 2", (SESSION_A,)
    )
    conn.commit()
    conn.close()

    second = src.collect(first.cursor)
    assert [e.id for e in second.events] == ["a-1"]
    assert _sessions(second.cursor)[SESSION_A] == 2
    assert any("rewritten" in d for d in second.diagnostics)


def test_deleted_sessions_leave_the_cursor(state_dir: Path) -> None:
    src = _source(state_dir)
    first = src.collect({})
    conn = sqlite3.connect(_db_path(state_dir))
    conn.execute("DELETE FROM transcript_events WHERE session_id = ?", (SESSION_B,))
    conn.commit()
    # Rows gone but the session still exists: keep the watermark.
    assert SESSION_B in _sessions(src.collect(first.cursor).cursor)
    conn.execute("DELETE FROM session_windows WHERE session_id = ?", (SESSION_B,))
    conn.commit()
    conn.close()
    assert _sessions(src.collect(first.cursor).cursor) == {SESSION_A: 4}


# --- zstd payloads ---


def _zstd(record: dict) -> bytes:
    zstd = pytest.importorskip("compression.zstd")
    return zstd.compress(json.dumps(record).encode())


def test_zstd_compressed_events_are_decoded(state_dir: Path) -> None:
    _append(_db_path(state_dir), SESSION_A, [_zstd(_records("z")[1])])
    batch = _source(state_dir).collect({})
    assert "z-1" in [e.id for e in batch.events]
    assert batch.diagnostics == []


def test_missing_zstd_decoder_pauses_the_session_without_a_gap(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _db_path(state_dir)
    _append(path, SESSION_A, [_zstd(_records("z")[1]), _records("a2")[1]])
    decoder = openclaw_sqlite._zstd_decompress
    monkeypatch.setattr(openclaw_sqlite, "_zstd_decompress", None)

    src = _source(state_dir)
    first = src.collect({})
    assert [e.id for e in first.events] == ["a-1", "a-2", "a-3", "b-1", "b-2", "b-3"]
    assert _sessions(first.cursor)[SESSION_A] == 4
    assert any("zstd" in d for d in first.diagnostics)

    monkeypatch.setattr(openclaw_sqlite, "_zstd_decompress", decoder)
    second = src.collect(first.cursor)
    assert [e.id for e in second.events] == ["z-1", "a2-1"]


# --- unknown schema ---


def test_unknown_schema_fails_soft_with_a_diagnostic(tmp_path: Path) -> None:
    path = _db_path(tmp_path, "legacy")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO messages (body) VALUES ('hi')")
    conn.commit()
    conn.close()
    good = _create_store(tmp_path, "main")
    _add_session(good, SESSION_A, key="agent:main:main")
    _append(good, SESSION_A, _records("a"))

    batch = _source(tmp_path).collect({})

    assert [e.id for e in batch.events] == ["a-1", "a-2", "a-3"]
    assert len(batch.diagnostics) == 1
    message = batch.diagnostics[0]
    assert "agent 'legacy'" in message
    assert "unknown OpenClaw schema" in message
    assert "no transcript_events table" in message
    assert "messages" in message


def test_transcript_table_with_missing_columns_fails_soft(tmp_path: Path) -> None:
    path = _db_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transcript_events (session_id TEXT, body TEXT)")
    conn.commit()
    conn.close()

    batch = _source(tmp_path).collect({})

    assert batch.events == []
    assert "missing column(s): seq, created_at, event_json or event_zstd" in (
        batch.diagnostics[0]
    )


def test_transcript_only_schema_still_collects(tmp_path: Path) -> None:
    """Session metadata tables are optional; the transcript alone is enough."""
    path = _db_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, "
        "event_json TEXT, created_at INTEGER, PRIMARY KEY (session_id, seq))"
    )
    conn.commit()
    conn.close()
    _append(path, SESSION_A, _records("a"))

    batch = _source(tmp_path).collect({})

    assert batch.diagnostics == []
    assert [e.id for e in batch.events] == ["a-1", "a-2", "a-3"]
    assert "channel" not in batch.events[0].raw_data


def test_corrupt_file_and_missing_store_fail_soft(tmp_path: Path) -> None:
    empty = _source(tmp_path).collect({"agents": {"main": {"sessions": {"s": 3}}}})
    assert empty.events == []
    assert "no OpenClaw agent database" in empty.diagnostics[0]
    # The stored watermark survives a run in which the store is absent.
    assert _sessions(empty.cursor) == {"s": 3}

    _db_path(tmp_path).write_bytes(b"this is not a sqlite database" * 100)
    batch = _source(tmp_path).collect({})
    assert batch.events == []
    assert "cannot read database" in batch.diagnostics[0]


# --- read-only enforcement ---


def test_connection_cannot_write(state_dir: Path) -> None:
    conn = connect_read_only(_db_path(state_dir), busy_timeout_ms=20)
    try:
        for statement in (
            "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
            "VALUES ('x', 1, '{}', 1)",
            "DELETE FROM transcript_events",
            "CREATE TABLE drover_probe (id INTEGER)",
            "PRAGMA user_version = 7",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(statement)
        # Even with query_only switched off, mode=ro still refuses writes.
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM transcript_events")
    finally:
        conn.close()


def test_read_only_open_never_creates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect_read_only(missing, busy_timeout_ms=20)
    assert not missing.exists()


def test_collect_leaves_the_store_byte_identical(state_dir: Path) -> None:
    path = _db_path(state_dir)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    listing = sorted(p.name for p in path.parent.iterdir())

    assert _source(state_dir).collect({}).events

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in path.parent.iterdir()) == listing


# --- busy database ---


def test_locked_database_is_skipped_not_fatal(state_dir: Path) -> None:
    src = _source(state_dir)
    first = src.collect({})
    _append(_db_path(state_dir), SESSION_A, [_records("a2")[1]])

    writer = sqlite3.connect(_db_path(state_dir))
    writer.execute("BEGIN EXCLUSIVE")
    try:
        locked = src.collect(first.cursor)
    finally:
        writer.rollback()
        writer.close()

    assert locked.events == []
    assert locked.cursor["agents"] == first.cursor["agents"]
    assert len(locked.diagnostics) == 1
    assert "database busy after 2 retries" in locked.diagnostics[0]

    assert [e.id for e in src.collect(locked.cursor).events] == ["a2-1"]


def test_briefly_locked_database_is_retried(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = sqlite3.connect(_db_path(state_dir))
    writer.execute("BEGIN EXCLUSIVE")
    sleeps: list[float] = []

    def release(seconds: float) -> None:
        sleeps.append(seconds)
        if writer.in_transaction:
            writer.rollback()

    monkeypatch.setattr(openclaw_sqlite.time, "sleep", release)
    try:
        batch = _source(state_dir).collect({})
    finally:
        writer.close()

    assert len(sleeps) == 1
    assert batch.diagnostics == []
    assert len(batch.events) == 6


def test_wal_store_with_a_live_writer_reads_committed_rows_only(
    state_dir: Path,
) -> None:
    path = _db_path(state_dir)
    writer = sqlite3.connect(path)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    src = _source(state_dir)
    first = src.collect({})
    assert len(first.events) == 6 and first.diagnostics == []

    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
        "VALUES (?, 5, ?, ?)",
        (SESSION_A, json.dumps(_records("w")[1]), BASE_MS),
    )
    # An open write transaction neither blocks the reader nor leaks into it.
    during = src.collect(first.cursor)
    assert during.events == [] and during.diagnostics == []
    # ...and the reader does not hold the writer up either.
    writer.commit()
    writer.close()

    assert [e.id for e in src.collect(during.cursor).events] == ["w-1"]


def test_reader_does_not_block_the_writer(state_dir: Path) -> None:
    """Between statements the source holds no lock, so a write goes straight in."""
    path = _db_path(state_dir)
    conn = connect_read_only(path, busy_timeout_ms=20)
    reader = openclaw_sqlite._Reader(conn=conn, busy_retries=0, retry_delay_s=0)
    try:
        reader.discover_schema()
        assert reader.events_after(SESSION_A, 0, 2)
        assert not conn.in_transaction
        writer = sqlite3.connect(path, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
            "VALUES (?, 99, '{}', 1)",
            (SESSION_A,),
        )
        writer.commit()
        writer.close()
    finally:
        conn.close()


# --- config and CLI ---


def _oc_cfg(**openclaw: Any) -> dict:
    return {"host_id": "h", "sources": {"openclaw": {"enabled": True, **openclaw}}}


def test_auto_store_adds_sqlite_only_when_a_store_exists(
    state_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "old" / "agents" / "main" / "sessions"
    jsonl_only = _build_sources(_oc_cfg(root=str(root), state_dir=str(tmp_path / "x")))
    assert [type(s) for s in jsonl_only] == [OpenClawSource]

    both = _build_sources(_oc_cfg(root=str(root), state_dir=str(state_dir)))
    assert [s.id for s in both] == ["openclaw", "openclaw_sqlite"]
    assert both[1].state_dir == state_dir


def test_state_dir_is_derived_from_a_pre_sqlite_config(state_dir: Path) -> None:
    root = state_dir / "agents" / "main" / "sessions"
    sources = _build_sources(_oc_cfg(root=str(root)))
    assert [s.id for s in sources] == ["openclaw", "openclaw_sqlite"]
    assert sources[1].state_dir == state_dir


def test_explicit_store_selection_and_tuning(state_dir: Path, tmp_path: Path) -> None:
    root = str(state_dir / "agents" / "main" / "sessions")
    jsonl = _build_sources(_oc_cfg(store="jsonl", root=root))
    assert [s.id for s in jsonl] == ["openclaw"]

    sqlite_only = _build_sources(
        _oc_cfg(
            store="sqlite",
            root=root,
            state_dir=str(tmp_path / "not-there-yet"),
            agents=["main"],
            batch_size=50,
            max_events_per_run=10,
            busy_timeout_ms=100,
        )
    )
    assert [s.id for s in sqlite_only] == ["openclaw_sqlite"]
    src = sqlite_only[0]
    assert (src.agents, src.batch_size, src.max_events_per_run) == (("main",), 50, 10)
    assert src.busy_timeout_ms == 100

    import click

    with pytest.raises(click.ClickException, match="store must be one of"):
        _build_sources(_oc_cfg(store="postgres"))
    with pytest.raises(click.ClickException, match="needs root"):
        _build_sources(_oc_cfg(store="jsonl"))


def test_agents_filter_limits_databases(tmp_path: Path) -> None:
    _create_store(tmp_path, "main")
    _create_store(tmp_path, "research")
    src = _source(tmp_path, agents=("research",))
    assert [agent for agent, _ in src.databases()] == ["research"]


def _write_cli_config(cfg: Path, *, state_dir: Path, tmp_path: Path) -> None:
    cfg.write_text(f"""
host_id = "test-host"
remote_host = "hub.test"
remote_user = "tester"
state_dir = "{tmp_path / 'state'}"
staging_dir = "{tmp_path / 'staging'}"

[sources.openclaw]
enabled = true
store = "sqlite"
state_dir = "{state_dir}"
""")


def _staged_ids(staging: Path) -> list[str]:
    ids: list[str] = []
    for path in sorted(staging.glob("openclaw_sqlite-*.jsonl")):
        ids.extend(json.loads(line)["id"] for line in path.read_text().splitlines())
    return ids


def test_cli_run_persists_the_keyed_cursor_and_resumes(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "collect.toml"
    _write_cli_config(cfg, state_dir=state_dir, tmp_path=tmp_path)
    staging = tmp_path / "staging"
    cursor_file = tmp_path / "state" / "openclaw_sqlite.cursor"
    runner = CliRunner()

    def run(run_id: str) -> None:
        # run_id has one-second resolution; pin it so each run stages its own file.
        import drover.collect.__main__ as cli

        class _Clock(cli.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(run_id).replace(tzinfo=tz)

        monkeypatch.setattr(cli, "datetime", _Clock)
        result = runner.invoke(collect_main, ["--config", str(cfg), "run", "--dry-run"])
        assert result.exit_code == 0, result.output

    run("2026-10-01T00:00:01")
    assert _staged_ids(staging) == ["a-1", "a-2", "a-3", "b-1", "b-2", "b-3"]
    payload = json.loads(cursor_file.read_text())
    assert payload["agents"]["main"]["sessions"] == {SESSION_A: 4, SESSION_B: 4}
    assert payload["watermark_iso"] == _iso(3).replace("Z", "+00:00")

    # Nothing new: no extra staging file, cursor intact.
    run("2026-10-01T00:00:02")
    assert len(list(staging.glob("*.jsonl"))) == 1
    assert json.loads(cursor_file.read_text())["agents"] == payload["agents"]

    _append(_db_path(state_dir), SESSION_B, [_records("b2")[1]])
    run("2026-10-01T00:00:03")
    assert _staged_ids(staging)[-1] == "b2-1"
    assert len(_staged_ids(staging)) == len(set(_staged_ids(staging))) == 7

    status = runner.invoke(collect_main, ["--config", str(cfg), "status"])
    assert "openclaw_sqlite" in status.output


def test_cli_does_not_advance_the_cursor_when_shipping_fails(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import drover.collect.__main__ as cli

    cfg = tmp_path / "collect.toml"
    _write_cli_config(cfg, state_dir=state_dir, tmp_path=tmp_path)

    def boom(**_: Any):
        raise cli.ShipError("rsync exited 23")

    monkeypatch.setattr(cli, "ship_staging", boom)
    result = CliRunner().invoke(collect_main, ["--config", str(cfg), "run"])
    assert result.exit_code == 2, result.output
    assert not (tmp_path / "state" / "openclaw_sqlite.cursor").exists()
