"""Export a bounded Pond session inventory through the pinned local CLI."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    MAX_INVENTORY_RECORDS,
    PondInventory,
    PondInventoryRecord,
    write_private_json,
)

POND_VERSION = "0.16.3"
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

_POND_VERSION_TOKENS = ("pond", POND_VERSION)
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
_ROOT_SOURCE_AGENTS = frozenset({"claude-code", "codex-cli"})
_URI_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*:")
_DATAFUSION_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?" r"(?:Z|[+-]\d{2}:\d{2})\Z"
)
_CHUNK_BYTES = 64 * 1024
_ARTIFACT_POLL_SECONDS = 0.01
_TERMINATE_GRACE_SECONDS = 0.25
_COPY_CHUNK_BYTES = 64 * 1024


class _PondInventoryError(ValueError):
    pass


def _failure(category: str) -> _PondInventoryError:
    return _PondInventoryError(f"pond inventory {category}")


def export_pond_inventory(
    binary: Path,
    output: Path,
    *,
    storage_path: Path,
    timeout_seconds: float = 60.0,
    env: Mapping[str, str] | None = None,
) -> PondInventory:
    """Export strict session metadata without exposing Pond rows or locations."""
    executable = _require_executable(binary)
    local_store = _require_local_storage(storage_path)
    timeout = _require_timeout(timeout_seconds)
    child_env = _child_environment(env)

    try:
        temporary = tempfile.TemporaryDirectory(prefix="drover-pond-inventory-")
    except OSError:
        raise _failure("temporary") from None
    with temporary as temporary_name:
        temporary_path = Path(temporary_name)
        snapshot_path = temporary_path / "pond-store"
        _snapshot_store(local_store, snapshot_path)
        version_stdout = temporary_path / "version.stdout"
        version_stderr = temporary_path / "version.stderr"
        version_returncode = _run_bounded(
            [str(executable), "--version"],
            timeout_seconds=10.0,
            env=child_env,
            stdout_path=version_stdout,
            stderr_path=version_stderr,
        )
        if version_returncode != 0:
            raise _failure("version")
        version_bytes = _read_private_bytes(version_stdout, "version")
        try:
            version_tokens = tuple(version_bytes.decode("utf-8").split())
        except UnicodeDecodeError:
            raise _failure("version") from None
        if version_tokens != _POND_VERSION_TOKENS:
            raise _failure("version")

        preflight_path = temporary_path / "preflight.ndjson"
        preflight_stdout = temporary_path / "preflight.stdout"
        preflight_stderr = temporary_path / "preflight.stderr"
        preflight_command = [
            str(executable),
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
        ]
        preflight_returncode = _run_bounded(
            preflight_command,
            timeout_seconds=float(timeout),
            env=child_env,
            stdout_path=preflight_stdout,
            stderr_path=preflight_stderr,
            artifact_path=preflight_path,
        )
        if preflight_returncode != 0:
            raise _failure("preflight")
        _read_preflight(preflight_path)

        export_path = temporary_path / "inventory.ndjson"
        sql_stdout = temporary_path / "sql.stdout"
        sql_stderr = temporary_path / "sql.stderr"
        command = [
            str(executable),
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
        ]
        returncode = _run_bounded(
            command,
            timeout_seconds=float(timeout),
            env=child_env,
            stdout_path=sql_stdout,
            stderr_path=sql_stderr,
            artifact_path=export_path,
        )
        if returncode != 0:
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
        write_private_json(output, inventory.to_wire())
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


def _require_executable(binary: Path) -> Path:
    try:
        path = Path(binary).resolve(strict=True)
        metadata = path.stat()
    except (OSError, TypeError, ValueError):
        raise _failure("binary") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise _failure("binary")
    return path


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


def _child_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    try:
        child_env = dict(os.environ)
        if env is not None:
            child_env.update(env)
        child_env.pop("POND_STORAGE_PATH", None)
        return child_env
    except (TypeError, ValueError):
        raise _failure("environment") from None


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


def _run_bounded(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    artifact_path: Path | None = None,
) -> int:
    with (
        _open_private_output(stdout_path) as stdout_file,
        _open_private_output(stderr_path) as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                command,
                env=env,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                umask=0o077,
            )
        except (OSError, TypeError, ValueError):
            raise _failure("subprocess") from None

        assert process.stdout is not None
        assert process.stderr is not None
        process_group = process.pid
        selector: selectors.BaseSelector | None = None
        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, stdout_file)
            selector.register(process.stderr, selectors.EVENT_READ, stderr_file)
            counts = {stdout_file: 0, stderr_file: 0}
            deadline = time.monotonic() + timeout_seconds
            while selector.get_map() or process.poll() is None:
                if artifact_path is not None:
                    _check_artifact_during_run(artifact_path)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _failure("timeout")
                ready = selector.select(min(remaining, _ARTIFACT_POLL_SECONDS))
                for key, _ in ready:
                    sink = key.data
                    capacity = MAX_INVENTORY_BYTES - counts[sink]
                    chunk = os.read(key.fd, min(_CHUNK_BYTES, capacity + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(chunk) > capacity:
                        if capacity:
                            sink.write(chunk[:capacity])
                        raise _failure("size")
                    sink.write(chunk)
                    counts[sink] += len(chunk)
            if artifact_path is not None:
                _check_artifact_during_run(artifact_path)
            returncode = process.wait(timeout=0)
            if _process_group_exists(process_group):
                _stop_process(process, process_group)
            return returncode
        except _PondInventoryError:
            _stop_process(process, process_group)
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            _stop_process(process, process_group)
            raise _failure("subprocess") from None
        finally:
            if selector is not None:
                selector.close()
            process.stdout.close()
            process.stderr.close()


def _check_artifact_during_run(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise _failure("export") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise _failure("export")
    if metadata.st_size > MAX_INVENTORY_BYTES:
        raise _failure("size")


def _stop_process(process: subprocess.Popen[bytes], process_group: int) -> None:
    _signal_process_group(process, process_group, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(process_group):
        _signal_process_group(process, process_group, signal.SIGKILL)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, process_group, signal.SIGKILL)
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except (OSError, AttributeError):
        return False
    return True


def _signal_process_group(
    process: subprocess.Popen[bytes], process_group: int, signal_number: int
) -> None:
    try:
        os.killpg(process_group, signal_number)
    except (OSError, AttributeError):
        if process.poll() is None:
            try:
                process.send_signal(signal_number)
            except OSError:
                pass


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
        if set(row) != _POND_COLUMNS:
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
