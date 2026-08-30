"""Exact private verification receipts for immutable Pond backups."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    _open_nofollow_path,
    _write_private_json_at,
    private_json_sha256,
)
from drover.server.archive.pond_inventory import POND_VERSION

_RECEIPT_ERROR = "archive backup receipt failed"
_MAX_RECEIPTS = 1024
_MAX_RSS_BYTES = 3 * 1024**3
_MAX_PHYSICAL_BYTES = 4 * 1024**3
_MAX_SWAP_GROWTH_BYTES = 512 * 1024**2
_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_RECEIPT_NAME = re.compile(
    r"\Abackup-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json\Z"
)


def _load_exclusive_rename() -> tuple[Any | None, int]:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            function = library.renameatx_np
            flag = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            function = library.renameat2
            flag = 0x00000001  # RENAME_NOREPLACE
        else:
            return None, 0
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, flag
    except (AttributeError, OSError, TypeError, ValueError):
        return None, 0


_EXCLUSIVE_RENAME, _EXCLUSIVE_RENAME_FLAG = _load_exclusive_rename()
_RECEIPT_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "created_at",
        "pond_version",
        "store_scope_id",
        "generation_id",
        "previous_receipt_sha256",
        "source_inventory_sha256",
        "local_pond_inventory_sha256",
        "remote_pond_inventory_sha256",
        "coverage_report_sha256",
        "sessions",
        "messages",
        "parts",
        "source_not_archive_eligible",
        "collision_counts",
        "copy_duration_ms",
        "verify_duration_ms",
        "health_samples",
        "health_p95_ms",
        "peak_rss_bytes",
        "peak_physical_bytes",
        "swap_delta_bytes",
        "result",
    }
)
_COLLISION_FIELDS = frozenset(
    {
        "duplicate_source_groups",
        "cross_harness_native_id_groups",
        "archive_logical_duplicate_candidate_groups",
        "archive_signature_unverifiable",
    }
)


class _InvalidReceipt(ValueError):
    pass


def _invalid() -> _InvalidReceipt:
    return _InvalidReceipt(_RECEIPT_ERROR)


@dataclass(frozen=True, slots=True)
class CollisionCounts:
    duplicate_source_groups: int
    cross_harness_native_id_groups: int
    archive_logical_duplicate_candidate_groups: int
    archive_signature_unverifiable: int

    def to_wire(self) -> dict[str, int]:
        _validate_collision_counts(self)
        return {
            "duplicate_source_groups": self.duplicate_source_groups,
            "cross_harness_native_id_groups": self.cross_harness_native_id_groups,
            "archive_logical_duplicate_candidate_groups": (
                self.archive_logical_duplicate_candidate_groups
            ),
            "archive_signature_unverifiable": self.archive_signature_unverifiable,
        }


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    schema_version: int
    created_at: str
    pond_version: str
    store_scope_id: str
    generation_id: str
    previous_receipt_sha256: str | None
    source_inventory_sha256: str
    local_pond_inventory_sha256: str
    remote_pond_inventory_sha256: str
    coverage_report_sha256: str
    sessions: int
    messages: int
    parts: int
    source_not_archive_eligible: int
    collision_counts: CollisionCounts
    copy_duration_ms: int
    verify_duration_ms: int
    health_samples: int
    health_p95_ms: float
    peak_rss_bytes: int
    peak_physical_bytes: int
    swap_delta_bytes: int
    result: Literal["verified"] = "verified"

    def to_wire(self) -> dict[str, Any]:
        _validate_receipt(self)
        return {
            "kind": "pond_backup_receipt",
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "pond_version": self.pond_version,
            "store_scope_id": self.store_scope_id,
            "generation_id": self.generation_id,
            "previous_receipt_sha256": self.previous_receipt_sha256,
            "source_inventory_sha256": self.source_inventory_sha256,
            "local_pond_inventory_sha256": self.local_pond_inventory_sha256,
            "remote_pond_inventory_sha256": self.remote_pond_inventory_sha256,
            "coverage_report_sha256": self.coverage_report_sha256,
            "sessions": self.sessions,
            "messages": self.messages,
            "parts": self.parts,
            "source_not_archive_eligible": self.source_not_archive_eligible,
            "collision_counts": self.collision_counts.to_wire(),
            "copy_duration_ms": self.copy_duration_ms,
            "verify_duration_ms": self.verify_duration_ms,
            "health_samples": self.health_samples,
            "health_p95_ms": self.health_p95_ms,
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_physical_bytes": self.peak_physical_bytes,
            "swap_delta_bytes": self.swap_delta_bytes,
            "result": self.result,
        }


def _require_exact_dict(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _invalid()
    return value


def _require_int(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid()
    if maximum is not None and value > maximum:
        raise _invalid()
    return value


def _require_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise _invalid() from None
    if parsed.version != 4 or str(parsed) != value:
        raise _invalid()
    return value


def _require_digest(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _invalid()
    return value


def _require_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise _invalid()
    try:
        parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
        if datetime.fromisoformat(parsed).tzinfo is None:
            raise _invalid()
    except ValueError:
        raise _invalid() from None
    return value


def _require_health_p95(value: Any) -> float:
    if (
        not isinstance(value, float)
        or not math.isfinite(value)
        or value < 0
        or value >= 100
    ):
        raise _invalid()
    return float(value)


def _validate_collision_counts(counts: CollisionCounts) -> None:
    if type(counts) is not CollisionCounts:
        raise _invalid()
    for value in (
        counts.duplicate_source_groups,
        counts.cross_harness_native_id_groups,
        counts.archive_logical_duplicate_candidate_groups,
        counts.archive_signature_unverifiable,
    ):
        if _require_int(value) != 0:
            raise _invalid()


def _validate_receipt(receipt: BackupReceipt) -> None:
    if type(receipt) is not BackupReceipt:
        raise _invalid()
    if _require_int(receipt.schema_version, minimum=1, maximum=1) != 1:
        raise _invalid()
    _require_timestamp(receipt.created_at)
    if receipt.pond_version != POND_VERSION:
        raise _invalid()
    _require_uuid(receipt.store_scope_id)
    _require_uuid(receipt.generation_id)
    _require_digest(receipt.previous_receipt_sha256, optional=True)
    _require_digest(receipt.source_inventory_sha256)
    _require_digest(receipt.local_pond_inventory_sha256)
    _require_digest(receipt.remote_pond_inventory_sha256)
    _require_digest(receipt.coverage_report_sha256)
    _require_int(receipt.sessions)
    _require_int(receipt.messages)
    _require_int(receipt.parts)
    _require_int(receipt.source_not_archive_eligible)
    _validate_collision_counts(receipt.collision_counts)
    _require_int(receipt.copy_duration_ms)
    _require_int(receipt.verify_duration_ms)
    _require_int(receipt.health_samples, minimum=30)
    _require_health_p95(receipt.health_p95_ms)
    _require_int(receipt.peak_rss_bytes, maximum=_MAX_RSS_BYTES)
    _require_int(receipt.peak_physical_bytes, maximum=_MAX_PHYSICAL_BYTES)
    _require_int(receipt.swap_delta_bytes, maximum=_MAX_SWAP_GROWTH_BYTES)
    if receipt.result != "verified":
        raise _invalid()


def _receipt_from_wire(payload: Any) -> BackupReceipt:
    root = _require_exact_dict(payload, _RECEIPT_FIELDS)
    if root["kind"] != "pond_backup_receipt":
        raise _invalid()
    collision = _require_exact_dict(root["collision_counts"], _COLLISION_FIELDS)
    receipt = BackupReceipt(
        schema_version=_require_int(root["schema_version"], minimum=1, maximum=1),
        created_at=_require_timestamp(root["created_at"]),
        pond_version=root["pond_version"],
        store_scope_id=_require_uuid(root["store_scope_id"]),
        generation_id=_require_uuid(root["generation_id"]),
        previous_receipt_sha256=_require_digest(
            root["previous_receipt_sha256"], optional=True
        ),
        source_inventory_sha256=_require_digest(root["source_inventory_sha256"]),
        local_pond_inventory_sha256=_require_digest(
            root["local_pond_inventory_sha256"]
        ),
        remote_pond_inventory_sha256=_require_digest(
            root["remote_pond_inventory_sha256"]
        ),
        coverage_report_sha256=_require_digest(root["coverage_report_sha256"]),
        sessions=_require_int(root["sessions"]),
        messages=_require_int(root["messages"]),
        parts=_require_int(root["parts"]),
        source_not_archive_eligible=_require_int(root["source_not_archive_eligible"]),
        collision_counts=CollisionCounts(
            duplicate_source_groups=_require_int(collision["duplicate_source_groups"]),
            cross_harness_native_id_groups=_require_int(
                collision["cross_harness_native_id_groups"]
            ),
            archive_logical_duplicate_candidate_groups=_require_int(
                collision["archive_logical_duplicate_candidate_groups"]
            ),
            archive_signature_unverifiable=_require_int(
                collision["archive_signature_unverifiable"]
            ),
        ),
        copy_duration_ms=_require_int(root["copy_duration_ms"]),
        verify_duration_ms=_require_int(root["verify_duration_ms"]),
        health_samples=_require_int(root["health_samples"], minimum=30),
        health_p95_ms=_require_health_p95(root["health_p95_ms"]),
        peak_rss_bytes=_require_int(root["peak_rss_bytes"], maximum=_MAX_RSS_BYTES),
        peak_physical_bytes=_require_int(
            root["peak_physical_bytes"], maximum=_MAX_PHYSICAL_BYTES
        ),
        swap_delta_bytes=_require_int(
            root["swap_delta_bytes"], maximum=_MAX_SWAP_GROWTH_BYTES
        ),
        result=root["result"],
    )
    _validate_receipt(receipt)
    return receipt


def load_backup_receipt(path: str | os.PathLike[str]) -> BackupReceipt:
    """Load and validate one bounded owner-only receipt."""
    descriptor = -1
    try:
        descriptor = _open_nofollow_path(path)
        return _receipt_from_wire(_read_private_json_descriptor(descriptor))
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_ctime_ns,
    )


def _open_receipt_directory(
    path: str | os.PathLike[str],
) -> tuple[Path, int, tuple[int, ...]]:
    descriptor = -1
    try:
        directory = Path(path)
        if not directory.is_absolute() or directory.resolve(strict=True) != directory:
            raise _invalid()
        descriptor = _open_nofollow_path(directory, flags=_directory_flags())
        opened = os.fstat(descriptor)
        lexical_descriptor = _open_nofollow_path(directory, flags=_directory_flags())
        try:
            lexical = os.fstat(lexical_descriptor)
        finally:
            os.close(lexical_descriptor)
    except (OSError, RuntimeError, TypeError, ValueError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _invalid() from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or _directory_identity(opened) != _directory_identity(lexical)
    ):
        os.close(descriptor)
        raise _invalid()
    return directory, descriptor, _directory_identity(opened)


def _require_unchanged_directory(
    directory: Path, descriptor: int, identity: tuple[int, ...]
) -> None:
    try:
        opened = os.fstat(descriptor)
        lexical_descriptor = _open_nofollow_path(directory, flags=_directory_flags())
        try:
            lexical = os.fstat(lexical_descriptor)
        finally:
            os.close(lexical_descriptor)
    except OSError:
        raise _invalid() from None
    if (
        _directory_identity(opened) != identity
        or _directory_identity(lexical) != identity
    ):
        raise _invalid()


def _read_private_json_descriptor(input_descriptor: int) -> Any:
    try:
        before = os.fstat(input_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_INVENTORY_BYTES
        ):
            raise _invalid()
        data = os.read(input_descriptor, MAX_INVENTORY_BYTES + 1)
        after = os.fstat(input_descriptor)
    except _InvalidReceipt:
        raise
    except (OSError, ValueError):
        raise _invalid() from None
    if len(data) > MAX_INVENTORY_BYTES or (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise _invalid()
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid() from None


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _read_private_json_at(descriptor: int, name: str) -> Any:
    input_descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        input_descriptor = os.open(name, flags, dir_fd=descriptor)
        return _read_private_json_descriptor(input_descriptor)
    except (OSError, TypeError, ValueError):
        raise _invalid() from None
    finally:
        if input_descriptor >= 0:
            try:
                os.close(input_descriptor)
            except OSError:
                pass


def _scan_receipts(descriptor: int) -> dict[str, tuple[str, BackupReceipt]]:
    names: list[str] = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if _RECEIPT_NAME.fullmatch(entry.name):
                    names.append(entry.name)
                    if len(names) > _MAX_RECEIPTS:
                        raise _invalid()
    except OSError:
        raise _invalid() from None
    scanned: dict[str, tuple[str, BackupReceipt]] = {}
    for name in sorted(names):
        match = _RECEIPT_NAME.fullmatch(name)
        if match is None:
            raise _invalid()
        receipt = _receipt_from_wire(_read_private_json_at(descriptor, name))
        if receipt.generation_id != match.group(1):
            raise _invalid()
        digest = private_json_sha256(receipt.to_wire())
        if digest in scanned:
            raise _invalid()
        scanned[digest] = (name, receipt)
    return scanned


def _linear_scope_chain(
    scanned: dict[str, tuple[str, BackupReceipt]], store_scope_id: str
) -> tuple[tuple[str, BackupReceipt], ...]:
    scoped = {
        digest: entry
        for digest, entry in scanned.items()
        if entry[1].store_scope_id == store_scope_id
    }
    if not scoped:
        return ()
    roots = [
        digest
        for digest, (_, receipt) in scoped.items()
        if receipt.previous_receipt_sha256 is None
    ]
    if len(roots) != 1:
        raise _invalid()
    children: dict[str, str] = {}
    for digest, (_, receipt) in scoped.items():
        previous = receipt.previous_receipt_sha256
        if previous is None:
            continue
        if previous not in scoped or previous in children:
            raise _invalid()
        children[previous] = digest
    ordered: list[tuple[str, BackupReceipt]] = []
    seen: set[str] = set()
    current: str | None = roots[0]
    while current is not None:
        if current in seen:
            raise _invalid()
        seen.add(current)
        ordered.append(scoped[current])
        current = children.get(current)
    if seen != set(scoped):
        raise _invalid()
    return tuple(ordered)


def load_backup_receipt_chain(
    path: str | os.PathLike[str], receipt_directory: str | os.PathLike[str]
) -> tuple[BackupReceipt, ...]:
    """Validate one complete same-scope chain and return root through selection."""
    descriptor = -1
    try:
        directory, descriptor, identity = _open_receipt_directory(receipt_directory)
        selected = Path(path)
        if not selected.is_absolute() or selected.parent != directory:
            raise _invalid()
        _require_unchanged_directory(directory, descriptor, identity)
        scanned = _scan_receipts(descriptor)
        selected_entry = next(
            (entry for entry in scanned.values() if entry[0] == selected.name), None
        )
        if selected_entry is None:
            raise _invalid()
        ordered = _linear_scope_chain(scanned, selected_entry[1].store_scope_id)
        selected_index = next(
            (index for index, (name, _) in enumerate(ordered) if name == selected.name),
            None,
        )
        if selected_index is None:
            raise _invalid()
        return tuple(receipt for _, receipt in ordered[: selected_index + 1])
    except (KeyError, OSError, StopIteration, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def latest_backup_receipt(
    receipt_directory: str | os.PathLike[str], store_scope_id: str
) -> BackupReceipt | None:
    """Return the tail of the one valid chain for a private store scope."""
    descriptor = -1
    try:
        directory, descriptor, identity = _open_receipt_directory(receipt_directory)
        return _latest_backup_receipt_at(
            directory,
            descriptor,
            identity,
            store_scope_id,
        )
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _latest_backup_receipt_at(
    directory: Path,
    descriptor: int,
    identity: tuple[int, ...],
    store_scope_id: str,
) -> BackupReceipt | None:
    """Return one tail using only a caller-owned pinned directory descriptor."""
    try:
        scope = _require_uuid(store_scope_id)
        _require_unchanged_directory(directory, descriptor, identity)
        ordered = _linear_scope_chain(_scan_receipts(descriptor), scope)
        _require_unchanged_directory(directory, descriptor, identity)
        return ordered[-1][1] if ordered else None
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None


def write_backup_receipt(
    receipt_directory: str | os.PathLike[str], receipt: BackupReceipt
) -> Path:
    """Exclusively persist one validated receipt in a pinned private directory."""
    descriptor = -1
    try:
        directory, descriptor, identity = _open_receipt_directory(receipt_directory)
        return _write_backup_receipt_at(
            directory,
            descriptor,
            identity,
            receipt,
        )
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_backup_receipt_at(
    directory: Path,
    descriptor: int,
    identity: tuple[int, ...],
    receipt: BackupReceipt,
    *,
    before_publish: Callable[[], None] | None = None,
) -> Path:
    """Write through one caller-owned pinned receipt-directory descriptor."""
    temporary_name = ""
    try:
        payload = receipt.to_wire()
        name = f"backup-{receipt.generation_id}.json"
        final_path = directory / name
        _require_exclusive_rename_supported()
        _require_unchanged_directory(directory, descriptor, identity)
        temporary_name = f".backup-receipt-{uuid4()}.tmp"
        created = _write_private_json_at(descriptor, temporary_name, payload)
        temporary_identity = (created.st_dev, created.st_ino)
        _require_private_temporary(
            descriptor,
            temporary_name,
            temporary_identity,
        )
        prepared_identity = _require_same_directory_node(
            directory,
            descriptor,
            identity,
        )
        if before_publish is not None:
            before_publish()
        _require_unchanged_directory(directory, descriptor, prepared_identity)
        _require_private_temporary(
            descriptor,
            temporary_name,
            temporary_identity,
        )
        _require_absent_entry(descriptor, name)
        os.fsync(descriptor)
    except (KeyError, OSError, TypeError, ValueError):
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=descriptor)
            except OSError:
                pass
        raise ValueError(_RECEIPT_ERROR) from None
    try:
        _rename_noreplace_at(descriptor, temporary_name, descriptor, name)
    except (OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
    return final_path


def _require_exclusive_rename_supported() -> None:
    if _EXCLUSIVE_RENAME is None or _EXCLUSIVE_RENAME_FLAG == 0:
        raise _invalid()


def _require_private_temporary(
    descriptor: int,
    name: str,
    expected: tuple[int, int],
) -> None:
    temporary = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(temporary.st_mode)
        or temporary.st_uid != os.geteuid()
        or temporary.st_nlink != 1
        or stat.S_IMODE(temporary.st_mode) != 0o600
        or (temporary.st_dev, temporary.st_ino) != expected
    ):
        raise _invalid()


def _require_absent_entry(descriptor: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise _invalid()


def _rename_noreplace_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    function = _EXCLUSIVE_RENAME
    flag = _EXCLUSIVE_RENAME_FLAG
    if function is None or flag == 0:
        raise OSError(errno.ENOTSUP, "exclusive receipt publication unsupported")
    if any(
        isinstance(descriptor, bool) or not isinstance(descriptor, int)
        for descriptor in (source_descriptor, destination_descriptor)
    ):
        raise TypeError
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    ctypes.set_errno(0)
    result = function(
        source_descriptor,
        source,
        destination_descriptor,
        destination,
        flag,
    )
    if result == 0:
        return
    error = ctypes.get_errno() or errno.EIO
    raise OSError(error, "exclusive receipt publication failed")


def _require_same_directory_node(
    directory: Path,
    descriptor: int,
    expected: tuple[int, ...],
) -> tuple[int, ...]:
    try:
        opened = _directory_identity(os.fstat(descriptor))
        lexical_descriptor = _open_nofollow_path(directory, flags=_directory_flags())
        try:
            lexical = _directory_identity(os.fstat(lexical_descriptor))
        finally:
            os.close(lexical_descriptor)
    except OSError:
        raise _invalid() from None
    if opened != lexical or opened[:4] != expected[:4]:
        raise _invalid()
    return opened


def backup_receipt_summary(receipt: BackupReceipt) -> dict[str, Any]:
    """Return only fixed aggregate verification evidence for operator output."""
    try:
        _validate_receipt(receipt)
        return {
            "schema_version": receipt.schema_version,
            "pond_version": receipt.pond_version,
            "sessions": receipt.sessions,
            "messages": receipt.messages,
            "parts": receipt.parts,
            "source_not_archive_eligible": receipt.source_not_archive_eligible,
            "collision_counts": receipt.collision_counts.to_wire(),
            "copy_duration_ms": receipt.copy_duration_ms,
            "verify_duration_ms": receipt.verify_duration_ms,
            "health_samples": receipt.health_samples,
            "health_p95_ms": receipt.health_p95_ms,
            "peak_rss_bytes": receipt.peak_rss_bytes,
            "peak_physical_bytes": receipt.peak_physical_bytes,
            "swap_delta_bytes": receipt.swap_delta_bytes,
            "result": receipt.result,
        }
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_RECEIPT_ERROR) from None
