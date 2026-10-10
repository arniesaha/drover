"""Per-host event sources: file selection + parsing.

Each source delegates parsing to the existing ``drover.parsers`` functions
and just iterates over file selection (``list_files_since`` semantics).
A source's id is the key consumers use as the cursor key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Protocol, runtime_checkable

from drover import parsers
from drover.attribution import enrich_raw_repo_attribution
from drover.models import AgentEvent


@runtime_checkable
class Source(Protocol):
    id: str

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]: ...
    def parse(self, path: Path) -> Iterator[AgentEvent]: ...


@dataclass
class IncrementalBatch:
    """One keyed read from a store that is not file/mtime shaped.

    ``cursor`` is the source's complete resume state. It only becomes durable
    once the caller has staged and shipped ``events``.
    """

    events: list[AgentEvent]
    cursor: dict
    diagnostics: list[str]


@runtime_checkable
class IncrementalSource(Protocol):
    """Source which resumes from its own stable key instead of a timestamp."""

    id: str

    def collect(self, cursor: dict) -> IncrementalBatch: ...


def _files_modified_after(
    root: Path, pattern: str, watermark: Optional[datetime]
) -> list[Path]:
    if not root.exists():
        return []
    cutoff = watermark.timestamp() if watermark else 0.0
    out: list[Path] = []
    for p in root.rglob(pattern):
        try:
            if p.is_file() and p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue
    return sorted(out)


@dataclass(frozen=True)
class ClaudeCodeSource:
    """Standard Claude Code project sessions in ``~/.claude/projects``.

    ``agent_id`` defaults to the host_id from collect.toml, threaded through
    by the CLI so each installation gets a portable, operator-owned identity.
    """

    root: Path
    agent_id: str = "claude_code"
    id: str = "claude_code"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        return _files_modified_after(self.root, "*.jsonl", watermark)

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_claude_audit_log(str(path), agent_id=self.agent_id)


@dataclass(frozen=True)
class ClaudeMacMiniSource:
    """Claude Code's local-agent-mode-sessions variant on macOS."""

    root: Path
    agent_id: str = "claude-code"
    id: str = "claude_macmini"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        return _files_modified_after(self.root, "*.jsonl", watermark)

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_claude_audit_log(str(path), agent_id=self.agent_id)


@dataclass(frozen=True)
class HermesSource:
    root: Path
    id: str = "hermes"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        return _files_modified_after(self.root, "*.json", watermark)

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_hermes_sessions(str(path))


@dataclass(frozen=True)
class OpenClawSource:
    root: Path
    id: str = "openclaw"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        return _files_modified_after(self.root, "*.jsonl", watermark)

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_openclaw_sessions(str(path))


@dataclass(frozen=True)
class PiMonoSource:
    """SQLite single-file source. Cursor watermarks the file mtime."""

    db_path: Path
    id: str = "pi_mono"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        if not self.db_path.exists():
            return []
        if watermark is None:
            return [self.db_path]
        try:
            mtime = self.db_path.stat().st_mtime
        except OSError:
            return []
        return [self.db_path] if mtime >= watermark.timestamp() else []

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_task_journal(str(path))


@dataclass(frozen=True)
class OpenClawTaskFlowSource:
    """Read-only Capability Factory provenance from OpenClaw managed TaskFlow."""

    db_path: Path
    controller_id: str = "capability-factory/taskflow-controller"
    id: str = "openclaw_taskflow"

    def list_files_since(self, watermark: Optional[datetime]) -> list[Path]:
        if not self.db_path.exists():
            return []
        if watermark is None:
            return [self.db_path]
        cutoff = watermark.timestamp()
        try:
            if self.db_path.stat().st_mtime >= cutoff:
                return [self.db_path]
        except OSError:
            return []

        # SQLite commonly commits live TaskFlow updates to the WAL before the
        # main database checkpoint changes its mtime. The parser still opens
        # only the database read-only; this is solely a collection trigger.
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        try:
            with wal_path.open("rb"):
                pass
            if wal_path.stat().st_mtime >= cutoff:
                return [self.db_path]
        except OSError:
            pass
        return []

    def parse(self, path: Path) -> Iterator[AgentEvent]:
        yield from parsers.parse_openclaw_taskflow(
            str(path), controller_id=self.controller_id
        )


def write_events_jsonl(
    events: Iterable[AgentEvent],
    staging_dir: Path,
    *,
    run_id: str,
    source_id: str,
) -> Optional[Path]:
    """Write events to ``<staging>/<source>-<run_id>.jsonl`` atomically.

    Writes go to ``.jsonl.tmp``, fsync, then ``os.replace`` to ``.jsonl``.
    Returns the final path, or ``None`` if there were no events to write.
    """
    materialized = list(events)
    if not materialized:
        return None
    for event in materialized:
        event.raw_data = enrich_raw_repo_attribution(event.raw_data)
    staging_dir.mkdir(parents=True, exist_ok=True)
    final = staging_dir / f"{source_id}-{run_id}.jsonl"
    tmp = final.with_suffix(final.suffix + ".tmp")
    with open(tmp, "w") as f:
        for event in materialized:
            f.write(event.model_dump_json(exclude_none=True))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def latest_event_timestamp(events: Iterable[AgentEvent]) -> Optional[datetime]:
    """Return the max timestamp across events; None if iterable is empty."""
    latest: Optional[datetime] = None
    for e in events:
        ts = e.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if latest is None or ts > latest:
            latest = ts
    return latest
