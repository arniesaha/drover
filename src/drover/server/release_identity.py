"""Safe, launch-time identity for the internal TestFlight staging lane."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from drover import __version__

_ROLE = "testflight-staging"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SESSION_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ATTESTATION_KEYS = {"source_sha", "host_id", "completed_at", "session_id_sha256"}
_INVALID_IDENTITY = "invalid staging release identity"


def _is_timezone_bearing_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None


@dataclass(frozen=True)
class StagingProbeAttestation:
    source_sha: str
    host_id: str
    completed_at: str
    session_id_sha256: str

    def as_json(self) -> dict[str, str]:
        return {
            "source_sha": self.source_sha,
            "host_id": self.host_id,
            "completed_at": self.completed_at,
            "session_id_sha256": self.session_id_sha256,
        }


@dataclass(frozen=True)
class ReleaseIdentity:
    role: str
    source_sha: str
    package_version: str
    staging_probe: StagingProbeAttestation | None

    def as_json(self) -> dict[str, object]:
        return {
            "package_version": self.package_version,
            "role": self.role,
            "source_sha": self.source_sha,
            "staging_probe": (
                self.staging_probe.as_json() if self.staging_probe else None
            ),
        }


def _load_probe(path: str, source_sha: str) -> StagingProbeAttestation | None:
    """Return only a matching, owner-only attestation, otherwise nothing."""
    try:
        attestation_path = Path(path)
        metadata = attestation_path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            return None
        raw = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or set(raw) != _ATTESTATION_KEYS:
        return None
    if (
        raw.get("source_sha") != source_sha
        or not isinstance(raw.get("host_id"), str)
        or not raw["host_id"]
        or not _is_timezone_bearing_iso_timestamp(raw.get("completed_at"))
        or not isinstance(raw.get("session_id_sha256"), str)
        or not _SESSION_DIGEST_RE.fullmatch(raw["session_id_sha256"])
    ):
        return None
    return StagingProbeAttestation(
        source_sha=source_sha,
        host_id=raw["host_id"],
        completed_at=raw["completed_at"],
        session_id_sha256=raw["session_id_sha256"],
    )


def load_release_identity(environ: Mapping[str, str]) -> ReleaseIdentity:
    """Load the staging identity, refusing unknown launch provenance."""
    role = environ.get("DROVER_RELEASE_ROLE")
    source_sha = environ.get("DROVER_RELEASE_SHA")
    if (
        role != _ROLE
        or not isinstance(source_sha, str)
        or not _SHA_RE.fullmatch(source_sha)
    ):
        raise ValueError(_INVALID_IDENTITY)
    probe_path = environ.get("DROVER_STAGING_ATTESTATION_PATH")
    return ReleaseIdentity(
        role=role,
        source_sha=source_sha,
        package_version=__version__,
        staging_probe=_load_probe(probe_path, source_sha) if probe_path else None,
    )
