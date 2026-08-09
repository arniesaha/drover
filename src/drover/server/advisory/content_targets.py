"""Safe, bounded construction of ephemeral advisory content bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterable, Sequence

from drover.server.advisory.redaction import redact_content

DEFAULT_MAX_FILE_BYTES = 131072
DEFAULT_MAX_BUNDLE_BYTES = 524288


class ContentTargetError(ValueError):
    """A configured target cannot be safely included in a content bundle."""


@dataclass(frozen=True)
class ContentTarget:
    path: Path
    target_id: str = ""

    def __post_init__(self) -> None:
        path = Path(self.path)
        target_id = self.target_id.strip() or path.name
        if not target_id or len(target_id) > 256:
            raise ValueError("target_id must contain between 1 and 256 characters")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "target_id", target_id)


@dataclass(frozen=True)
class BundledTarget:
    target_id: str
    content_hash: str
    redacted_content: str


@dataclass(frozen=True)
class ContentBundle:
    host_id: str
    created_at: datetime
    targets: tuple[BundledTarget, ...]
    bundle_hash: str

    def __post_init__(self) -> None:
        if not self.host_id.strip():
            raise ValueError("host_id is required")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


def validate_content_bundle(
    bundle: ContentBundle,
    *,
    host_id: str,
    requested_ids: Sequence[str],
) -> ContentBundle:
    """Validate identity and hashes before an ephemeral bundle reaches a model."""

    if not isinstance(bundle, ContentBundle):
        raise ValueError("content bundle fetcher must return a validated ContentBundle")
    if bundle.host_id != host_id:
        raise ValueError("content bundle host does not match request")
    returned_ids = tuple(target.target_id for target in bundle.targets)
    if returned_ids != tuple(requested_ids):
        raise ValueError("content bundle target IDs do not match request")
    hash_pairs: list[tuple[str, str]] = []
    for target in bundle.targets:
        computed = hashlib.sha256(target.redacted_content.encode("utf-8")).hexdigest()
        if target.content_hash != computed:
            raise ValueError("content bundle target hash does not match content")
        hash_pairs.append((target.target_id, target.content_hash))
    computed_bundle_hash = hashlib.sha256(
        json.dumps(hash_pairs, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    if bundle.bundle_hash != computed_bundle_hash:
        raise ValueError("content bundle hash does not match target hashes")
    return bundle


def content_bundle_from_payload(
    payload: object,
    *,
    host_id: str,
    requested_ids: Sequence[str],
) -> ContentBundle:
    """Strictly parse the authenticated transport payload into a bundle."""

    if not isinstance(payload, dict) or set(payload) != {
        "bundle_hash",
        "created_at",
        "targets",
    }:
        raise ValueError("content bundle response has invalid fields")
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
        raw_targets = payload["targets"]
        if not isinstance(raw_targets, list):
            raise TypeError
        targets: list[BundledTarget] = []
        for item in raw_targets:
            if not isinstance(item, dict) or set(item) != {
                "target_id",
                "content_hash",
                "redacted_content",
            }:
                raise TypeError
            if not all(
                isinstance(item[name], str)
                for name in ("target_id", "content_hash", "redacted_content")
            ):
                raise TypeError
            targets.append(
                BundledTarget(
                    target_id=item["target_id"],
                    content_hash=item["content_hash"],
                    redacted_content=item["redacted_content"],
                )
            )
        bundle = ContentBundle(
            host_id=host_id,
            created_at=created_at,
            targets=tuple(targets),
            bundle_hash=payload["bundle_hash"],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("content bundle response is invalid") from None
    return validate_content_bundle(bundle, host_id=host_id, requested_ids=requested_ids)


def build_content_bundle(
    targets: Iterable[ContentTarget],
    *,
    allowed_roots: Sequence[str | Path],
    host_id: str = "local",
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> ContentBundle:
    """Read, redact, and hash allowlisted regular files without persisting them."""

    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")
    if max_bundle_bytes <= 0:
        raise ValueError("max_bundle_bytes must be positive")
    roots = _resolve_allowed_roots(allowed_roots)
    if not roots:
        raise ContentTargetError("at least one allowed root is required")

    bundled: list[BundledTarget] = []
    seen_ids: set[str] = set()
    raw_total = 0
    redacted_total = 0
    for target in targets:
        if target.target_id in seen_ids:
            raise ContentTargetError("target IDs must be unique")
        seen_ids.add(target.target_id)
        path = _resolve_target(target.path, roots)
        payload = _read_regular_file(path, max_file_bytes=max_file_bytes)
        raw_total += len(payload)
        if raw_total > max_bundle_bytes:
            raise ContentTargetError("content bundle exceeds aggregate byte limit")
        try:
            content = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContentTargetError("content target is not valid UTF-8") from exc
        redacted = redact_content(content)
        redacted_bytes = redacted.encode("utf-8")
        redacted_total += len(redacted_bytes)
        if redacted_total > max_bundle_bytes:
            raise ContentTargetError("content bundle exceeds aggregate byte limit")
        bundled.append(
            BundledTarget(
                target_id=target.target_id,
                content_hash=hashlib.sha256(redacted_bytes).hexdigest(),
                redacted_content=redacted,
            )
        )

    hash_input = json.dumps(
        [(item.target_id, item.content_hash) for item in bundled],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ContentBundle(
        host_id=host_id,
        created_at=datetime.now(timezone.utc),
        targets=tuple(bundled),
        bundle_hash=hashlib.sha256(hash_input).hexdigest(),
    )


@dataclass(frozen=True)
class _AllowedRoot:
    lexical: Path
    canonical: Path
    is_directory: bool


def _resolve_allowed_roots(roots: Sequence[str | Path]) -> tuple[_AllowedRoot, ...]:
    resolved: list[_AllowedRoot] = []
    for root_value in roots:
        lexical = Path(root_value).absolute()
        if lexical.is_symlink():
            raise ContentTargetError("allowed root must not be a symlink")
        try:
            canonical = lexical.resolve(strict=True)
        except OSError as exc:
            raise ContentTargetError("allowed root is inaccessible") from exc
        if not (canonical.is_dir() or canonical.is_file()):
            raise ContentTargetError("allowed root must be a directory or regular file")
        resolved.append(
            _AllowedRoot(
                lexical=lexical,
                canonical=canonical,
                is_directory=canonical.is_dir(),
            )
        )
    return tuple(resolved)


def _resolve_target(path_value: Path, roots: Sequence[_AllowedRoot]) -> Path:
    path = Path(path_value)
    if ".." in path.parts:
        raise ContentTargetError("content target contains path traversal")
    lexical = path.absolute()
    try:
        canonical = lexical.resolve(strict=True)
    except OSError as exc:
        raise ContentTargetError("content target is inaccessible") from exc

    matched = next(
        (
            root
            for root in roots
            if canonical == root.canonical
            or (root.is_directory and root.canonical in canonical.parents)
        ),
        None,
    )
    if matched is None:
        raise ContentTargetError("content target is outside allowlist")
    if lexical.is_symlink() or _has_symlink_below_root(lexical, matched.lexical):
        raise ContentTargetError("symlink content targets are not allowed")
    return canonical


def _has_symlink_below_root(target: Path, root: Path) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _read_regular_file(path: Path, *, max_file_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContentTargetError("content target could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContentTargetError("content target must be a regular file")
        if metadata.st_size > max_file_bytes:
            raise ContentTargetError("content target exceeds per-file byte limit")
        chunks: list[bytes] = []
        remaining = max_file_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_file_bytes:
            raise ContentTargetError("content target exceeds per-file byte limit")
        return payload
    finally:
        os.close(descriptor)
