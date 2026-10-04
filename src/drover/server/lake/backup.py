"""Fail-closed immutable lake backup generations and isolated restore drills.

This module deliberately operates on an already-isolated staging copy. It never
loads hub configuration, opens a catalog DSN, or accepts an existing destination.
Catalog export is supplied by the operator as a separately-created snapshot file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .runtime import LakeError, sha256_file

STAGING_MARKER = ".drover-isolated-staging-copy"
FORMAT_VERSION = 1


def _fail(code: str):
    raise LakeError(code)


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _require_staging(root: Path) -> Path:
    root = _resolve(root)
    if not root.is_dir() or root.is_symlink():
        _fail("lake_backup_staging_root_required")
    marker = root / STAGING_MARKER
    if not marker.is_file() or marker.is_symlink():
        _fail("lake_backup_isolated_staging_marker_required")
    if marker.read_text(encoding="utf-8").strip() != "DROVER_ISOLATED_STAGING_COPY":
        _fail("lake_backup_invalid_staging_marker")
    if not (root / "verification" / "report.json").is_file():
        _fail("lake_backup_verification_report_required")
    return root


def _require_snapshot(path: Path) -> Path:
    path = _resolve(path)
    if not path.is_file() or path.is_symlink() or not path.stat().st_size:
        _fail("lake_backup_catalog_snapshot_required")
    return path


def _new_destination(path: Path, code: str) -> Path:
    path = _resolve(path)
    if path.exists() or path.is_symlink():
        _fail(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("lake_backup_symlink_not_allowed")
        if path.is_file() and path.name != STAGING_MARKER:
            yield path


def _parquet_rows(path: Path) -> int:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        _fail("lake_backup_parquet_verifier_unavailable")
    return pq.ParquetFile(path).metadata.num_rows


def _row_counts(root: Path, report: dict) -> dict[str, int]:
    """Recount parquet metadata and bind it to the rebuild evidence report."""
    counts = {
        "agent_events": 0,
        "agent_events_legacy_metadata": 0,
        "provider_usage_snapshots": 0,
        "control_outbox_batches": 0,
    }
    for path in root.joinpath("partitions").rglob("*.parquet"):
        name = path.name
        if name == "events.parquet":
            table = "agent_events"
        elif name == "metadata.parquet":
            table = "agent_events_legacy_metadata"
        elif name == "rows.parquet" and path.parents[1].name in counts:
            table = path.parents[1].name
        else:
            _fail("lake_backup_unexpected_partition_file")
        counts[table] += _parquet_rows(path)
    expected = {
        "agent_events": report.get("canonical_agent_events", {}).get("rows"),
        "agent_events_legacy_metadata": report.get("legacy_metadata", {}).get("rows"),
        "provider_usage_snapshots": report.get("raw", {})
        .get("provider_usage_snapshots", {})
        .get("rows"),
        "control_outbox_batches": report.get("raw", {})
        .get("control_outbox_batches", {})
        .get("rows"),
    }
    if (
        any(not isinstance(value, int) for value in expected.values())
        or counts != expected
    ):
        _fail("lake_backup_row_count_mismatch")
    return counts


def _copy(source: Path, destination: Path) -> dict:
    files = []
    for path in _files(source):
        rel = path.relative_to(source)
        target = destination / "data" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if sha256_file(path) != sha256_file(target):
            _fail("lake_backup_copy_checksum_mismatch")
        files.append(
            {
                "path": str(Path("data") / rel),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(target),
            }
        )
    return {"files": files}


def create_backup(
    staging_root: Path,
    catalog_snapshot: Path,
    backup_root: Path,
    *,
    generation_id: str | None = None,
) -> dict:
    staging_root = _require_staging(staging_root)
    catalog_snapshot = _require_snapshot(catalog_snapshot)
    report = json.loads((staging_root / "verification" / "report.json").read_text())
    counts = _row_counts(staging_root, report)
    generation_id = (
        generation_id or f"lake-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}"
    )
    if not generation_id.replace("-", "").isalnum():
        _fail("lake_backup_invalid_generation_id")
    generation = _resolve(backup_root) / generation_id
    if generation.is_relative_to(staging_root) or catalog_snapshot.is_relative_to(
        staging_root
    ):
        _fail("lake_backup_sources_must_be_separate")
    generation = _new_destination(generation, "lake_backup_generation_exists")
    generation.mkdir(mode=0o700)
    manifest = _copy(staging_root, generation)
    catalog = generation / "catalog" / "catalog.snapshot"
    catalog.parent.mkdir()
    shutil.copyfile(catalog_snapshot, catalog)
    manifest["files"].append(
        {
            "path": "catalog/catalog.snapshot",
            "bytes": catalog.stat().st_size,
            "sha256": sha256_file(catalog),
        }
    )
    manifest["files"].sort(key=lambda item: item["path"])
    manifest["format_version"] = FORMAT_VERSION
    manifest["row_counts"] = counts
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    (generation / "manifest.json").write_bytes(manifest_bytes)
    receipt = {
        "format_version": FORMAT_VERSION,
        "generation_id": generation_id,
        "created_at": datetime.now(UTC).isoformat(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "row_counts": counts,
        "verified": True,
    }
    (generation / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def _load_generation(generation: Path) -> tuple[dict, dict]:
    generation = _resolve(generation)
    try:
        manifest_bytes = (generation / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        receipt = json.loads((generation / "receipt.json").read_text())
    except (OSError, json.JSONDecodeError):
        _fail("lake_restore_receipt_required")
    if (
        receipt.get("format_version") != FORMAT_VERSION
        or not receipt.get("verified")
        or receipt.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        _fail("lake_restore_receipt_invalid")
    if manifest.get("format_version") != FORMAT_VERSION:
        _fail("lake_restore_manifest_invalid")
    expected = {item.get("path"): item for item in manifest.get("files", [])}
    if not expected or len(expected) != len(manifest.get("files", [])):
        _fail("lake_restore_manifest_invalid")
    for rel, item in expected.items():
        path = generation / rel
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != item.get("bytes")
            or sha256_file(path) != item.get("sha256")
        ):
            _fail("lake_restore_artifact_verification_failed")
    return manifest, receipt


def restore_backup(
    generation: Path, data_root: Path, catalog_destination: Path
) -> dict:
    manifest, receipt = _load_generation(generation)
    data_root = _new_destination(data_root, "lake_restore_requires_new_data_root")
    catalog_destination = _new_destination(
        catalog_destination, "lake_restore_requires_new_catalog_destination"
    )
    if (
        data_root == catalog_destination
        or data_root in catalog_destination.parents
        or catalog_destination in data_root.parents
    ):
        _fail("lake_restore_destinations_must_be_separate")
    data_root.mkdir(mode=0o700)
    for item in manifest["files"]:
        rel = Path(item["path"])
        if rel.parts[0] != "data":
            continue
        target = data_root / Path(*rel.parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_resolve(generation) / rel, target)
    source_catalog = _resolve(generation) / "catalog" / "catalog.snapshot"
    catalog_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_catalog, catalog_destination)
    report = json.loads((data_root / "verification" / "report.json").read_text())
    counts = _row_counts(data_root, report)
    if counts != manifest.get("row_counts") or counts != receipt.get("row_counts"):
        _fail("lake_restore_row_count_mismatch")
    return {
        "generation_id": receipt["generation_id"],
        "row_counts": counts,
        "verified": True,
    }
