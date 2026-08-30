"""Exact Pond corpus snapshots through the one bounded Pond process boundary."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

from drover.server.archive.backup_runtime import BackupRuntimeError
from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    MAX_INVENTORY_RECORDS,
    PondInventory,
    PondInventoryRecord,
    _open_nofollow_path,
    _write_private_json_at,
    private_json_sha256,
)
from drover.server.archive.pond_inventory import _pond_record
from drover.server.archive.pond_process import (
    POND_VERSION,
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
    _aggregate_resource_evidence,
    _process_resource_evidence,
    is_pinned_pond_version,
    run_pond_process,
)

POND_CORPUS_SNAPSHOT_SQL = """WITH signatures AS (
    SELECT s.session_id, s.source_agent, s.created_at,
           count(m.message_id) AS message_count,
           min(m.timestamp) AS first_message_at,
           max(m.timestamp) AS last_message_at
    FROM sessions s
    LEFT JOIN messages m ON m.session_id = s.session_id
    GROUP BY s.session_id, s.source_agent, s.created_at
), root_records AS (
    SELECT session_id, source_agent, created_at, message_count,
           first_message_at, last_message_at
    FROM signatures
    WHERE source_agent IN ('claude-code', 'codex-cli')
    ORDER BY source_agent, session_id
    LIMIT 100001
), duplicate_groups AS (
    SELECT source_agent, created_at, message_count,
           first_message_at, last_message_at,
           count(*) AS session_count
    FROM signatures
    GROUP BY source_agent, created_at, message_count,
             first_message_at, last_message_at
    HAVING count(*) > 1
), aggregate_counts AS (
    SELECT
      (SELECT count(*) FROM sessions) AS sessions,
      (SELECT count(*) FROM messages) AS messages,
      (SELECT count(*) FROM parts) AS parts,
      (SELECT count(*) FROM sessions
       WHERE source_agent IS NULL
          OR (source_agent != 'claude-code'
              AND source_agent NOT LIKE 'claude-code/%'
              AND source_agent != 'codex-cli')) AS disallowed_sessions,
      (SELECT count(*) FROM duplicate_groups) AS logical_duplicate_groups,
      (SELECT coalesce(sum(session_count), 0)
       FROM duplicate_groups) AS sessions_in_logical_duplicate_groups
)
SELECT 'root' AS row_kind,
       session_id, source_agent,
       created_at,
       message_count,
       first_message_at,
       last_message_at,
       CAST(NULL AS BIGINT) AS sessions,
       CAST(NULL AS BIGINT) AS messages,
       CAST(NULL AS BIGINT) AS parts,
       CAST(NULL AS BIGINT) AS disallowed_sessions,
       CAST(NULL AS BIGINT) AS logical_duplicate_groups,
       CAST(NULL AS BIGINT) AS sessions_in_logical_duplicate_groups
FROM root_records
UNION ALL
SELECT 'aggregate' AS row_kind,
       CAST(NULL AS VARCHAR) AS session_id,
       CAST(NULL AS VARCHAR) AS source_agent,
       NULL AS created_at,
       CAST(NULL AS BIGINT) AS message_count,
       NULL AS first_message_at,
       NULL AS last_message_at,
       sessions, messages, parts, disallowed_sessions,
       logical_duplicate_groups, sessions_in_logical_duplicate_groups
