"""Strict, private inventory manifests used by local archive operators."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_INVENTORY_BYTES = 32 * 1024 * 1024
MAX_INVENTORY_RECORDS = 100_000
_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_NATIVE_ROOT_FIELDS = frozenset(
    {"kind", "schema_version", "captured_at", "host_id", "records"}
)
_NATIVE_RECORD_FIELDS = frozenset(
    {"source_agent", "session_id", "updated_at", "size_bytes", "source_copies"}
)
_POND_ROOT_FIELDS = frozenset(
    {"kind", "schema_version", "captured_at", "pond_version", "records"}
)
_POND_RECORD_FIELDS = frozenset(
    {
        "session_id",
        "source_agent",
        "created_at",
        "message_count",
        "first_message_at",
        "last_message_at",
    }
)


def _error(category: str, field: str) -> ValueError:
    return ValueError(f"inventory {category}: {field}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error("invalid", field)
    return value


def _require_timestamp(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if not _RFC3339.fullmatch(value):
        raise _error("invalid", field)
    try:
        parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
        if datetime.fromisoformat(parsed).tzinfo is None:
            raise ValueError
    except ValueError:
        raise _error("invalid", field) from None
    return value


def _require_nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("invalid", field)
    return value


def _require_exact_fields(
    value: Any, expected: frozenset[str], field: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _error("invalid", field)
    return value


def _require_records(value: Any) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_INVENTORY_RECORDS:
        raise _error("invalid", "records")
    return value


def _require_schema_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise _error("invalid", "schema_version")
    return 1


@dataclass(frozen=True, slots=True)
class NativeInventoryRecord:
    source_agent: str
    session_id: str
    updated_at: str
    size_bytes: int
    source_copies: int


@dataclass(frozen=True, slots=True)
class NativeInventory:
    schema_version: int
    captured_at: str
    host_id: str
    records: tuple[NativeInventoryRecord, ...]

    def to_wire(self) -> dict[str, Any]:
        _validate_native_inventory(self)
        return {
            "kind": "native_source_inventory",
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "host_id": self.host_id,
            "records": [
                {
                    "source_agent": record.source_agent,
                    "session_id": record.session_id,
                    "updated_at": record.updated_at,
                    "size_bytes": record.size_bytes,
                    "source_copies": record.source_copies,
                }
                for record in sorted(
                    self.records,
                    key=lambda record: (record.source_agent, record.session_id),
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class PondInventoryRecord:
    session_id: str
    source_agent: str
    created_at: str
    message_count: int
    first_message_at: str | None
    last_message_at: str | None


@dataclass(frozen=True, slots=True)
class PondInventory:
    schema_version: int
    captured_at: str
    pond_version: str
    records: tuple[PondInventoryRecord, ...]

    def to_wire(self) -> dict[str, Any]:
        _validate_pond_inventory(self)
        return {
            "kind": "pond_session_inventory",
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "pond_version": self.pond_version,
            "records": [
                {
                    "session_id": record.session_id,
                    "source_agent": record.source_agent,
                    "created_at": record.created_at,
                    "message_count": record.message_count,
                    "first_message_at": record.first_message_at,
                    "last_message_at": record.last_message_at,
                }
                for record in sorted(
                    self.records,
                    key=lambda record: (record.source_agent, record.session_id),
                )
            ],
        }


def _validate_native_record(record: NativeInventoryRecord) -> None:
    if type(record) is not NativeInventoryRecord:
        raise _error("invalid", "record")
    _require_string(record.source_agent, "source_agent")
    _require_string(record.session_id, "session_id")
    _require_timestamp(record.updated_at, "updated_at")
    _require_nonnegative_integer(record.size_bytes, "size_bytes")
    _require_nonnegative_integer(record.source_copies, "source_copies")


def _validate_native_inventory(inventory: NativeInventory) -> None:
    if type(inventory) is not NativeInventory or not isinstance(
        inventory.records, tuple
    ):
        raise _error("invalid", "inventory")
    _require_schema_version(inventory.schema_version)
    _require_timestamp(inventory.captured_at, "captured_at")
    _require_string(inventory.host_id, "host_id")
    if len(inventory.records) > MAX_INVENTORY_RECORDS:
        raise _error("invalid", "records")
    seen: set[tuple[str, str]] = set()
    for record in inventory.records:
        _validate_native_record(record)
        key = (record.source_agent, record.session_id)
        if key in seen:
            raise _error("invalid", "records")
        seen.add(key)


def _validate_pond_record(record: PondInventoryRecord) -> None:
    if type(record) is not PondInventoryRecord:
        raise _error("invalid", "record")
    _require_string(record.session_id, "session_id")
    _require_string(record.source_agent, "source_agent")
    _require_timestamp(record.created_at, "created_at")
    message_count = _require_nonnegative_integer(record.message_count, "message_count")
    timestamps = (record.first_message_at, record.last_message_at)
    if message_count == 0:
        if timestamps != (None, None):
            raise _error("invalid", "message_timestamps")
    else:
        if None in timestamps:
            raise _error("invalid", "message_timestamps")
        _require_timestamp(record.first_message_at, "first_message_at")
        _require_timestamp(record.last_message_at, "last_message_at")


def _validate_pond_inventory(inventory: PondInventory) -> None:
    if type(inventory) is not PondInventory or not isinstance(inventory.records, tuple):
        raise _error("invalid", "inventory")
    _require_schema_version(inventory.schema_version)
    _require_timestamp(inventory.captured_at, "captured_at")
    _require_string(inventory.pond_version, "pond_version")
    if len(inventory.records) > MAX_INVENTORY_RECORDS:
        raise _error("invalid", "records")
    seen: set[tuple[str, str]] = set()
    for record in inventory.records:
        _validate_pond_record(record)
        key = (record.source_agent, record.session_id)
        if key in seen:
            raise _error("invalid", "records")
        seen.add(key)


def _native_inventory_from_wire(payload: Any) -> NativeInventory:
    root = _require_exact_fields(payload, _NATIVE_ROOT_FIELDS, "root")
    if root["kind"] != "native_source_inventory":
        raise _error("invalid", "kind")
    records: list[NativeInventoryRecord] = []
    for value in _require_records(root["records"]):
        record = _require_exact_fields(value, _NATIVE_RECORD_FIELDS, "record")
        records.append(
            NativeInventoryRecord(
                source_agent=_require_string(record["source_agent"], "source_agent"),
                session_id=_require_string(record["session_id"], "session_id"),
                updated_at=_require_timestamp(record["updated_at"], "updated_at"),
                size_bytes=_require_nonnegative_integer(
                    record["size_bytes"], "size_bytes"
                ),
                source_copies=_require_nonnegative_integer(
                    record["source_copies"], "source_copies"
                ),
            )
        )
    inventory = NativeInventory(
        schema_version=_require_schema_version(root["schema_version"]),
        captured_at=_require_timestamp(root["captured_at"], "captured_at"),
        host_id=_require_string(root["host_id"], "host_id"),
        records=tuple(records),
    )
    _validate_native_inventory(inventory)
    return NativeInventory(
        inventory.schema_version,
        inventory.captured_at,
        inventory.host_id,
        tuple(
            sorted(
                inventory.records,
                key=lambda record: (record.source_agent, record.session_id),
            )
        ),
    )


def _pond_inventory_from_wire(payload: Any) -> PondInventory:
    root = _require_exact_fields(payload, _POND_ROOT_FIELDS, "root")
    if root["kind"] != "pond_session_inventory":
        raise _error("invalid", "kind")
    records: list[PondInventoryRecord] = []
    for value in _require_records(root["records"]):
        record = _require_exact_fields(value, _POND_RECORD_FIELDS, "record")
        records.append(
            PondInventoryRecord(
                session_id=_require_string(record["session_id"], "session_id"),
                source_agent=_require_string(record["source_agent"], "source_agent"),
                created_at=_require_timestamp(record["created_at"], "created_at"),
                message_count=_require_nonnegative_integer(
                    record["message_count"], "message_count"
                ),
                first_message_at=record["first_message_at"],
                last_message_at=record["last_message_at"],
            )
        )
    inventory = PondInventory(
        schema_version=_require_schema_version(root["schema_version"]),
        captured_at=_require_timestamp(root["captured_at"], "captured_at"),
        pond_version=_require_string(root["pond_version"], "pond_version"),
        records=tuple(records),
    )
    _validate_pond_inventory(inventory)
    return PondInventory(
        inventory.schema_version,
        inventory.captured_at,
        inventory.pond_version,
        tuple(
            sorted(
                inventory.records,
                key=lambda record: (record.source_agent, record.session_id),
            )
        ),
    )


def _encode_private_json(payload: Any) -> bytes:
    try:
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError):
        raise _error("output", "payload") from None
    if len(encoded) > MAX_INVENTORY_BYTES:
        raise _error("output", "size")
    return encoded


def _private_output_flags() -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _write_private_json_descriptor(descriptor: int, encoded: bytes) -> None:
    try:
        output = os.fdopen(descriptor, "wb")
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise _error("output", "write") from None
    try:
        with output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise _error("output", "write") from None


def _write_private_json_at(
    directory_descriptor: int,
    name: str,
    payload: Any,
) -> None:
    """Write one private manifest relative to an already-pinned directory."""
    encoded = _encode_private_json(payload)
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or os.path.basename(name) != name
    ):
        raise _error("output", "file")
    try:
        descriptor = os.open(
            name,
            _private_output_flags(),
            0o600,
            dir_fd=directory_descriptor,
        )
    except (OSError, TypeError, ValueError):
        raise _error("output", "file") from None
    _write_private_json_descriptor(descriptor, encoded)


def write_private_json(path: str | os.PathLike[str], payload: Any) -> None:
    """Write a new JSON file that only its owner can read or modify."""
    encoded = _encode_private_json(payload)
    try:
        target = os.fspath(path)
        descriptor = os.open(target, _private_output_flags(), 0o600)
    except (OSError, TypeError):
        raise _error("output", "file") from None
    _write_private_json_descriptor(descriptor, encoded)


def read_private_json(
    path: str | os.PathLike[str], max_bytes: int = MAX_INVENTORY_BYTES
) -> Any:
    """Read one bounded, owner-only regular JSON file without following links."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise _error("invalid", "max_bytes")
    try:
        target = os.fspath(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags)
    except (OSError, TypeError):
        raise _error("input", "file") from None
    try:
        with os.fdopen(descriptor, "rb") as input_file:
            metadata = os.fstat(input_file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise _error("input", "file")
            if metadata.st_size > max_bytes:
                raise _error("input", "size")
            data = input_file.read(max_bytes + 1)
    except ValueError:
        raise
    except OSError:
        raise _error("input", "content") from None
    if len(data) > max_bytes:
        raise _error("input", "size")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("input", "content") from None


def load_native_inventory(path: str | Path) -> NativeInventory:
    """Load exactly one native-source inventory manifest."""
    return _native_inventory_from_wire(read_private_json(path))


def load_pond_inventory(path: str | Path) -> PondInventory:
    """Load exactly one Pond-session inventory manifest."""
    return _pond_inventory_from_wire(read_private_json(path))
