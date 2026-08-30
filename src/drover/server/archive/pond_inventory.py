"""Export a bounded Pond session inventory through the pinned local CLI."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Mapping

from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    MAX_INVENTORY_RECORDS,
    PondInventory,
    PondInventoryRecord,
    _write_private_json_at,
)
from drover.server.archive.pond_process import (
    POND_VERSION,
    PondProcessError,
    is_pinned_pond_version,
    require_pinned_pond,
    run_pond_process,
)

POND_INVENTORY_SQL = """SELECT s.session_id, s.source_agent, s.created_at,
       count(m.message_id) AS message_count,
       min(m.timestamp) AS first_message_at,
       max(m.timestamp) AS last_message_at
FROM sessions s
LEFT JOIN messages m ON m.session_id = s.session_id
WHERE s.source_agent IN ('claude-code', 'codex-cli')
GROUP BY s.session_id, s.source_agent, s.created_at
ORDER BY s.source_agent, s.session_id
LIMIT 100001"""
# The empty six-column NDJSON object plus newline is exactly 117 bytes. For
# every value byte, six bytes is the JSON worst case (a ``\u00xx`` escape).
# Optional timestamp strings are added without subtracting their null
# placeholders, and the count length is added without subtracting its one-digit
# placeholder, so the aggregate deliberately overestimates the main export.
POND_INVENTORY_PREFLIGHT_SQL = """WITH inventory AS (
    SELECT s.session_id, s.source_agent, s.created_at,
           count(m.message_id) AS message_count,
           min(m.timestamp) AS first_message_at,
           max(m.timestamp) AS last_message_at
    FROM sessions s
    LEFT JOIN messages m ON m.session_id = s.session_id
    WHERE s.source_agent IN ('claude-code', 'codex-cli')
    GROUP BY s.session_id, s.source_agent, s.created_at
    ORDER BY s.source_agent, s.session_id
    LIMIT 100001
)
SELECT count(*) AS row_count,
       coalesce(sum(
           117
           + 6 * coalesce(octet_length(session_id), 4)
           + 6 * coalesce(octet_length(source_agent), 4)
           + 6 * coalesce(octet_length(CAST(created_at AS VARCHAR)), 4)
           + octet_length(CAST(message_count AS VARCHAR))
           + CASE WHEN first_message_at IS NULL THEN 0
                  ELSE 2 + 6 * octet_length(CAST(first_message_at AS VARCHAR)) END
           + CASE WHEN last_message_at IS NULL THEN 0
                  ELSE 2 + 6 * octet_length(CAST(last_message_at AS VARCHAR)) END
       ), 0) AS worst_case_ndjson_bytes