FROM aggregate_counts
ORDER BY row_kind, source_agent, session_id"""

_ERROR = "archive backup preflight failed"
_CORPUS_FILENAME = "corpus-snapshot.ndjson"
POND_INVENTORY_FILENAME = "pond-inventory.json"
_ROOT_COLUMNS = frozenset(
    {
        "session_id",
        "source_agent",
        "created_at",
        "message_count",
        "first_message_at",
        "last_message_at",
    }
)
_COUNT_COLUMNS = frozenset({"sessions", "messages", "parts", "disallowed_sessions"})
_DUPLICATE_COLUMNS = frozenset(
    {"logical_duplicate_groups", "sessions_in_logical_duplicate_groups"}
)
_ALL_COUNT_COLUMNS = _COUNT_COLUMNS | _DUPLICATE_COLUMNS
_SNAPSHOT_COLUMNS = _ROOT_COLUMNS | _ALL_COUNT_COLUMNS | {"row_kind"}
_ROOT_AGENTS = frozenset({"claude-code", "codex-cli"})
_R2_AUTHORITY = re.compile(r"\A[a-z0-9][a-z0-9-]*\.r2\.cloudflarestorage\.com\Z")
_MAX_TIMEOUT_SECONDS = 1800


class _SnapshotError(ValueError):
    pass


def _failure() -> _SnapshotError:
    return _SnapshotError(_ERROR)


@dataclass(frozen=True, slots=True, repr=False)
class LocalPondStore:
    path: Path = field(repr=False)

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.path, Path) or not self.path.is_absolute():
                raise _failure()
            resolved = self.path.resolve(strict=True)
            metadata = self.path.lstat()
            if resolved != self.path or not stat.S_ISDIR(metadata.st_mode):
                raise _failure()
        except _SnapshotError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise _failure() from None


@dataclass(frozen=True, slots=True, repr=False)
class RemotePondGeneration:
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            _require_generation_url(self.url)
        except _SnapshotError:
            raise
        except Exception:
            raise _failure() from None


@dataclass(frozen=True, slots=True)
class PondCorpusCounts:
    sessions: int
    messages: int
    parts: int
    disallowed_sessions: int
    logical_duplicate_groups: int
    sessions_in_logical_duplicate_groups: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.sessions,
                self.messages,
                self.parts,
                self.disallowed_sessions,
                self.logical_duplicate_groups,
                self.sessions_in_logical_duplicate_groups,
            )
        ):
            raise _failure()
        if (
            self.disallowed_sessions > self.sessions
            or self.sessions_in_logical_duplicate_groups > self.sessions
            or self.logical_duplicate_groups * 2
            > self.sessions_in_logical_duplicate_groups
            or (self.logical_duplicate_groups == 0)
            != (self.sessions_in_logical_duplicate_groups == 0)
        ):
            raise _failure()

    def to_wire(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "messages": self.messages,
            "parts": self.parts,
            "disallowed_sessions": self.disallowed_sessions,
            "logical_duplicate_groups": self.logical_duplicate_groups,
            "sessions_in_logical_duplicate_groups": (
                self.sessions_in_logical_duplicate_groups
            ),
        }


@dataclass(frozen=True, slots=True, repr=False)
class PondStoreSnapshot:
    root_inventory: PondInventory = field(repr=False)
    counts: PondCorpusCounts = field(repr=False)
    resource_evidence: PondResourceEvidence = field(
        default=PondResourceEvidence(0, None, 0),
        repr=False,
    )

    def __post_init__(self) -> None:
        try:
            if type(self.root_inventory) is not PondInventory:
                raise _failure()
            self.root_inventory.to_wire()
            if self.root_inventory.pond_version != POND_VERSION:
                raise _failure()
            if any(
                record.source_agent not in _ROOT_AGENTS
                for record in self.root_inventory.records
            ):
                raise _failure()
            if type(self.counts) is not PondCorpusCounts:
                raise _failure()
            if type(self.resource_evidence) is not PondResourceEvidence:
                raise _failure()
            if len(self.root_inventory.records) > self.counts.sessions:
                raise _failure()
            if (
                sum(record.message_count for record in self.root_inventory.records)
                > self.counts.messages
            ):
                raise _failure()
        except _SnapshotError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _failure() from None


def pond_inventory_content_sha256(inventory: PondInventory) -> str:
    """Hash the complete canonical inventory content except capture time."""
    try:
        if type(inventory) is not PondInventory:
            raise _failure()
        wire = inventory.to_wire()
        payload = {
            "kind": wire["kind"],
            "schema_version": wire["schema_version"],
            "pond_version": wire["pond_version"],
            "records": wire["records"],
        }
        return private_json_sha256(payload)
    except _SnapshotError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _failure() from None


def capture_pond_store_snapshot(
    binary: Path,
    *,
    storage: LocalPondStore | RemotePondGeneration,
    pond_config: Path,
    workspace: Path,
    timeout_seconds: float,
    progress_callback: Callable[[], None] | None = None,
    resource_limits: ResourceLimits | None = None,
) -> PondStoreSnapshot:
    """Capture root records and exact whole-store counts without retaining rows."""
    workspace_descriptor: int | None = None
    config_descriptor: int | None = None
    store_descriptor: int | None = None
    try:
        timeout = _require_timeout(timeout_seconds)
        workspace_path, workspace_descriptor, workspace_identity = _pin_workspace(
            workspace
        )
        config_descriptor, config_identity = _pin_private_config(pond_config)
        selector, store_descriptor, store_identity = _storage_selector(storage)
        _require_pinned_path(pond_config, config_descriptor, config_identity)
        _require_local_store_path(storage, store_descriptor, store_identity)

        version = run_pond_process(
            binary,
            ("--version",),
            timeout_seconds=10.0,
            run_directory=workspace_path,
            label="snapshot-version",
            resource_limits=resource_limits,
            progress_callback=progress_callback,
        )
        if version.returncode != 0:
            raise _failure()
        version_bytes = _read_private_artifact(
            workspace_descriptor, version.stdout_path.name
        )
        try:
            version_tokens = tuple(version_bytes.decode("utf-8").split())
        except UnicodeDecodeError:
            raise _failure() from None
        if not is_pinned_pond_version(version_tokens):
            raise _failure()

        corpus_data, corpus_result = _run_sql(
            binary,
            selector=selector,
            pond_config=pond_config,
            workspace=workspace_path,
            workspace_descriptor=workspace_descriptor,
            timeout=timeout,
            sql=POND_CORPUS_SNAPSHOT_SQL,
            filename=_CORPUS_FILENAME,
            label="corpus-snapshot",
            progress_callback=progress_callback,
            resource_limits=resource_limits,
        )
        records, corpus = _corpus_snapshot(corpus_data)
        inventory = PondInventory(
            schema_version=1,
            captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            pond_version=POND_VERSION,
            records=records,
        )
        counts = PondCorpusCounts(
            sessions=corpus["sessions"],
            messages=corpus["messages"],
            parts=corpus["parts"],
            disallowed_sessions=corpus["disallowed_sessions"],
            logical_duplicate_groups=corpus["logical_duplicate_groups"],
            sessions_in_logical_duplicate_groups=corpus[
                "sessions_in_logical_duplicate_groups"
            ],
        )
        snapshot = PondStoreSnapshot(
            inventory,
            counts,
            _aggregate_resource_evidence(
                _process_resource_evidence(version),
                _process_resource_evidence(corpus_result),
            ),
        )
        _require_workspace_same(workspace_path, workspace_identity)
        _require_pinned_path(pond_config, config_descriptor, config_identity)
        _require_local_store_path(storage, store_descriptor, store_identity)
        _write_inventory(workspace_descriptor, inventory)
        _require_workspace_same(workspace_path, workspace_identity)
        return snapshot
    except _SnapshotError:
        raise
    except (BackupRuntimeError, PondProcessError):
        raise
    except Exception:
        raise _failure() from None
    finally:
        for descriptor in (store_descriptor, config_descriptor, workspace_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _require_generation_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _failure()
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise _failure()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _failure() from None
    if (
        parsed.scheme != "s3+https"
        or not parsed.hostname
        or not _R2_AUTHORITY.fullmatch(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.netloc != parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != value
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
        or "\\" in parsed.path
        or "%" in parsed.path
    ):
        raise _failure()
    segments = parsed.path.split("/")[1:]
    if (
        len(segments) < 3
        or any(segment in {"", ".", ".."} for segment in segments)
        or segments[-2] != "generations"
        or segments.count("generations") != 1
    ):
        raise _failure()
    try:
        generation = UUID(segments[-1])
    except (AttributeError, ValueError):
        raise _failure() from None
    if generation.version != 4 or str(generation) != segments[-1]:
        raise _failure()
    return value


def _require_timeout(value: float) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value != int(value)
        or not 5 <= value <= _MAX_TIMEOUT_SECONDS
    ):
        raise _failure()
    return int(value)


def _descriptor_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _pin_workspace(path: Path) -> tuple[Path, int, tuple[int, ...]]:
    try:
        requested = Path(os.fspath(path))
        if not requested.is_absolute():
            raise _failure()
        if not requested.exists():
            requested.parent.resolve(strict=True)
            requested.mkdir(mode=0o700)
        resolved = requested.resolve(strict=True)
        if resolved != requested:
            raise _failure()
        descriptor = _open_nofollow_path(
            requested, flags=_descriptor_flags(directory=True)
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise _failure()
        return requested, descriptor, _directory_identity(metadata)
    except _SnapshotError:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _failure() from None


def _pin_private_config(path: Path) -> tuple[int, tuple[int, ...]]:
    try:
        requested = Path(os.fspath(path))
        if not requested.is_absolute() or requested.resolve(strict=True) != requested:
            raise _failure()
        descriptor = _open_nofollow_path(requested)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_INVENTORY_BYTES
        ):
            raise _failure()
        return descriptor, _identity(metadata)
    except _SnapshotError:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _failure() from None


def _storage_selector(
    storage: LocalPondStore | RemotePondGeneration,
) -> tuple[str, int | None, tuple[int, ...] | None]:
    if type(storage) is RemotePondGeneration:
        return _require_generation_url(storage.url), None, None
    if type(storage) is not LocalPondStore:
        raise _failure()
    try:
        descriptor = _open_nofollow_path(
            storage.path, flags=_descriptor_flags(directory=True)
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _failure()
        return str(storage.path), descriptor, _identity(metadata)
    except _SnapshotError:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _require_pinned_path(
    path: Path, descriptor: int, expected: tuple[int, ...]
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            _identity(os.fstat(descriptor)) != expected
            or _identity(metadata) != expected
        ):
            raise _failure()
    except _SnapshotError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _require_local_store_path(
    storage: LocalPondStore | RemotePondGeneration,
    descriptor: int | None,
    expected: tuple[int, ...] | None,
) -> None:
    if type(storage) is RemotePondGeneration:
        if descriptor is not None or expected is not None:
            raise _failure()
        return
    if type(storage) is not LocalPondStore or descriptor is None or expected is None:
        raise _failure()
    _require_pinned_path(storage.path, descriptor, expected)


def _require_workspace_same(path: Path, expected: tuple[int, ...]) -> None:
    try:
        if _directory_identity(path.stat(follow_symlinks=False)) != expected:
            raise _failure()
    except _SnapshotError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _run_sql(
    binary: Path,
    *,
    selector: str,
    pond_config: Path,
    workspace: Path,
    workspace_descriptor: int,
    timeout: int,
    sql: str,
    filename: str,
    label: str,
    progress_callback: Callable[[], None] | None,
    resource_limits: ResourceLimits | None,
) -> tuple[bytes, PondProcessResult]:
    artifact = workspace / filename
    result = run_pond_process(
        binary,
        (
            "--config-file",
            str(pond_config),
            "--storage-path",
            selector,
            "sql",
            sql,
            "--format",
            "ndjson",
            "--output-file",
            str(artifact),
            "--timeout",
            str(timeout),
        ),
        timeout_seconds=float(timeout),
        run_directory=workspace,
        label=label,
        artifact_path=artifact,
        resource_limits=resource_limits,
        progress_callback=progress_callback,
    )
    if result.returncode != 0:
        raise _failure()
    return _read_private_artifact(workspace_descriptor, filename), result


def _read_private_artifact(directory_descriptor: int, name: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            _descriptor_flags(),
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_INVENTORY_BYTES
            ):
                raise _failure()
            data = stream.read(MAX_INVENTORY_BYTES + 1)
            after = os.fstat(stream.fileno())
        final = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            len(data) > MAX_INVENTORY_BYTES
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(final)
        ):
            raise _failure()
        return data
    except _SnapshotError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _corpus_snapshot(
    data: bytes,
) -> tuple[tuple[PondInventoryRecord, ...], dict[str, int]]:
    records: list[PondInventoryRecord] = []
    seen: set[tuple[str, str]] = set()
    aggregate: dict[str, int] | None = None
    lines = data.splitlines()
    if not lines or len(lines) > MAX_INVENTORY_RECORDS + 2:
        raise _failure()
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _failure() from None
        if not isinstance(row, dict) or set(row) != _SNAPSHOT_COLUMNS:
            raise _failure()
        row_kind = row["row_kind"]
        if row_kind == "aggregate":
            if aggregate is not None or any(
                row[field] is not None for field in _ROOT_COLUMNS
            ):
                raise _failure()
            aggregate = _count_values(row)
            continue
        if row_kind != "root" or any(
            row[field] is not None for field in _ALL_COUNT_COLUMNS
        ):
            raise _failure()
        if len(records) >= MAX_INVENTORY_RECORDS:
            raise _failure()
        try:
            record = _pond_record({field: row[field] for field in _ROOT_COLUMNS})
        except (KeyError, TypeError, ValueError):
            raise _failure() from None
        key = (record.source_agent, record.session_id)
        if key in seen:
            raise _failure()
        seen.add(key)
        records.append(record)
    if aggregate is None:
        raise _failure()
    return (
        tuple(sorted(records, key=lambda row: (row.source_agent, row.session_id))),
        aggregate,
    )


def _count_values(row: dict[str, Any]) -> dict[str, int]:
    values = {field: row[field] for field in _ALL_COUNT_COLUMNS}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values.values()
    ):
        raise _failure()
    return values


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _write_inventory(directory_descriptor: int, inventory: PondInventory) -> None:
    created = _write_private_json_at(
        directory_descriptor,
        POND_INVENTORY_FILENAME,
        inventory.to_wire(),
    )
    try:
        final = os.stat(
            POND_INVENTORY_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            created.st_dev != final.st_dev
            or created.st_ino != final.st_ino
            or not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.geteuid()
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise _failure()
        os.fsync(directory_descriptor)
    except _SnapshotError:
        raise
    except OSError:
        raise _failure() from None
