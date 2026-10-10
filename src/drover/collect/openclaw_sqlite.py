"""OpenClaw sessions from its per-agent SQLite transcript store.

Current OpenClaw releases keep transcripts in
``<state dir>/agents/<agentId>/agent/openclaw-agent.sqlite`` instead of JSONL
session files. This source reads ``transcript_events`` incrementally and maps
each record through the same mapper as the JSONL source.

The gateway owns these databases and uses a single SQLite writer, so this
reader is deliberately timid: ``mode=ro`` plus ``query_only``, autocommit, a
short busy timeout, and every statement is small and fully fetched before the
next one starts. A busy or unfamiliar database is skipped for the run, never
waited on.

The watermark is ``(session_id, seq)``, the table's primary key: one last
``seq`` per session, per agent. ``rowid`` is not used because the table has no
``INTEGER PRIMARY KEY``, so SQLite may reuse or renumber it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from drover.collect.sources import IncrementalBatch
from drover.models import AgentEvent
from drover.parsers import OpenClawRecordMapper

log = logging.getLogger("drover.collect.openclaw_sqlite")

AGENT_DB_GLOB = "agents/*/agent/openclaw-agent.sqlite"
DEFAULT_STATE_DIR = "~/.openclaw"
CURSOR_VERSION = 1

_EVENTS_TABLE = "transcript_events"
_EVENTS_REQUIRED = ("session_id", "seq", "created_at")
_EVENTS_PAYLOADS = ("event_json", "event_zstd")

# Everything this source may read besides transcript rows. Columns are an
# allow-list on purpose: the same database holds auth profiles, dispatch
# tokens, peer ids and delivery targets, none of which belong in Drover.
_SESSION_COLUMNS: dict[str, tuple[str, ...]] = {
    "session_windows": (
        "session_key",
        "channel",
        "chat_type",
        "model_provider",
        "model",
        "primary_conversation_id",
        "parent_session_key",
        "spawned_by",
        "display_name",
    ),
    "session_nodes": (
        "label",
        "display_name",
        "parent_session_key",
        "spawned_by",
        "created_via",
    ),
    "conversations": ("channel", "kind"),
    "session_conversations": ("conversation_id", "role"),
    "schema_meta": ("app_version",),
}
_SESSION_KEYS = {
    "session_windows": "session_id",
    "session_nodes": "session_key",
    "conversations": "conversation_id",
    "session_conversations": "session_id",
}

_MIN_SEQ = -(2**63)
# The schema caps a stored event at 4 MiB of UTF-8; anything larger is not a
# transcript record this source understands.
_MAX_EVENT_BYTES = 8 * 1024 * 1024


def _load_zstd() -> Optional[Callable[[bytes], bytes]]:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        zstd = None
    if zstd is not None:

        def _stdlib(blob: bytes) -> bytes:
            decoder = zstd.ZstdDecompressor()
            out = decoder.decompress(blob, max_length=_MAX_EVENT_BYTES)
            if not decoder.eof:
                raise ValueError("zstd frame is truncated or too large")
            return out

        return _stdlib
    try:
        import zstandard
    except ImportError:
        return None

    def _third_party(blob: bytes) -> bytes:
        return zstandard.ZstdDecompressor().decompress(
            blob, max_output_size=_MAX_EVENT_BYTES
        )

    return _third_party


_zstd_decompress = _load_zstd()


class _SchemaUnknown(Exception):
    """The database is not a transcript store this source recognizes."""


class _DecoderUnavailable(Exception):
    """A row is zstd-compressed and this interpreter cannot decode zstd."""


def discover_agent_databases(state_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(agent_id, db_path)`` for every agent store under ``state_dir``."""
    try:
        paths = sorted(state_dir.glob(AGENT_DB_GLOB))
    except OSError:
        return []
    return [(p.parent.parent.name, p) for p in paths if p.is_file()]


