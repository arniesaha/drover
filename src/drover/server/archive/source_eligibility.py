"""Bounded classification of metadata-only native source files."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drover.native_history_identity import native_source_fingerprint
from drover.server.archive.inventory import SourceEligibilityReceipt

MAX_ELIGIBILITY_SOURCE_BYTES = 4 * 1024
_ALLOWED_EVENT_TYPES = frozenset({"ai-title", "agent-name"})
_FORBIDDEN_CONTENT_KEYS = frozenset(
    {"content", "message", "messages", "prompt", "toolUseResult"}
)


def assess_metadata_only_source(
    home: Path,
    source_path: Path,
    host_id: str,
    *,
    assessed_at: datetime | None = None,
) -> SourceEligibilityReceipt:
    """Classify one complete canonical Claude source without retaining content."""
    normalized_host_id = _required_text(host_id, "host_id")
    try:
        source_lstat = source_path.lstat()
        if stat.S_ISLNK(source_lstat.st_mode):
            raise OSError
        source = source_path.resolve(strict=True)
        projects_root = (home / ".claude/projects").resolve(strict=True)
        relative = source.relative_to(projects_root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("source eligibility invalid source") from None
    if len(relative.parts) != 2 or source.suffix != ".jsonl" or not source.stem:
        raise ValueError("source eligibility invalid source")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError:
        raise ValueError("source eligibility input failed") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_size <= 0
                or initial.st_size > MAX_ELIGIBILITY_SOURCE_BYTES
            ):
                raise ValueError("source eligibility input rejected")
            data = stream.read(initial.st_size)
            final = os.fstat(stream.fileno())
        path_metadata = source.stat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("source eligibility input failed") from None
    if (
        len(data) != initial.st_size
        or not _same_snapshot(initial, final)
        or not _same_snapshot(final, path_metadata)
    ):
        raise ValueError("source eligibility source changed")
    _require_metadata_only_jsonl(data, source.stem)
    return SourceEligibilityReceipt(
        schema_version=1,
        assessed_at=_timestamp(assessed_at or datetime.now(timezone.utc)),
        host_id=normalized_host_id,
        source_agent="claude-code",
        session_id=source.stem,
        source_fingerprint=native_source_fingerprint(final),
        classification="source_not_archive_eligible",
    )


def source_eligibility_summary(
    receipt: SourceEligibilityReceipt,
) -> dict[str, object]:
    """Return only fixed aggregate receipt information for stdout."""
    receipt.to_wire()
    return {
        "schema_version": 1,
        "receipts": 1,
        "source_not_archive_eligible": 1,
    }


def _require_metadata_only_jsonl(data: bytes, session_id: str) -> None:
    try:
        text = data.decode("utf-8")
        lines = text.splitlines()
        if not lines:
            raise ValueError
        for line in lines:
            value = json.loads(line)
            if (
                not isinstance(value, dict)
                or value.get("type") not in _ALLOWED_EVENT_TYPES
                or _contains_forbidden_content(value)
            ):
                raise ValueError
            declared_session = value.get("sessionId")
            if declared_session is not None and declared_session != session_id:
                raise ValueError
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("source eligibility not metadata only") from None


def _contains_forbidden_content(value: Any) -> bool:
    pending = [value]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 512:
            return True
        if isinstance(current, dict):
            if any(key in _FORBIDDEN_CONTENT_KEYS for key in current):
                return True
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"source eligibility invalid {field}")
    return text


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("source eligibility invalid assessed_at")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