FROM inventory"""

_POND_COLUMNS = frozenset(
    {
        "session_id",
        "source_agent",
        "created_at",
        "message_count",
        "first_message_at",
        "last_message_at",
    }
)
_POND_EMPTY_ROW_COLUMNS = _POND_COLUMNS - {
    "first_message_at",
    "last_message_at",
}
_ROOT_SOURCE_AGENTS = frozenset({"claude-code", "codex-cli"})
_URI_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*:")
_DATAFUSION_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?" r"(?:Z|[+-]\d{2}:\d{2})\Z"
)
_COPY_CHUNK_BYTES = 64 * 1024


class _PondInventoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _PinnedOutput:
    directory_descriptor: int
    requested_parent: Path
    name: str
    directory_identity: tuple[int, int, int, int]
    local_store: Path


def _failure(category: str) -> _PondInventoryError:
    return _PondInventoryError(f"pond inventory {category}")


def _raise_process_failure(
    error: PondProcessError, *, resource_category: str = "subprocess"
) -> None:
    if error.category == "artifact":
        category = "export"
    elif error.category == "resource":
        category = resource_category
    else:
        category = error.category
    raise _failure(category) from None


def export_pond_inventory(
    binary: Path,
    output: Path,
    *,
    storage_path: Path,
    timeout_seconds: float = 60.0,
    env: Mapping[str, str] | None = None,
) -> PondInventory:
    """Export strict session metadata without exposing Pond rows or locations."""
    try:
        executable = require_pinned_pond(binary)
    except PondProcessError as error:
        _raise_process_failure(error)
    local_store = _require_local_storage(storage_path)
    pinned_output = _pin_external_output(output, local_store)
    try:
        timeout = _require_timeout(timeout_seconds)
        return _export_to_pinned_output(
            executable,
            pinned_output,
            local_store=local_store,
            timeout=timeout,
            env=env,
        )
    finally:
        try:
            os.close(pinned_output.directory_descriptor)
        except OSError:
            pass


def _export_to_pinned_output(
    executable: Path,
    pinned_output: _PinnedOutput,
    *,
    local_store: Path,
    timeout: int,
    env: Mapping[str, str] | None,
) -> PondInventory:
    """Run the bounded export while retaining the validated output directory."""

    try:
        temporary = tempfile.TemporaryDirectory(prefix="drover-pond-inventory-")
    except OSError:
        raise _failure("temporary") from None
    with temporary as temporary_name:
        temporary_path = Path(temporary_name)
        snapshot_path = temporary_path / "pond-store"
        _snapshot_store(local_store, snapshot_path)
        try:
            version_result = run_pond_process(
                executable,
                ("--version",),
                timeout_seconds=10.0,
                run_directory=temporary_path,
                label="version",
                env=env,
            )
        except PondProcessError as error:
            _raise_process_failure(error, resource_category="version")
        if version_result.returncode != 0:
            raise _failure("version")
        version_bytes = _read_private_bytes(version_result.stdout_path, "version")
        try:
            version_tokens = tuple(version_bytes.decode("utf-8").split())
        except UnicodeDecodeError:
            raise _failure("version") from None
        if not is_pinned_pond_version(version_tokens):
            raise _failure("version")

        preflight_path = temporary_path / "preflight.ndjson"
        preflight_arguments = (
            "--storage-path",
            str(snapshot_path),
            "sql",
            POND_INVENTORY_PREFLIGHT_SQL,
            "--format",
            "ndjson",
            "--output-file",
            str(preflight_path),
            "--timeout",
            str(timeout),
        )
        try:
            preflight_result = run_pond_process(
                executable,
                preflight_arguments,
                timeout_seconds=float(timeout),
                run_directory=temporary_path,
                label="preflight",
                env=env,
                artifact_path=preflight_path,
            )
        except PondProcessError as error:
            _raise_process_failure(error, resource_category="preflight")
        if preflight_result.returncode != 0:
            raise _failure("preflight")
        _read_preflight(preflight_path)

        export_path = temporary_path / "inventory.ndjson"
        arguments = (
            "--storage-path",
            str(snapshot_path),
            "sql",
            POND_INVENTORY_SQL,
            "--format",
            "ndjson",
            "--output-file",
            str(export_path),
            "--timeout",
            str(timeout),
        )
        try:
            result = run_pond_process(
                executable,
                arguments,
                timeout_seconds=float(timeout),
                run_directory=temporary_path,
                label="inventory",
                env=env,
                artifact_path=export_path,
            )
        except PondProcessError as error:
            _raise_process_failure(error, resource_category="subprocess")
        if result.returncode != 0:
            raise _failure("subprocess")
        records = _read_pond_rows(export_path)
        inventory = PondInventory(
            schema_version=1,
            captured_at=_canonical_timestamp(datetime.now(timezone.utc).isoformat()),
            pond_version=POND_VERSION,
            records=tuple(
                sorted(records, key=lambda row: (row.source_agent, row.session_id))
            ),
        )
        _validate_pinned_output_parent(pinned_output)
        _write_private_json_at(
            pinned_output.directory_descriptor,
            pinned_output.name,
            inventory.to_wire(),
        )
        return inventory


def pond_inventory_summary(inventory: PondInventory) -> dict[str, object]:
    """Return aggregate-only Pond inventory metadata for operator output."""
    try:
        inventory.to_wire()
    except (AttributeError, TypeError, ValueError):
        raise _failure("summary") from None
    if inventory.pond_version != POND_VERSION:
        raise _failure("summary")
    by_harness: dict[str, int] = {}
    empty_sessions = 0
    for record in inventory.records:
        if record.source_agent not in _ROOT_SOURCE_AGENTS:
            raise _failure("summary")
        by_harness[record.source_agent] = by_harness.get(record.source_agent, 0) + 1
        empty_sessions += record.message_count == 0
    return {
        "schema_version": inventory.schema_version,
        "pond_version": inventory.pond_version,
        "archive_sessions": len(inventory.records),
        "empty_sessions": empty_sessions,
        "by_harness": dict(sorted(by_harness.items())),
    }


def _require_local_storage(storage_path: Path) -> Path:
    try:
        raw = os.fspath(storage_path)
        if not isinstance(raw, str) or _URI_SCHEME.match(raw):
            raise _failure("storage")
        path = Path(raw)
        if not path.is_absolute():
            raise _failure("storage")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _failure("storage")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise _failure("storage")
        return resolved
    except _PondInventoryError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure("storage") from None


def _pin_external_output(output: Path, local_store: Path) -> _PinnedOutput:
    descriptor: int | None = None
    try:
        candidate = Path(output)
        name = candidate.name
        if not name or name in {".", ".."} or os.path.basename(name) != name:
            raise _failure("output")
        try:
            output_metadata = candidate.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(output_metadata.st_mode):
                raise _failure("output")
        requested_parent = Path(os.path.abspath(os.fspath(candidate.parent)))
        resolved_parent = requested_parent.resolve(strict=True)
        resolved = resolved_parent / name
        try:
            resolved.relative_to(local_store)
        except ValueError:
            pass
        else:
            raise _failure("output")
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved_parent, flags)
        opened = os.fstat(descriptor)
        requested = requested_parent.stat()
        opened_identity = _directory_identity(opened)
        if opened_identity is None or _directory_identity(requested) != opened_identity:
            raise _failure("output")
        pinned = _PinnedOutput(
            directory_descriptor=descriptor,
            requested_parent=requested_parent,
            name=name,
            directory_identity=opened_identity,
            local_store=local_store,
        )
        descriptor = None
        return pinned
    except _PondInventoryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _failure("output") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int] | None:
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_ctime_ns,
    )


def _validate_pinned_output_parent(pinned: _PinnedOutput) -> None:
    try:
        resolved_parent = pinned.requested_parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(pinned.local_store)
        except ValueError:
            pass
        else:
            raise _failure("output")
        opened = os.fstat(pinned.directory_descriptor)
        requested = pinned.requested_parent.stat()
    except _PondInventoryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _failure("output") from None
    if (
        _directory_identity(opened) != pinned.directory_identity
        or _directory_identity(requested) != pinned.directory_identity
    ):
        raise _failure("output")


def _snapshot_store(source: Path, target: Path) -> None:
    source_descriptor: int | None = None
    try:
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_descriptor = os.open(source, flags)
        opened = os.fstat(source_descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise _failure("snapshot")
        if _snapshot_signature(source.lstat()) != _snapshot_signature(opened):
            raise _failure("snapshot")
        signatures: dict[tuple[str, ...], tuple[int, ...]] = {}
        directory_names: dict[tuple[str, ...], tuple[str, ...]] = {}
        _snapshot_directory(
            source_descriptor,
            target,
            (),
            signatures,
            directory_names,
        )
        _validate_snapshot_tree(
            source_descriptor,
            (),
            signatures,
            directory_names,
        )
        if _snapshot_signature(source.lstat()) != _snapshot_signature(opened):
            raise _failure("snapshot")
    except _PondInventoryError:
        raise _failure("snapshot") from None
    except (OSError, TypeError, ValueError):
        raise _failure("snapshot") from None
    finally:
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass


def _snapshot_directory(
    source_descriptor: int,
    target: Path,
    relative: tuple[str, ...],
    signatures: dict[tuple[str, ...], tuple[int, ...]],
    directory_names: dict[tuple[str, ...], tuple[str, ...]],
) -> None:
    before = os.fstat(source_descriptor)
    names_before = _snapshot_names(source_descriptor)
    signatures[relative] = _snapshot_signature(before)
    directory_names[relative] = names_before
    for name in names_before:
        entry_before = os.stat(
            name,
            dir_fd=source_descriptor,
            follow_symlinks=False,
        )
        destination = target / name
        child_relative = (*relative, name)
        if stat.S_ISREG(entry_before.st_mode):
            signatures[child_relative] = _snapshot_signature(entry_before)
            _snapshot_regular_file(
                source_descriptor,
                name,
                entry_before,
                destination,
            )
        elif stat.S_ISDIR(entry_before.st_mode):
            destination.mkdir(mode=0o700)
            destination.chmod(0o700)
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            child_descriptor = os.open(name, flags, dir_fd=source_descriptor)
            try:
                if _snapshot_signature(
                    os.fstat(child_descriptor)
                ) != _snapshot_signature(entry_before):
                    raise _failure("snapshot")
                _snapshot_directory(
                    child_descriptor,
                    destination,
                    child_relative,
                    signatures,
                    directory_names,
                )
            finally:
                os.close(child_descriptor)
        else:
            raise _failure("snapshot")
        entry_after = os.stat(
            name,
            dir_fd=source_descriptor,
            follow_symlinks=False,
        )
        if _snapshot_signature(entry_after) != _snapshot_signature(entry_before):
            raise _failure("snapshot")
    if names_before != _snapshot_names(source_descriptor):
        raise _failure("snapshot")
    if _snapshot_signature(os.fstat(source_descriptor)) != _snapshot_signature(before):
        raise _failure("snapshot")


def _validate_snapshot_tree(
    source_descriptor: int,
    relative: tuple[str, ...],
    signatures: Mapping[tuple[str, ...], tuple[int, ...]],
    directory_names: Mapping[tuple[str, ...], tuple[str, ...]],
) -> None:
    if _snapshot_signature(os.fstat(source_descriptor)) != signatures[relative]:
        raise _failure("snapshot")
    names = _snapshot_names(source_descriptor)
    if names != directory_names[relative]:
        raise _failure("snapshot")
    for name in names:
        child_relative = (*relative, name)
        metadata = os.stat(
            name,
            dir_fd=source_descriptor,
            follow_symlinks=False,
        )
        if _snapshot_signature(metadata) != signatures[child_relative]:
            raise _failure("snapshot")
        if stat.S_ISDIR(metadata.st_mode):
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            child_descriptor = os.open(name, flags, dir_fd=source_descriptor)
            try:
                _validate_snapshot_tree(
                    child_descriptor,
                    child_relative,
                    signatures,
                    directory_names,
                )
            finally:
                os.close(child_descriptor)


def _snapshot_regular_file(
    source_directory: int,
    name: str,
    expected: os.stat_result,
    destination: Path,
) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    source_descriptor = os.open(name, flags, dir_fd=source_directory)
    try:
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or _snapshot_signature(
            opened
        ) != _snapshot_signature(expected):
            raise _failure("snapshot")
        copied = 0
        with _open_private_output(destination) as output:
            while chunk := os.read(source_descriptor, _COPY_CHUNK_BYTES):
                output.write(chunk)
                copied += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_descriptor)
        if copied != expected.st_size or _snapshot_signature(
            after
        ) != _snapshot_signature(expected):
            raise _failure("snapshot")
        destination_metadata = destination.lstat()
        if (
            not stat.S_ISREG(destination_metadata.st_mode)
            or destination_metadata.st_mode & 0o077
            or destination_metadata.st_size != copied
        ):
            raise _failure("snapshot")
    finally:
        os.close(source_descriptor)


def _snapshot_names(source_descriptor: int) -> tuple[str, ...]:
    with os.scandir(source_descriptor) as entries:
        return tuple(sorted(entry.name for entry in entries))


def _snapshot_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_timeout(timeout_seconds: float) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds != int(timeout_seconds)
        or not 5 <= timeout_seconds <= 600
    ):
        raise _failure("timeout")
    return int(timeout_seconds)


def _open_private_output(path: Path) -> BinaryIO:
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb", buffering=0)
    except OSError:
        raise _failure("temporary") from None


def _read_private_bytes(path: Path, category: str) -> bytes:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except OSError:
        raise _failure(category) from None
    try:
        with os.fdopen(descriptor, "rb") as input_file:
            metadata = os.fstat(input_file.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_size > MAX_INVENTORY_BYTES
            ):
                raise _failure(category)
            data = input_file.read(MAX_INVENTORY_BYTES + 1)
    except _PondInventoryError:
        raise
    except OSError:
        raise _failure(category) from None
    if len(data) > MAX_INVENTORY_BYTES:
        raise _failure("size")
    return data


def _read_preflight(path: Path) -> tuple[int, int]:
    data = _read_private_bytes(path, "preflight")
    lines = data.splitlines()
    if len(lines) != 1:
        raise _failure("preflight")
    try:
        row = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _failure("preflight") from None
    if not isinstance(row, dict) or set(row) != {
        "row_count",
        "worst_case_ndjson_bytes",
    }:
        raise _failure("preflight")
    row_count = row["row_count"]
    worst_case_bytes = row["worst_case_ndjson_bytes"]
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or isinstance(worst_case_bytes, bool)
        or not isinstance(worst_case_bytes, int)
        or worst_case_bytes < 0
        or row_count > MAX_INVENTORY_RECORDS
        or worst_case_bytes > MAX_INVENTORY_BYTES
    ):
        raise _failure("preflight")
    return row_count, worst_case_bytes


def _read_pond_rows(path: Path) -> tuple[PondInventoryRecord, ...]:
    data = _read_private_bytes(path, "export")
    records: list[PondInventoryRecord] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(data.splitlines(), 1):
        if line_number > MAX_INVENTORY_RECORDS:
            raise _failure("rows")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _failure("row") from None
        if not isinstance(row, dict):
            raise _failure("row")
        columns = set(row)
        if columns == _POND_EMPTY_ROW_COLUMNS and row.get("message_count") == 0:
            row = {**row, "first_message_at": None, "last_message_at": None}
        elif columns != _POND_COLUMNS:
            raise _failure("columns")
        record = _pond_record(row)
        key = (record.source_agent, record.session_id)
        if key in seen:
            raise _failure("duplicate")
        seen.add(key)
        records.append(record)
    return tuple(records)


def _pond_record(row: dict[str, object]) -> PondInventoryRecord:
    session_id = row["session_id"]
    source_agent = row["source_agent"]
    message_count = row["message_count"]
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or source_agent not in _ROOT_SOURCE_AGENTS
        or isinstance(message_count, bool)
        or not isinstance(message_count, int)
        or message_count < 0
    ):
        raise _failure("row")
    created_at = _row_timestamp(row["created_at"])
    first_value = row["first_message_at"]
    last_value = row["last_message_at"]
    if message_count == 0:
        if first_value is not None or last_value is not None:
            raise _failure("row")
        first_message_at = None
        last_message_at = None
    else:
        first_instant = _parse_timestamp(first_value)
        last_instant = _parse_timestamp(last_value)
        if first_instant > last_instant:
            raise _failure("row")
        first_message_at = _render_timestamp(first_instant)
        last_message_at = _render_timestamp(last_instant)
    return PondInventoryRecord(
        session_id=session_id,
        source_agent=source_agent,
        created_at=created_at,
        message_count=message_count,
        first_message_at=first_message_at,
        last_message_at=last_message_at,
    )


def _row_timestamp(value: object) -> str:
    return _render_timestamp(_parse_timestamp(value))


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise _failure("row")
    try:
        if not _DATAFUSION_TIMESTAMP.fullmatch(value):
            raise ValueError
        parsed_value = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(parsed_value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise _failure("row") from None


def _canonical_timestamp(value: str) -> str:
    return _render_timestamp(_parse_timestamp(value))


def _render_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
