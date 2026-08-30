"""Strict private configuration for manual Pond backup operations."""

from __future__ import annotations

import os
import re
import stat
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from drover.server.archive.inventory import MAX_INVENTORY_BYTES, _open_nofollow_path

_CONFIG_ERROR = "archive backup config failed"
_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "pond_binary",
        "local_pond_config",
        "local_store",
        "remote_pond_config",
        "backup_root_url",
        "store_scope_id",
        "receipt_directory",
        "copy_timeout_seconds",
        "max_rss_bytes",
        "max_physical_bytes",
        "max_swap_growth_bytes",
    }
)
_R2_AUTHORITY = re.compile(r"\A[a-z0-9][a-z0-9-]*\.r2\.cloudflarestorage\.com\Z")
_MIN_COPY_TIMEOUT_SECONDS = 5
_MAX_COPY_TIMEOUT_SECONDS = 1800
_MAX_RSS_BYTES = 3 * 1024**3
_MAX_PHYSICAL_BYTES = 4 * 1024**3
_MAX_SWAP_GROWTH_BYTES = 512 * 1024**2


class _InvalidConfig(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BackupConfig:
    schema_version: int
    pond_binary: Path
    local_pond_config: Path
    local_store: Path
    remote_pond_config: Path
    backup_root_url: str
    store_scope_id: str
    receipt_directory: Path
    copy_timeout_seconds: int
    max_rss_bytes: int
    max_physical_bytes: int
    max_swap_growth_bytes: int


def _invalid() -> _InvalidConfig:
    return _InvalidConfig(_CONFIG_ERROR)


def _descriptor_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_private_toml(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        descriptor = _open_nofollow_path(path)
    except (OSError, TypeError, ValueError):
        raise _invalid() from None
    try:
        with os.fdopen(descriptor, "rb") as input_file:
            before = os.fstat(input_file.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_INVENTORY_BYTES
            ):
                raise _invalid()
            data = input_file.read(MAX_INVENTORY_BYTES + 1)
            after = os.fstat(input_file.fileno())
    except _InvalidConfig:
        raise
    except (OSError, ValueError):
        raise _invalid() from None
    if len(data) > MAX_INVENTORY_BYTES or _metadata_identity(
        before
    ) != _metadata_identity(after):
        raise _invalid()
    try:
        payload = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        raise _invalid() from None
    if not isinstance(payload, dict) or set(payload) != _CONFIG_FIELDS:
        raise _invalid()
    return payload


def _require_schema_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise _invalid()
    return value


def _require_string(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _invalid()
    return value


def _require_uuid(value: Any) -> str:
    value = _require_string(value)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise _invalid() from None
    if parsed.version != 4 or str(parsed) != value:
        raise _invalid()
    return value


def _canonical_existing_path(value: Any) -> Path:
    raw = _require_string(value)
    path = Path(raw)
    if not path.is_absolute():
        raise _invalid()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _invalid() from None
    if resolved != path:
        raise _invalid()
    return path


def _open_validated_path(path: Path, *, directory: bool) -> os.stat_result:
    try:
        descriptor = _open_nofollow_path(
            path,
            flags=_descriptor_flags(directory=directory),
        )
    except (OSError, TypeError, ValueError):
        raise _invalid() from None
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        raise _invalid() from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise _invalid()
    return metadata


def _require_executable(value: Any) -> Path:
    path = _canonical_existing_path(value)
    metadata = _open_validated_path(path, directory=False)
    if not metadata.st_mode & 0o111 or not os.access(path, os.X_OK):
        raise _invalid()
    return path


def _require_private_file(value: Any) -> Path:
    path = _canonical_existing_path(value)
    metadata = _open_validated_path(path, directory=False)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise _invalid()
    return path


def _require_local_store(value: Any) -> Path:
    path = _canonical_existing_path(value)
    metadata = _open_validated_path(path, directory=True)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise _invalid()
    return path


def _require_receipt_directory(value: Any) -> Path:
    path = _canonical_existing_path(value)
    metadata = _open_validated_path(path, directory=True)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise _invalid()
    return path


def _require_backup_root_url(value: Any) -> str:
    value = _require_string(value)
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise _invalid()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _invalid() from None
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
        raise _invalid()
    segments = parsed.path.split("/")[1:]
    if not segments or any(segment in {"", ".", ".."} for segment in segments):
        raise _invalid()
    if "generations" in segments:
        raise _invalid()
    return value


def _require_integer_range(value: Any, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise _invalid()
    return value


def load_backup_config(path: str | os.PathLike[str]) -> BackupConfig:
    """Load one exact, bounded, owner-only backup TOML document."""
    try:
        payload = _read_private_toml(path)
        return BackupConfig(
            schema_version=_require_schema_version(payload["schema_version"]),
            pond_binary=_require_executable(payload["pond_binary"]),
            local_pond_config=_require_private_file(payload["local_pond_config"]),
            local_store=_require_local_store(payload["local_store"]),
            remote_pond_config=_require_private_file(payload["remote_pond_config"]),
            backup_root_url=_require_backup_root_url(payload["backup_root_url"]),
            store_scope_id=_require_uuid(payload["store_scope_id"]),
            receipt_directory=_require_receipt_directory(payload["receipt_directory"]),
            copy_timeout_seconds=_require_integer_range(
                payload["copy_timeout_seconds"],
                _MIN_COPY_TIMEOUT_SECONDS,
                _MAX_COPY_TIMEOUT_SECONDS,
            ),
            max_rss_bytes=_require_integer_range(
                payload["max_rss_bytes"], 1, _MAX_RSS_BYTES
            ),
            max_physical_bytes=_require_integer_range(
                payload["max_physical_bytes"], 1, _MAX_PHYSICAL_BYTES
            ),
            max_swap_growth_bytes=_require_integer_range(
                payload["max_swap_growth_bytes"], 0, _MAX_SWAP_GROWTH_BYTES
            ),
        )
    except _InvalidConfig:
        raise ValueError(_CONFIG_ERROR) from None
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError(_CONFIG_ERROR) from None


def generation_storage_url(config: BackupConfig, generation_id: UUID) -> str:
    """Append the fixed generation path to an already-validated backup root."""
    try:
        if type(config) is not BackupConfig or type(generation_id) is not UUID:
            raise _invalid()
        if generation_id.version != 4:
            raise _invalid()
        root = _require_backup_root_url(config.backup_root_url)
        return f"{root}/generations/{generation_id}"
    except _InvalidConfig:
        raise ValueError(_CONFIG_ERROR) from None
