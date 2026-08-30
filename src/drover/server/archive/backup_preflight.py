"""Strictly local, fail-closed preparation for one Pond backup generation."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from drover.config import DroverConfig
from drover.server.archive.backup_config import BackupConfig
from drover.server.archive.backup_runtime import BackupRuntimeError, RuntimeGuard
from drover.server.archive.coverage import (
    CoverageReport,
    build_coverage_report,
    coverage_summary,
    load_registry_candidates,
)
from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    NativeInventory,
    SourceEligibilityReceipt,
    _open_nofollow_path,
    _pond_inventory_from_wire,
    _source_eligibility_receipt_from_wire,
    _write_private_json_at,
    canonical_private_json_bytes,
    private_json_sha256,
)
from drover.server.archive.native_inventory import (
    MAX_NATIVE_INVENTORY_RECORDS,
    discover_native_history_inventory,
    native_inventory_summary,
)
from drover.server.archive.pond_process import (
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
    _aggregate_resource_evidence,
    _PinnedPondExecutable,
    _process_resource_evidence,
    run_pond_process,
)
from drover.server.archive.pond_snapshot import (
    POND_INVENTORY_FILENAME,
    LocalPondStore,
    PondCorpusCounts,
    PondStoreSnapshot,
    _capture_pond_store_snapshot,
)
from drover.server.db import control_plane_path

_ERROR = "archive backup preflight failed"
_SOURCE_INVENTORY_FILENAME = "source-inventory.json"
_COVERAGE_REPORT_FILENAME = "coverage-report.json"
_REGISTRY_SNAPSHOT_FILENAME = "registry-snapshot.duckdb"
_ELIGIBILITY_DIRECTORY = "eligibility"
_MAX_ELIGIBILITY_RECEIPTS = 100_000
_MAX_DRY_RUN_ADAPTERS = 64
_MAX_REGISTRY_SNAPSHOT_BYTES = 256 * 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024
_DRY_RUN_ADAPTERS = frozenset({"claude-code", "codex-cli"})
_DRY_RUN_ROOT_FIELDS = frozenset({"dry_run", "adapters"})
_DRY_RUN_ADAPTER_FIELDS = frozenset({"name", "path", "sessions", "fresh", "pending"})
_COVERAGE_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_coverage",
        "current_source_coverage",
        "certified_coverage",
        "misses",
        "collisions",
        "unsupported_harness_sessions",
        "ready_for_next_writer",
    }
)
_MISS_FIELDS = frozenset(
    {"discovered_not_synced", "source_absent_after_prior_inventory", "unverifiable"}
)
_COLLISION_FIELDS = frozenset(
    {
        "duplicate_source_groups",
        "cross_harness_native_id_groups",
        "archive_logical_duplicate_candidate_groups",
        "archive_signature_unverifiable",
    }
)
_CURRENT_SOURCE_FIELDS = frozenset(
    {
        "discovered",
        "matched",
        "source_not_archive_eligible",
        "discovered_not_synced",
    }
)
_CANDIDATE_FIELDS = frozenset({"eligible", "matched", "percent", "by_harness"})
_HARNESS_FIELDS = frozenset({"eligible", "matched"})
_CERTIFIED_FIELDS = frozenset({"status", "certified"})


class BackupPreflightError(ValueError):
    """The one fixed public failure category for local preflight."""


def _failure() -> BackupPreflightError:
    return BackupPreflightError(_ERROR)


_NativeHome = Callable[[], Path]
_DiscoverNative = Callable[..., NativeInventory]
_CapturePond = Callable[..., PondStoreSnapshot]
_ResolveRegistry = Callable[[Path], Path]
_LoadRegistry = Callable[[Path], tuple[Any, ...]]
_BuildCoverage = Callable[..., CoverageReport]
_CoverageSummary = Callable[[CoverageReport], dict[str, object]]
_RunPond = Callable[..., PondProcessResult]


@dataclass(frozen=True, slots=True, repr=False)
class _PreflightDependencies:
    native_home: _NativeHome = field(repr=False)
    discover_native_inventory: _DiscoverNative = field(repr=False)
    capture_pond_snapshot: _CapturePond = field(repr=False)
    resolve_control_plane_path: _ResolveRegistry = field(repr=False)
    load_registry_candidates: _LoadRegistry = field(repr=False)
    build_coverage_report: _BuildCoverage = field(repr=False)
    coverage_summary: _CoverageSummary = field(repr=False)
    run_pond_process: _RunPond = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class BackupPreflightResult:
    source_inventory: NativeInventory = field(repr=False)
    pond_snapshot: PondStoreSnapshot = field(repr=False)
    coverage: CoverageReport = field(repr=False)
    coverage_summary: Mapping[str, object] = field(repr=False)
    source_inventory_path: Path = field(repr=False)
    pond_inventory_path: Path = field(repr=False)
    coverage_report_path: Path = field(repr=False)
    source_not_archive_eligible: int
    resource_evidence: PondResourceEvidence = field(
        default=PondResourceEvidence(0, None, 0),
        repr=False,
    )
    eligibility_receipts_sha256: str = field(
        default=private_json_sha256([]),
        repr=False,
    )


def _native_home() -> Path:
    return Path.home()


_PRODUCTION_DEPENDENCIES = _PreflightDependencies(
    native_home=_native_home,
    discover_native_inventory=discover_native_history_inventory,
    capture_pond_snapshot=_capture_pond_store_snapshot,
    resolve_control_plane_path=control_plane_path,
    load_registry_candidates=load_registry_candidates,
    build_coverage_report=build_coverage_report,
    coverage_summary=coverage_summary,
    run_pond_process=run_pond_process,
)


def run_backup_preflight(
    config: BackupConfig,
    drover_config: DroverConfig,
    workspace: Path,
    runtime_guard: RuntimeGuard,
    *,
    resource_limits: ResourceLimits | None = None,
) -> BackupPreflightResult:
    """Run preflight through fixed production dependencies and a real guard."""
    if type(runtime_guard) is not RuntimeGuard:
        raise _failure()
    return _run_backup_preflight(
        config,
        drover_config,
        workspace,
        runtime_guard,
        pond_executable=config.pond_binary,
        resource_limits=resource_limits,
        dependencies=_PRODUCTION_DEPENDENCIES,
    )


def _run_backup_preflight_at(
    config: BackupConfig,
    drover_config: DroverConfig,
    workspace: Path,
    runtime_guard: RuntimeGuard,
    *,
    receipt_directory_descriptor: int,
    receipt_directory_identity: tuple[int, ...],
    pond_executable: _PinnedPondExecutable,
    resource_limits: ResourceLimits | None = None,
) -> BackupPreflightResult:
    """Run applied preflight against one lock-owned receipt-root duplicate."""
    if (
        type(runtime_guard) is not RuntimeGuard
        or type(pond_executable) is not _PinnedPondExecutable
    ):
        raise _failure()
    return _run_backup_preflight(
        config,
        drover_config,
        workspace,
        runtime_guard,
        pond_executable=pond_executable,
        resource_limits=resource_limits,
        receipt_directory_descriptor=receipt_directory_descriptor,
        receipt_directory_identity=receipt_directory_identity,
        require_ready=True,
        dependencies=_PRODUCTION_DEPENDENCIES,
    )


def _run_backup_preflight(
    config: BackupConfig,
    drover_config: DroverConfig,
    workspace: Path,
    runtime_guard: RuntimeGuard,
    *,
    pond_executable: Path | _PinnedPondExecutable | None = None,
    resource_limits: ResourceLimits | None = None,
    receipt_directory_descriptor: int | None = None,
    receipt_directory_identity: tuple[int, ...] | None = None,
    require_ready: bool = False,
    dependencies: _PreflightDependencies,
) -> BackupPreflightResult:
    """Capture one local-only denominator and optionally require readiness."""
    workspace_descriptor: int | None = None
    config_descriptor: int | None = None
    store_descriptor: int | None = None
    try:
        if type(config) is not BackupConfig or type(drover_config) is not DroverConfig:
            raise _failure()
        if (
            type(runtime_guard) is not RuntimeGuard
            or type(dependencies) is not _PreflightDependencies
            or type(require_ready) is not bool
        ):
            raise _failure()
        deps = dependencies
        executable = config.pond_binary if pond_executable is None else pond_executable
        if not (
            isinstance(executable, Path) or type(executable) is _PinnedPondExecutable
        ):
            raise _failure()
        workspace_path, workspace_descriptor, workspace_identity = _pin_workspace(
            workspace
        )
        config_descriptor, config_identity = _pin_path(
            config.local_pond_config,
            directory=False,
            private=True,
        )
        store_descriptor, store_identity = _pin_path(
            config.local_store,
            directory=True,
            private=True,
        )
        host_id = runtime_guard.baseline_host_id()
        if not isinstance(host_id, str) or not host_id:
            raise _failure()
        source_inventory = deps.discover_native_inventory(
            deps.native_home(),
            host_id,
            max_records=MAX_NATIVE_INVENTORY_RECORDS,
        )
        _require_source_inventory(source_inventory, host_id)
        _write_workspace_json(
            workspace_descriptor,
            _SOURCE_INVENTORY_FILENAME,
            source_inventory.to_wire(),
        )
        receipts = _load_eligibility_receipts(
            config.receipt_directory,
            receipt_directory_descriptor=receipt_directory_descriptor,
            receipt_directory_identity=receipt_directory_identity,
        )
        _require_pinned_path(
            config.local_pond_config,
            config_descriptor,
            config_identity,
            directory=False,
        )
        _require_pinned_path(
            config.local_store,
            store_descriptor,
            store_identity,
            directory=True,
        )
        pond_snapshot = deps.capture_pond_snapshot(
            executable,
            storage=LocalPondStore(config.local_store),
            pond_config=config.local_pond_config,
            workspace=workspace_path,
            timeout_seconds=config.copy_timeout_seconds,
            progress_callback=runtime_guard.sample,
            resource_limits=resource_limits,
        )
        if type(pond_snapshot) is not PondStoreSnapshot:
            raise _failure()
        try:
            published_inventory = _pond_inventory_from_wire(
                _read_private_json_at(workspace_descriptor, POND_INVENTORY_FILENAME)
            )
        except (AttributeError, TypeError, ValueError):
            raise _failure() from None
        if published_inventory != pond_snapshot.root_inventory:
            raise _failure()

        registry_source = deps.resolve_control_plane_path(drover_config.duckdb_path)
        registry_path, registry_identity = _copy_registry_snapshot(
            Path(registry_source), workspace_descriptor, workspace_path
        )
        try:
            registry = deps.load_registry_candidates(registry_path)
        finally:
            _discard_registry_snapshot(workspace_descriptor, registry_identity)
        report = deps.build_coverage_report(
            registry,
            (source_inventory,),
            pond_snapshot.root_inventory,
            eligibility_receipts=receipts,
        )
        if type(report) is not CoverageReport:
            raise _failure()
        supplied_summary = deps.coverage_summary(report)
        canonical_summary = coverage_summary(report)
        if supplied_summary != canonical_summary:
            raise _failure()
        _require_coverage_valid(canonical_summary)
        summary = _freeze_mapping(canonical_summary)
        _require_snapshot_valid(pond_snapshot)
        _require_pinned_path(
            config.local_store,
            store_descriptor,
            store_identity,
            directory=True,
        )
        dry_run_evidence = _require_local_dry_run(
            config,
            workspace_path,
            workspace_descriptor,
            runtime_guard,
            deps,
            executable,
            resource_limits,
        )
        _write_workspace_json(
            workspace_descriptor,
            _COVERAGE_REPORT_FILENAME,
            report.to_wire(),
        )
        _require_workspace_same(workspace_path, workspace_identity)
        _require_pinned_path(
            config.local_pond_config,
            config_descriptor,
            config_identity,
            directory=False,
        )
        _require_pinned_path(
            config.local_store,
            store_descriptor,
            store_identity,
            directory=True,
        )
        result = BackupPreflightResult(
            source_inventory=source_inventory,
            pond_snapshot=pond_snapshot,
            coverage=report,
            coverage_summary=summary,
            source_inventory_path=workspace_path / _SOURCE_INVENTORY_FILENAME,
            pond_inventory_path=workspace_path / POND_INVENTORY_FILENAME,
            coverage_report_path=workspace_path / _COVERAGE_REPORT_FILENAME,
            source_not_archive_eligible=len(receipts),
            resource_evidence=_aggregate_resource_evidence(
                pond_snapshot.resource_evidence,
                dry_run_evidence,
            ),
            eligibility_receipts_sha256=_eligibility_receipts_sha256(receipts),
        )
        _require_result_valid(result)
        if require_ready and not _result_ready(result):
            raise _failure()
        return result
    except BackupPreflightError:
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


def backup_preflight_summary(result: BackupPreflightResult) -> dict[str, object]:
    """Return only aggregate, identifier-free preflight evidence."""
    try:
        _require_result_valid(result)
        canonical_coverage = coverage_summary(result.coverage)
        _require_coverage_valid(canonical_coverage)
        return {
            "schema_version": 1,
            "ready": _result_ready(result),
            "source_inventory": native_inventory_summary(result.source_inventory),
            "pond_corpus": result.pond_snapshot.counts.to_wire(),
            "coverage": canonical_coverage,
            "source_not_archive_eligible": result.source_not_archive_eligible,
        }
    except BackupPreflightError:
        raise
    except Exception:
        raise _failure() from None


def _require_source_inventory(inventory: NativeInventory, host_id: str) -> None:
    try:
        if (
            type(inventory) is not NativeInventory
            or inventory.schema_version != 2
            or inventory.host_id != host_id
        ):
            raise _failure()
        inventory.to_wire()
    except BackupPreflightError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _failure() from None


def _require_snapshot_valid(snapshot: PondStoreSnapshot) -> None:
    try:
        if type(snapshot) is not PondStoreSnapshot:
            raise _failure()
        snapshot.root_inventory.to_wire()
        if type(snapshot.counts) is not PondCorpusCounts:
            raise _failure()
        snapshot.counts.to_wire()
    except BackupPreflightError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _failure() from None


def _snapshot_ready(snapshot: PondStoreSnapshot) -> bool:
    _require_snapshot_valid(snapshot)
    counts = snapshot.counts
    return (
        counts.disallowed_sessions == 0
        and counts.logical_duplicate_groups == 0
        and counts.sessions_in_logical_duplicate_groups == 0
    )


def _require_coverage_valid(summary: Mapping[str, object]) -> None:
    if not isinstance(summary, dict) or set(summary) != _COVERAGE_ROOT_FIELDS:
        raise _failure()
    if type(summary["schema_version"]) is not int or summary["schema_version"] != 1:
        raise _failure()
    misses = summary["misses"]
    collisions = summary["collisions"]
    current = summary["current_source_coverage"]
    candidate = summary["candidate_coverage"]
    certified = summary["certified_coverage"]
    if (
        not isinstance(misses, dict)
        or set(misses) != _MISS_FIELDS
        or not isinstance(collisions, dict)
        or set(collisions) != _COLLISION_FIELDS
        or not isinstance(current, dict)
        or set(current) != _CURRENT_SOURCE_FIELDS
        or not isinstance(candidate, dict)
        or set(candidate) != _CANDIDATE_FIELDS
        or not isinstance(certified, dict)
        or set(certified) != _CERTIFIED_FIELDS
    ):
        raise _failure()
    for field in _MISS_FIELDS:
        _exact_nonnegative(misses[field])
    for field in _COLLISION_FIELDS:
        _exact_nonnegative(collisions[field])
    current_discovered = _exact_nonnegative(current["discovered"])
    current_matched = _exact_nonnegative(current["matched"])
    current_ineligible = _exact_nonnegative(current["source_not_archive_eligible"])
    current_not_synced = _exact_nonnegative(current["discovered_not_synced"])
    if current_discovered != current_matched + current_ineligible + current_not_synced:
        raise _failure()
    eligible = _exact_nonnegative(candidate["eligible"])
    matched = _exact_nonnegative(candidate["matched"])
    percent = candidate["percent"]
    by_harness = candidate["by_harness"]
    if (
        matched > eligible
        or type(percent) is not float
        or not math.isfinite(percent)
        or percent != (round(matched * 100 / eligible, 1) if eligible else 0.0)
        or not isinstance(by_harness, dict)
        or not set(by_harness) <= _DRY_RUN_ADAPTERS
    ):
        raise _failure()
    harness_eligible = 0
    harness_matched = 0
    for harness in by_harness.values():
        if not isinstance(harness, dict) or set(harness) != _HARNESS_FIELDS:
            raise _failure()
        eligible_count = _exact_nonnegative(harness["eligible"])
        matched_count = _exact_nonnegative(harness["matched"])
        harness_eligible += eligible_count
        harness_matched += matched_count
        if matched_count > eligible_count:
            raise _failure()
    if harness_eligible != eligible or harness_matched != matched:
        raise _failure()
    if (
        certified["status"] != "not_implemented"
        or _exact_nonnegative(certified["certified"]) != 0
    ):
        raise _failure()
    _exact_nonnegative(summary["unsupported_harness_sessions"])
    if type(summary["ready_for_next_writer"]) is not bool:
        raise _failure()


def _coverage_ready(summary: Mapping[str, object]) -> bool:
    _require_coverage_valid(summary)
    misses = summary["misses"]
    collisions = summary["collisions"]
    current = summary["current_source_coverage"]
    candidate = summary["candidate_coverage"]
    return bool(
        all(misses[field] == 0 for field in _MISS_FIELDS)
        and all(collisions[field] == 0 for field in _COLLISION_FIELDS)
        and current["discovered_not_synced"] == 0
        and candidate["matched"] == candidate["eligible"]
        and summary["unsupported_harness_sessions"] == 0
        and summary["ready_for_next_writer"] is True
    )


def _require_result_valid(result: BackupPreflightResult) -> None:
    if type(result) is not BackupPreflightResult:
        raise _failure()
    _require_source_inventory(result.source_inventory, result.source_inventory.host_id)
    _require_snapshot_valid(result.pond_snapshot)
    if (
        type(result.coverage) is not CoverageReport
        or type(result.resource_evidence) is not PondResourceEvidence
        or not isinstance(result.eligibility_receipts_sha256, str)
        or len(result.eligibility_receipts_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in result.eligibility_receipts_sha256
        )
        or isinstance(result.source_not_archive_eligible, bool)
        or not isinstance(result.source_not_archive_eligible, int)
        or result.source_not_archive_eligible < 0
    ):
        raise _failure()
    canonical_summary = coverage_summary(result.coverage)
    _require_coverage_valid(canonical_summary)
    if _thaw_mapping(result.coverage_summary) != canonical_summary:
        raise _failure()
    current = canonical_summary["current_source_coverage"]
    if (
        not isinstance(current, dict)
        or _exact_nonnegative(current["source_not_archive_eligible"])
        != result.source_not_archive_eligible
    ):
        raise _failure()
    paths = (
        result.source_inventory_path,
        result.pond_inventory_path,
        result.coverage_report_path,
    )
    if any(not isinstance(path, Path) for path in paths):
        raise _failure()


def _result_ready(result: BackupPreflightResult) -> bool:
    return _snapshot_ready(result.pond_snapshot) and _coverage_ready(
        coverage_summary(result.coverage)
    )


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            key: _freeze_mapping(item) if isinstance(item, dict) else item
            for key, item in value.items()
        }
    )


def _thaw_mapping(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _failure()
    return {
        key: _thaw_mapping(item) if isinstance(item, Mapping) else item
        for key, item in value.items()
    }


def _require_local_dry_run(
    config: BackupConfig,
    workspace: Path,
    workspace_descriptor: int,
    runtime_guard: RuntimeGuard,
    dependencies: _PreflightDependencies,
    pond_executable: Path | _PinnedPondExecutable,
    resource_limits: ResourceLimits | None,
) -> PondResourceEvidence:
    result = dependencies.run_pond_process(
        pond_executable,
        (
            "--config-file",
            str(config.local_pond_config),
            "--storage-path",
            str(config.local_store),
            "sync",
            "--dry-run",
            "--format",
            "json",
        ),
        timeout_seconds=float(config.copy_timeout_seconds),
        run_directory=workspace,
        label="local-dry-run",
        resource_limits=resource_limits,
        progress_callback=runtime_guard.sample,
    )
    if type(result) is not PondProcessResult or result.returncode != 0:
        raise _failure()
    expected_stdout = workspace / "local-dry-run.stdout"
    expected_stderr = workspace / "local-dry-run.stderr"
    if result.stdout_path != expected_stdout or result.stderr_path != expected_stderr:
        raise _failure()
    _read_private_file_at(workspace_descriptor, expected_stderr.name)
    data = _read_private_file_at(workspace_descriptor, result.stdout_path.name)
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _failure() from None
    if not isinstance(payload, dict) or set(payload) != _DRY_RUN_ROOT_FIELDS:
        raise _failure()
    if payload["dry_run"] is not True:
        raise _failure()
    adapters = payload["adapters"]
    if not isinstance(adapters, list) or len(adapters) > _MAX_DRY_RUN_ADAPTERS:
        raise _failure()
    seen: set[str] = set()
    for adapter in adapters:
        if not isinstance(adapter, dict) or set(adapter) != _DRY_RUN_ADAPTER_FIELDS:
            raise _failure()
        name = adapter["name"]
        path = adapter["path"]
        if (
            not isinstance(name, str)
            or name not in _DRY_RUN_ADAPTERS
            or name in seen
            or not isinstance(path, str)
            or not path
        ):
            raise _failure()
        seen.add(name)
        sessions = _exact_nonnegative(adapter["sessions"])
        fresh = _exact_nonnegative(adapter["fresh"])
        pending = _exact_nonnegative(adapter["pending"])
        if pending != 0 or fresh > sessions:
            raise _failure()
    if seen != _DRY_RUN_ADAPTERS:
        raise _failure()
    return _process_resource_evidence(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _exact_nonnegative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _failure()
    return value


def _descriptor_flags(*, directory: bool = False, write: bool = False) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if write else os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _receipt_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_ctime_ns,
    )


def _pin_workspace(path: Path) -> tuple[Path, int, tuple[int, ...]]:
    descriptor: int | None = None
    try:
        requested = Path(os.fspath(path))
        if not requested.is_absolute():
            raise _failure()
        if not requested.exists():
            requested.parent.resolve(strict=True)
            requested.mkdir(mode=0o700)
        if requested.resolve(strict=True) != requested:
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
    except BackupPreflightError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _failure() from None


def _pin_path(
    path: Path, *, directory: bool, private: bool
) -> tuple[int, tuple[int, ...]]:
    descriptor: int | None = None
    try:
        requested = Path(os.fspath(path))
        if not requested.is_absolute() or requested.resolve(strict=True) != requested:
            raise _failure()
        descriptor = _open_nofollow_path(
            requested, flags=_descriptor_flags(directory=directory)
        )
        metadata = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        private_mode = 0o700 if directory else 0o600
        if (
            not expected_type(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (private and stat.S_IMODE(metadata.st_mode) != private_mode)
        ):
            raise _failure()
        identity = (
            _directory_identity(metadata) if directory else _file_identity(metadata)
        )
        return descriptor, identity
    except BackupPreflightError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _failure() from None


def _require_pinned_path(
    path: Path,
    descriptor: int,
    expected: tuple[int, ...],
    *,
    directory: bool,
) -> None:
    try:
        current_descriptor = os.fstat(descriptor)
        current_path = path.stat(follow_symlinks=False)
        identity = _directory_identity if directory else _file_identity
        if (
            identity(current_descriptor) != expected
            or identity(current_path) != expected
        ):
            raise _failure()
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _require_workspace_same(path: Path, expected: tuple[int, ...]) -> None:
    try:
        if _directory_identity(path.stat(follow_symlinks=False)) != expected:
            raise _failure()
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _write_workspace_json(
    directory_descriptor: int, name: str, payload: object
) -> None:
    try:
        created = _write_private_json_at(directory_descriptor, name, payload)
        final = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            created.st_dev != final.st_dev
            or created.st_ino != final.st_ino
            or not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.geteuid()
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise _failure()
        os.fsync(directory_descriptor)
    except BackupPreflightError:
        raise
    except Exception:
        raise _failure() from None


def _load_eligibility_receipts(
    receipt_directory: Path,
    *,
    receipt_directory_descriptor: int | None = None,
    receipt_directory_identity: tuple[int, ...] | None = None,
) -> tuple[SourceEligibilityReceipt, ...]:
    root_descriptor: int | None = None
    close_root = False
    eligibility_descriptor: int | None = None
    try:
        if receipt_directory_descriptor is None:
            if receipt_directory_identity is not None:
                raise _failure()
            root_descriptor, _ = _pin_path(
                receipt_directory,
                directory=True,
                private=False,
            )
            close_root = True
        else:
            if (
                isinstance(receipt_directory_descriptor, bool)
                or not isinstance(receipt_directory_descriptor, int)
                or receipt_directory_descriptor < 0
                or type(receipt_directory_identity) is not tuple
            ):
                raise _failure()
            root_descriptor = receipt_directory_descriptor
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or (
                receipt_directory_identity is not None
                and _receipt_directory_identity(root_metadata)
                != receipt_directory_identity
            )
        ):
            raise _failure()
        root_content_identity = _file_identity(root_metadata)
        try:
            eligibility_descriptor = os.open(
                _ELIGIBILITY_DIRECTORY,
                _descriptor_flags(directory=True),
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            _require_unchanged_eligibility_root(
                receipt_directory,
                root_descriptor,
                root_content_identity,
                reopen_path=close_root,
            )
            return ()
        eligibility_metadata = os.fstat(eligibility_descriptor)
        if (
            not stat.S_ISDIR(eligibility_metadata.st_mode)
            or eligibility_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(eligibility_metadata.st_mode) != 0o700
        ):
            raise _failure()
        eligibility_identity = _file_identity(eligibility_metadata)
        names = os.listdir(eligibility_descriptor)
        if len(names) > _MAX_ELIGIBILITY_RECEIPTS:
            raise _failure()
        receipts: list[SourceEligibilityReceipt] = []
        for name in sorted(names):
            if (
                not isinstance(name, str)
                or not name.endswith(".json")
                or not name[:-5]
                or os.path.basename(name) != name
            ):
                raise _failure()
            payload = _read_private_json_at(eligibility_descriptor, name)
            try:
                receipt = _source_eligibility_receipt_from_wire(payload)
            except (AttributeError, TypeError, ValueError):
                raise _failure() from None
            receipts.append(receipt)
        current = os.stat(
            _ELIGIBILITY_DIRECTORY,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(os.fstat(eligibility_descriptor)) != eligibility_identity
            or _file_identity(current) != eligibility_identity
        ):
            raise _failure()
        _require_unchanged_eligibility_root(
            receipt_directory,
            root_descriptor,
            root_content_identity,
            reopen_path=close_root,
        )
        return tuple(receipts)
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None
    finally:
        descriptors = [eligibility_descriptor]
        if close_root:
            descriptors.append(root_descriptor)
        for descriptor in descriptors:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _require_unchanged_eligibility_root(
    path: Path,
    descriptor: int,
    expected: tuple[int, ...],
    *,
    reopen_path: bool,
) -> None:
    try:
        if _file_identity(os.fstat(descriptor)) != expected:
            raise _failure()
        if reopen_path and _file_identity(path.stat(follow_symlinks=False)) != expected:
            raise _failure()
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _eligibility_receipts_sha256(
    receipts: tuple[SourceEligibilityReceipt, ...],
) -> str:
    try:
        wires = [receipt.to_wire() for receipt in receipts]
        ordered = sorted(wires, key=canonical_private_json_bytes)
        return private_json_sha256(ordered)
    except (AttributeError, TypeError, ValueError):
        raise _failure() from None


def _read_private_json_at(directory_descriptor: int, name: str) -> object:
    data = _read_private_file_at(directory_descriptor, name)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _failure() from None


def _read_private_file_at(directory_descriptor: int, name: str) -> bytes:
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
            or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(final)
        ):
            raise _failure()
        return data
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _copy_registry_snapshot(
    source: Path, workspace_descriptor: int, workspace: Path
) -> tuple[Path, tuple[int, int]]:
    parent_descriptor: int | None = None
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    completed = False
    try:
        if not source.is_absolute() or source.resolve(strict=True) != source:
            raise _failure()
        parent = source.parent
        parent_descriptor, parent_identity = _pin_path(
            parent, directory=True, private=False
        )
        source_descriptor = os.open(
            source.name,
            _descriptor_flags(),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(source_descriptor)
        before_path = os.stat(
            source.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > _MAX_REGISTRY_SNAPSHOT_BYTES
            or _file_identity(before) != _file_identity(before_path)
        ):
            raise _failure()
        destination_descriptor = os.open(
            _REGISTRY_SNAPSHOT_FILENAME,
            _descriptor_flags(write=True),
            0o600,
            dir_fd=workspace_descriptor,
        )
        os.fchmod(destination_descriptor, 0o600)
        copied = 0
        while True:
            chunk = os.read(source_descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > _MAX_REGISTRY_SNAPSHOT_BYTES:
                raise _failure()
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise _failure()
                view = view[written:]
        after = os.fstat(source_descriptor)
        final_source = os.stat(
            source.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        destination = os.fstat(destination_descriptor)
        final = os.stat(
            _REGISTRY_SNAPSHOT_FILENAME,
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(final_source)
            or copied != before.st_size
            or destination.st_dev != final.st_dev
            or destination.st_ino != final.st_ino
            or destination.st_size != copied
            or not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.geteuid()
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise _failure()
        _require_pinned_path(
            parent,
            parent_descriptor,
            parent_identity,
            directory=True,
        )
        os.fsync(destination_descriptor)
        os.fsync(workspace_descriptor)
        completed = True
        return workspace / _REGISTRY_SNAPSHOT_FILENAME, (
            destination.st_dev,
            destination.st_ino,
        )
    except BackupPreflightError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _failure() from None
    finally:
        if destination_descriptor is not None and not completed:
            _discard_partial_registry_snapshot(
                workspace_descriptor, destination_descriptor
            )
        for descriptor in (
            destination_descriptor,
            source_descriptor,
            parent_descriptor,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _require_unchanged_directory(
    path: Path, descriptor: int, expected: tuple[int, ...]
) -> None:
    try:
        if (
            _file_identity(os.fstat(descriptor)) != expected
            or _file_identity(path.stat(follow_symlinks=False)) != expected
        ):
            raise _failure()
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None


def _discard_partial_registry_snapshot(
    workspace_descriptor: int, destination_descriptor: int
) -> None:
    try:
        opened = os.fstat(destination_descriptor)
        current = os.stat(
            _REGISTRY_SNAPSHOT_FILENAME,
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        if (
            opened.st_dev == current.st_dev
            and opened.st_ino == current.st_ino
            and stat.S_ISREG(current.st_mode)
            and current.st_uid == os.geteuid()
            and stat.S_IMODE(current.st_mode) == 0o600
        ):
            os.unlink(_REGISTRY_SNAPSHOT_FILENAME, dir_fd=workspace_descriptor)
            os.fsync(workspace_descriptor)
    except OSError:
        pass


def _discard_registry_snapshot(
    workspace_descriptor: int, expected: tuple[int, int]
) -> None:
    try:
        metadata = os.stat(
            _REGISTRY_SNAPSHOT_FILENAME,
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        if (
            (metadata.st_dev, metadata.st_ino) != expected
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise _failure()
        os.unlink(_REGISTRY_SNAPSHOT_FILENAME, dir_fd=workspace_descriptor)
        os.fsync(workspace_descriptor)
    except BackupPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise _failure() from None
