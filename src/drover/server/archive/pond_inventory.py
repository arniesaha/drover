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

        export_path = temporary_path / "inventory.ndjson"
        sql_stdout = temporary_path / "sql.stdout"
        sql_stderr = temporary_path / "sql.stderr"
        command = [
            str(executable),
            "--storage-path",
            str(local_store),
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
        path = Path(binary)
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
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise _failure("storage")
        return resolved
    except _PondInventoryError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure("storage") from None


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
            return process.wait(timeout=0)
        except _PondInventoryError:
            _stop_process(process)
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            _stop_process(process)
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


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _signal_process_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
        return
    except (OSError, AttributeError):
        pass
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
