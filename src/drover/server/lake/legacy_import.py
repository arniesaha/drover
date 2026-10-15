"""Retired unreceipted legacy importer.

Kept as a compatibility entry point so old operator invocations fail closed
instead of reintroducing history that current verification cannot prove.
"""

from __future__ import annotations

from pathlib import Path

from .runtime import LakeError, LakeSpec


def run_import(
    spec: LakeSpec,
    legacy_root: Path,
    *,
    since: str | None = None,
    session_id: str | None = None,
) -> dict[str, int | str | None]:
    """Refuse the retired importer before it opens or mutates a lake."""
    raise LakeError("lake_legacy_import_deprecated")
