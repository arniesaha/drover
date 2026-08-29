"""Capture a bounded, local-only denominator of native session sources."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from drover.server.archive.inventory import NativeInventory, NativeInventoryRecord
from drover.server.harness.daemon import discover_native_history_metadata

MAX_NATIVE_INVENTORY_RECORDS = 100_000
_SUMMARY_SOURCE_AGENTS = frozenset({"claude-code", "codex-cli"})


def discover_native_history_inventory(
    home: Path,
    host_id: str,
    *,
    captured_at: datetime | None = None,
    max_records: int = MAX_NATIVE_INVENTORY_RECORDS,
) -> NativeInventory:
    """Capture supported local native source metadata without source paths."""
    normalized_host_id = _required_text(host_id, "host_id")
    captured = _timestamp(captured_at or datetime.now(timezone.utc), "captured_at")
    grouped: dict[tuple[str, str], NativeInventoryRecord] = {}
    for source in discover_native_history_metadata(home, max_records=max_records):
        source_agent = _normalize_harness(source["harness"])
        if source_agent is None:
            continue
        session_id = _required_text(source["session_id"], "session_id")
        key = (source_agent, session_id)
        current = grouped.get(key)
        if current is None:
            grouped[key] = NativeInventoryRecord(
                source_agent=source_agent,
                session_id=session_id,
                updated_at=str(source["updated_at"]),
                size_bytes=int(source["size_bytes"]),
                source_copies=1,
            )
            continue
        grouped[key] = NativeInventoryRecord(
            source_agent=source_agent,
            session_id=session_id,
            updated_at=max(current.updated_at, str(source["updated_at"])),
            size_bytes=current.size_bytes + int(source["size_bytes"]),
            source_copies=current.source_copies + 1,
        )
    return NativeInventory(
        schema_version=1,
        captured_at=captured,
        host_id=normalized_host_id,
        records=tuple(
            sorted(grouped.values(), key=lambda row: (row.source_agent, row.session_id))
        ),
    )


def native_inventory_summary(inventory: NativeInventory) -> dict[str, object]:
    """Return aggregate-only inventory counts suitable for operator output."""
    by_harness: dict[str, int] = {}
    for record in inventory.records:
        if record.source_agent not in _SUMMARY_SOURCE_AGENTS:
            raise ValueError("native inventory invalid source_agent")
        by_harness[record.source_agent] = by_harness.get(record.source_agent, 0) + 1
    return {
        "schema_version": 1,
        "captured_sessions": len(inventory.records),
        "source_copies": sum(record.source_copies for record in inventory.records),
        "duplicate_source_groups": sum(
            record.source_copies > 1 for record in inventory.records
        ),
        "by_harness": dict(sorted(by_harness.items())),
    }


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"native inventory invalid {field}")
    return text


def _normalize_harness(harness: object) -> str | None:
    if harness == "claude-code":
        return "claude-code"
    if harness == "codex":
        return "codex-cli"
    return None


def _timestamp(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"native inventory invalid {field}")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
