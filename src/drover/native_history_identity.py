"""Opaque identity helpers for native harness history sources."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable


def native_source_fingerprint(metadata: os.stat_result) -> str:
    """Hash the stable stat identity used by race-safe source discovery."""
    values = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    encoded = b"\0".join(str(value).encode("ascii") for value in values)
    return hashlib.sha256(b"drover-native-source-v1\0" + encoded).hexdigest()


def grouped_native_source_fingerprint(fingerprints: Iterable[str]) -> str:
    """Combine one or more source fingerprints without exposing their inputs."""
    values = tuple(sorted(fingerprints))
    if len(values) == 1:
        return values[0]
    encoded = b"\0".join(value.encode("ascii") for value in values)
    return hashlib.sha256(b"drover-native-source-group-v1\0" + encoded).hexdigest()