def connect_read_only(db_path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open ``db_path`` so that this process cannot create or modify it."""
    conn = sqlite3.connect(
        db_path.expanduser().resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=busy_timeout_ms / 1000.0,
        # Autocommit: no implicit transaction outlives a single statement.
        isolation_level=None,
    )
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    ):
        return True
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _epoch_datetime(value: Any) -> Optional[datetime]:
    """OpenClaw writes millisecond epochs; tolerate second epochs too."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    seconds = value / 1000.0 if abs(value) >= 1e11 else float(value)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class _Reader:
    """One agent database, queried in small fully-fetched statements."""

    conn: sqlite3.Connection
    busy_retries: int
    retry_delay_s: float
    payloads: tuple[str, ...] = ()
    tables: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        attempt = 0
        while True:
            try:
                # fetchall() finishes the statement, which releases the read
                # lock before any Python-side work happens.
                return self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt >= self.busy_retries:
                    raise
                attempt += 1
                time.sleep(self.retry_delay_s * attempt)

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.query(f'PRAGMA table_info("{table}")')}

    def discover_schema(self) -> None:
        events = self._columns(_EVENTS_TABLE)
        missing = [c for c in _EVENTS_REQUIRED if c not in events]
        self.payloads = tuple(c for c in _EVENTS_PAYLOADS if c in events)
        if missing or not self.payloads:
            if not events:
                tables = sorted(
                    row[0]
                    for row in self.query(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                )
                shown = ", ".join(tables[:12]) or "none"
                raise _SchemaUnknown(
                    f"no {_EVENTS_TABLE} table (tables present: {shown})"
                )
            if not self.payloads:
                missing.append(" or ".join(_EVENTS_PAYLOADS))
            raise _SchemaUnknown(
                f"{_EVENTS_TABLE} is missing column(s): {', '.join(missing)}"
            )
        # Session metadata is optional; take whatever allow-listed columns
        # this release happens to have.
        for table, key in _SESSION_KEYS.items():
            present = self._columns(table)
            if key not in present:
                continue
            wanted = _SESSION_COLUMNS.get(table, ())
            self.tables[table] = tuple(c for c in wanted if c in present)
        meta = self._columns("schema_meta")
        self.tables["schema_meta"] = tuple(
            c for c in _SESSION_COLUMNS["schema_meta"] if c in meta
        )

    def _row(self, table: str, key_value: Any) -> dict[str, Any]:
        columns = self.tables.get(table)
        if not columns or key_value in (None, ""):
            return {}
        rows = self.query(
            f'SELECT {", ".join(columns)} FROM "{table}" '
            f"WHERE {_SESSION_KEYS[table]} = ? LIMIT 1",
            (key_value,),
        )
        return dict(zip(columns, rows[0])) if rows else {}

    def app_version(self) -> Optional[str]:
        if not self.tables.get("schema_meta"):
            return None
        rows = self.query(
            "SELECT app_version FROM schema_meta "
            "WHERE app_version IS NOT NULL LIMIT 1"
        )
        return str(rows[0][0]) if rows else None

    def session_exists(self, session_id: str) -> bool:
        if "session_windows" not in self.tables:
            return False
        return bool(
            self.query(
                "SELECT 1 FROM session_windows WHERE session_id = ? LIMIT 1",
                (session_id,),
            )
        )

    def session_metadata(self, session_id: str) -> dict[str, Any]:
        window = self._row("session_windows", session_id)
        node = self._row("session_nodes", window.get("session_key"))
        conversation_id = window.get("primary_conversation_id")
        links = self.tables.get("session_conversations", ())
        if not conversation_id and "conversation_id" in links:
            order = "ORDER BY role = 'primary' DESC " if "role" in links else ""
            rows = self.query(
                "SELECT conversation_id FROM session_conversations "
                f"WHERE session_id = ? {order}LIMIT 1",
                (session_id,),
            )
            conversation_id = rows[0][0] if rows else None
        conversation = self._row("conversations", conversation_id)
        return {
            "session_key": window.get("session_key"),
            "channel": window.get("channel") or conversation.get("channel"),
            "chat_type": window.get("chat_type") or conversation.get("kind"),
            "model_provider": window.get("model_provider"),
            "model": window.get("model"),
            "parent_session_key": window.get("parent_session_key")
            or node.get("parent_session_key"),
            "spawned_by": window.get("spawned_by") or node.get("spawned_by"),
            "display_name": window.get("display_name") or node.get("display_name"),
            "label": node.get("label"),
            "created_via": node.get("created_via"),
        }

    def next_session(self, after: str) -> Optional[str]:
        # MIN() over the primary-key index is a single seek, so walking the
        # sessions never scans the transcript rows themselves.
        rows = self.query(
            f"SELECT MIN(session_id) FROM {_EVENTS_TABLE} WHERE session_id > ?",
            (after,),
        )
        value = rows[0][0] if rows else None
        return value if isinstance(value, str) else None

    def max_seq(self, session_id: str) -> Optional[int]:
        rows = self.query(
            f"SELECT MAX(seq) FROM {_EVENTS_TABLE} WHERE session_id = ?",
            (session_id,),
        )
        return rows[0][0] if rows else None

    def events_after(
        self, session_id: str, seq: int, limit: int
    ) -> list[tuple[int, Any, dict[str, Any]]]:
        rows = self.query(
            f"SELECT seq, created_at, {', '.join(self.payloads)} "
            f"FROM {_EVENTS_TABLE} WHERE session_id = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (session_id, seq, limit),
        )
        return [(row[0], row[1], dict(zip(self.payloads, row[2:]))) for row in rows]


def _decode_payload(payload: dict[str, Any]) -> Optional[dict]:
    """Return the event record, or ``None`` when the row is not decodable."""
    text = payload.get("event_json")
    if text is None:
        blob = payload.get("event_zstd")
        if blob is None:
            return None
        if _zstd_decompress is None:
            raise _DecoderUnavailable()
        try:
            text = _zstd_decompress(bytes(blob))
        except Exception:  # noqa: BLE001 - any codec failure means "undecodable"
            return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class OpenClawSqliteSource:
    """Incremental reader over every agent's ``openclaw-agent.sqlite``."""

    state_dir: Path
    agents: tuple[str, ...] = ()
    batch_size: int = 200
    max_events_per_run: int = 5000
    busy_timeout_ms: int = 250
    busy_retries: int = 2
    retry_delay_s: float = 0.2
    id: str = "openclaw_sqlite"

    def databases(self) -> list[tuple[str, Path]]:
        found = discover_agent_databases(self.state_dir)
        if self.agents:
            found = [(agent, path) for agent, path in found if agent in self.agents]
        return found

    def collect(self, cursor: dict) -> IncrementalBatch:
        agents_state = _agents_state(cursor)
        events: list[AgentEvent] = []
        diagnostics: list[str] = []

        databases = self.databases()
        if not databases:
            diagnostics.append(
                f"no OpenClaw agent database matches {AGENT_DB_GLOB} under the "
                "configured state_dir"
            )
        for agent_id, db_path in databases:
            budget = self.max_events_per_run - len(events)
            if budget <= 0:
                break
            sessions = dict(agents_state.get(agent_id, {}).get("sessions", {}))
            self._collect_database(
                agent_id, db_path, sessions, budget, events, diagnostics
            )
            agents_state[agent_id] = {"sessions": sessions}

        return IncrementalBatch(
            events=events,
            cursor={"version": CURSOR_VERSION, "agents": agents_state},
            diagnostics=diagnostics,
        )

    def _collect_database(
        self,
        agent_id: str,
        db_path: Path,
        sessions: dict[str, int],
        budget: int,
        events: list[AgentEvent],
        diagnostics: list[str],
    ) -> None:
        """Advance ``sessions`` in place; whatever was read before a failure stays."""
        label = f"agent {agent_id!r}"
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = connect_read_only(db_path, busy_timeout_ms=self.busy_timeout_ms)
            reader = _Reader(
                conn=conn,
                busy_retries=self.busy_retries,
                retry_delay_s=self.retry_delay_s,
            )
            reader.discover_schema()
            self._read_sessions(agent_id, reader, sessions, budget, events, diagnostics)
        except _SchemaUnknown as exc:
            diagnostics.append(
                f"{label}: unknown OpenClaw schema, skipped ({exc}); this "
                "collector understands the transcript_events store"
            )
        except sqlite3.Error as exc:
            if _is_busy(exc):
                diagnostics.append(
                    f"{label}: database busy after {self.busy_retries} "
                    "retries, skipped until the next run"
                )
            else:
                diagnostics.append(f"{label}: cannot read database, skipped ({exc})")
        finally:
            if conn is not None:
                conn.close()

    def _read_sessions(
        self,
        agent_id: str,
        reader: _Reader,
        sessions: dict[str, int],
        budget: int,
        events: list[AgentEvent],
        diagnostics: list[str],
    ) -> None:
        label = f"agent {agent_id!r}"
        app_version = reader.app_version()
        seen: set[str] = set()
        skipped = 0
        blocked = 0
        rewound = 0
        remaining = budget
        complete = False
        after = ""

        while remaining > 0:
            session_id = reader.next_session(after)
            if session_id is None:
                complete = True
                break
            after = session_id
            seen.add(session_id)

            last_seq = sessions.get(session_id)
            mapper: Optional[OpenClawRecordMapper] = None
            extra: dict[str, Any] = {}
            while remaining > 0:
                limit = min(self.batch_size, remaining)
                rows = reader.events_after(
                    session_id, _MIN_SEQ if last_seq is None else last_seq, limit
                )
                if not rows and last_seq is not None and mapper is None:
                    newest = reader.max_seq(session_id)
                    if newest is not None and newest < last_seq:
                        # The stored transcript now ends before our watermark:
                        # OpenClaw rewrote it. Re-read; ingest dedupes repeats.
                        rewound += 1
                        last_seq = None
                        del sessions[session_id]
                        continue
                if not rows:
                    break
                if mapper is None:
                    mapper, extra = self._session_mapper(
                        agent_id, reader, session_id, app_version, last_seq
                    )
                stalled = False
                for seq, created_at, payload in rows:
                    try:
                        data = _decode_payload(payload)
                    except _DecoderUnavailable:
                        # Leave the watermark before this row so the session
                        # resumes without a gap once a decoder is installed.
                        stalled = True
                        blocked += 1
                        break
                    event = None
                    if data is not None:
                        event = _map_record(
                            mapper, data, agent_id, session_id, seq, created_at, extra
                        )
                        if event is None and data.get("type") != "session":
                            skipped += 1
                    else:
                        skipped += 1
                    last_seq = seq
                    sessions[session_id] = seq
                    if event is not None:
                        events.append(event)
                        remaining -= 1
                if stalled or len(rows) < limit:
                    break

        if complete:
            # Forget sessions OpenClaw itself deleted so the cursor stays bounded.
            for session_id in [s for s in sessions if s not in seen]:
                if not reader.session_exists(session_id):
                    del sessions[session_id]
        if blocked:
            diagnostics.append(
                f"{label}: {blocked} session(s) paused at zstd-compressed events; "
                "run the collector on Python 3.14+ or install the zstandard package"
            )
        if rewound:
            diagnostics.append(
                f"{label}: {rewound} session transcript(s) were rewritten by "
                "OpenClaw and re-read from the start"
            )
        if skipped:
            diagnostics.append(
                f"{label}: skipped {skipped} transcript row(s) that were not "
                "decodable event records"
            )

    def _session_mapper(
        self,
        agent_id: str,
        reader: _Reader,
        session_id: str,
        app_version: Optional[str],
        last_seq: Optional[int],
    ) -> tuple[OpenClawRecordMapper, dict[str, Any]]:
        meta = reader.session_metadata(session_id)
        defaults = {
            "agent_id": agent_id,
            "session_key": meta.get("session_key"),
            "channel": meta.get("channel"),
            "parent_session_key": meta.get("parent_session_key"),
            "topic": meta.get("label"),
            "harness_version": app_version,
        }
        mapper = OpenClawRecordMapper(
            pinned_session_id=session_id,
            session_defaults={k: v for k, v in defaults.items() if v not in (None, "")},
        )
        if last_seq is not None:
            # Resuming mid-session: replay the header row for cwd and friends.
            head = reader.events_after(session_id, _MIN_SEQ, 1)
            if head:
                try:
                    header = _decode_payload(head[0][2])
                except _DecoderUnavailable:
                    header = None
                if header is not None and header.get("type") == "session":
                    mapper.feed(header)
        extra = {
            key: meta[key]
            for key in (
                "chat_type",
                "model_provider",
                "model",
                "spawned_by",
                "display_name",
                "created_via",
            )
            if meta.get(key) not in (None, "")
        }
        return mapper, extra


def _map_record(
    mapper: OpenClawRecordMapper,
    data: dict,
    agent_id: str,
    session_id: str,
    seq: int,
    created_at: Any,
    extra: dict[str, Any],
) -> Optional[AgentEvent]:
    try:
        event = mapper.feed(
            data,
            fallback_timestamp=_epoch_datetime(created_at) or datetime.now(UTC),
            fallback_id=f"openclaw:{agent_id}:{session_id}:{seq}",
        )
    except Exception as exc:  # noqa: BLE001 - one bad record must not stall a session
        log.debug("unmappable OpenClaw record %s/%s: %s", session_id, seq, exc)
        return None
    if event is not None:
        event.raw_data["openclaw_store"] = {"kind": "sqlite", "seq": seq, **extra}
    return event


def _agents_state(cursor: dict) -> dict[str, dict[str, dict[str, int]]]:
    """Copy the per-agent watermarks out of a stored cursor, dropping junk."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    agents = cursor.get("agents") if isinstance(cursor, dict) else None
    if not isinstance(agents, dict):
        return out
    for agent_id, state in agents.items():
        sessions = state.get("sessions") if isinstance(state, dict) else None
        if not isinstance(sessions, dict):
            continue
        out[str(agent_id)] = {
            "sessions": {
                str(session_id): seq
                for session_id, seq in sessions.items()
                if isinstance(seq, int) and not isinstance(seq, bool)
            }
        }
    return out
