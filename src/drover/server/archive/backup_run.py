"""One immutable, verified Pond backup generation transaction."""

from __future__ import annotations

import json
import math
import os
import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from drover.config import DroverConfig
from drover.server.archive.backup_config import (
    BackupConfig,
    generation_storage_url,
)
from drover.server.archive.backup_preflight import (
    BackupPreflightResult,
    backup_preflight_summary,
    run_backup_preflight,
)
from drover.server.archive.backup_receipt import (
    BackupReceipt,
    CollisionCounts,
    _latest_backup_receipt_at,
    _write_backup_receipt_at,
    backup_receipt_summary,
)
from drover.server.archive.backup_runtime import (
    BackupLock,
    BackupRuntimeError,
    RuntimeEvidence,
    RuntimeGuard,
)
from drover.server.archive.inventory import (
    MAX_INVENTORY_BYTES,
    _open_nofollow_path,
    canonical_private_json_bytes,
    private_json_sha256,
)
from drover.server.archive.pond_inventory import POND_VERSION
from drover.server.archive.pond_process import (
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
)
from drover.server.archive.pond_process import (
    _aggregate_resource_evidence as _pond_aggregate_resource_evidence,
)
from drover.server.archive.pond_process import (
    _process_resource_evidence as _pond_process_resource_evidence,
)
from drover.server.archive.pond_process import (
    run_pond_process,
)
from drover.server.archive.pond_snapshot import (
    POND_INVENTORY_FILENAME,
    LocalPondStore,
    PondStoreSnapshot,
    RemotePondGeneration,
    capture_pond_store_snapshot,
    pond_inventory_content_sha256,
)

_PREFLIGHT_ERROR = "archive backup preflight failed"
_LOCAL_CHANGED_ERROR = "archive backup local changed"
_STORAGE_ERROR = "archive backup storage unavailable"
_COPY_ERROR = "archive backup copy failed"
_VERIFY_ERROR = "archive backup verify failed"
_RECEIPT_ERROR = "archive backup receipt failed"
_RESOURCE_ERROR = "archive backup resource limit"
_ERRORS = frozenset(
    {
        _PREFLIGHT_ERROR,
        _LOCAL_CHANGED_ERROR,
        _STORAGE_ERROR,
        _COPY_ERROR,
        _VERIFY_ERROR,
        _RECEIPT_ERROR,
        _RESOURCE_ERROR,
    }
)
_RUNS_DIRECTORY = ".backup-runs"
_PHASE_DIRECTORIES = (
    "before",
    "storage",
    "remote-empty",
    "copy",
    "verify",
    "after",
    "remote-after",
)
_STORAGE_REQUIRED_FIELDS = frozenset({"ok", "exit_code", "failure"})


class BackupRunError(ValueError):
    """One fixed public backup-run failure category."""

    def __init__(self, category: str) -> None:
        safe = category if category in _ERRORS else _PREFLIGHT_ERROR
        super().__init__(safe)


class _Runtime(Protocol):
    def capture_baseline(self) -> None: ...

    def sample(self) -> None: ...

    def finish(self) -> RuntimeEvidence: ...


class _BackupLockFactory(Protocol):
    def __call__(
        self, receipt_directory: Path
    ) -> AbstractContextManager[_HeldBackupLock]: ...


class _HeldBackupLock(Protocol):
    def _duplicate_receipt_directory(self) -> tuple[int, tuple[int, ...]]: ...


class _RuntimeFactory(Protocol):
    def __call__(self, config: DroverConfig) -> _Runtime: ...


class _Preflight(Protocol):
    def __call__(
        self,
        config: BackupConfig,
        drover_config: DroverConfig,
        workspace: Path,
        runtime_guard: _Runtime,
        *,
        resource_limits: ResourceLimits,
    ) -> BackupPreflightResult: ...


class _RunPond(Protocol):
    def __call__(
        self,
        binary: Path,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        run_directory: Path,
        label: str,
        env: Mapping[str, str] | None = None,
        artifact_path: Path | None = None,
        resource_limits: ResourceLimits | None = None,
        progress_callback: Callable[[], None] | None = None,
    ) -> PondProcessResult: ...


class _CaptureSnapshot(Protocol):
    def __call__(
        self,
        binary: Path,
        *,
        storage: LocalPondStore | RemotePondGeneration,
        pond_config: Path,
        workspace: Path,
        timeout_seconds: float,
        progress_callback: Callable[[], None] | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> PondStoreSnapshot: ...


class _LatestReceipt(Protocol):
    def __call__(
        self,
        receipt_directory: Path,
        receipt_descriptor: int,
        receipt_identity: tuple[int, ...],
        store_scope_id: str,
    ) -> BackupReceipt | None: ...


class _WriteReceipt(Protocol):
    def __call__(
        self,
        receipt_directory: Path,
        receipt_descriptor: int,
        receipt_identity: tuple[int, ...],
        receipt: BackupReceipt,
        *,
        before_publish: Callable[[], None],
    ) -> Path: ...


@dataclass(frozen=True, slots=True, repr=False)
class _BackupDependencies:
    backup_lock: _BackupLockFactory = field(repr=False)
    uuid4: Callable[[], UUID] = field(repr=False)
    runtime_guard: _RuntimeFactory = field(repr=False)
    run_preflight: _Preflight = field(repr=False)
    run_pond_process: _RunPond = field(repr=False)
    capture_pond_snapshot: _CaptureSnapshot = field(repr=False)
    latest_receipt: _LatestReceipt = field(repr=False)
    write_receipt: _WriteReceipt = field(repr=False)
    now: Callable[[], datetime] = field(repr=False)


def _production_backup_dependencies() -> _BackupDependencies:
    """Construct fixed production callbacks without contacting any service."""
    return _BackupDependencies(
        backup_lock=BackupLock,
        uuid4=uuid4,
        runtime_guard=RuntimeGuard,
        run_preflight=run_backup_preflight,
        run_pond_process=run_pond_process,
        capture_pond_snapshot=capture_pond_store_snapshot,
        latest_receipt=_latest_backup_receipt_at,
        write_receipt=_write_backup_receipt_at,
        now=lambda: datetime.now(timezone.utc),
    )


def run_backup(config: BackupConfig, drover_config: DroverConfig) -> BackupReceipt:
    """Run one applied backup using only fixed, lazily built production wiring."""
    return _run_backup(
        config,
        drover_config,
        dependencies=_production_backup_dependencies(),
    )


def _run_backup(
    config: BackupConfig,
    drover_config: DroverConfig,
    *,
    dependencies: _BackupDependencies,
) -> BackupReceipt:
    """Private exact dependency seam for deterministic transaction tests."""
    if (
        type(config) is not BackupConfig
        or type(drover_config) is not DroverConfig
        or type(dependencies) is not _BackupDependencies
    ):
        raise BackupRunError(_PREFLIGHT_ERROR)
    try:
        lock = dependencies.backup_lock(config.receipt_directory)
        with lock as held_lock:
            receipt_descriptor, receipt_identity = (
                held_lock._duplicate_receipt_directory()
            )
            return _run_locked(
                config,
                drover_config,
                dependencies,
                receipt_descriptor=receipt_descriptor,
                receipt_identity=receipt_identity,
            )
    except BackupRunError:
        raise
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except Exception:
        raise BackupRunError(_PREFLIGHT_ERROR) from None


def _run_locked(
    config: BackupConfig,
    drover_config: DroverConfig,
    dependencies: _BackupDependencies,
    *,
    receipt_descriptor: int,
    receipt_identity: tuple[int, ...],
) -> BackupReceipt:
    try:
        generation_id = _new_generation_id(dependencies)
    except Exception:
        try:
            os.close(receipt_descriptor)
        except OSError:
            pass
        raise
    try:
        workspace_context = _PinnedRunWorkspace(
            config,
            generation_id,
            receipt_descriptor,
            receipt_identity,
        )
    except Exception:
        raise BackupRunError(_PREFLIGHT_ERROR) from None
    with workspace_context as workspace:
        runtime = _create_runtime(dependencies, drover_config)
        _capture_baseline(runtime)
        limits = _resource_limits(config)
        before = _preflight(
            dependencies,
            config,
            drover_config,
            workspace.phase("before"),
            runtime,
            limits,
            after_copy=False,
        )
        _validate_preflight_artifacts(before, _PREFLIGHT_ERROR)
        workspace.require_same()
        try:
            generation_url = generation_storage_url(config, generation_id)
            remote_generation = RemotePondGeneration(generation_url)
        except Exception:
            raise BackupRunError(_PREFLIGHT_ERROR) from None
        storage_result = _storage_check(
            dependencies,
            config,
            generation_url,
            workspace.phase("storage"),
            runtime,
            limits,
        )
        workspace.require_same()
        empty = _capture_remote_snapshot(
            dependencies,
            config,
            remote_generation,
            workspace.phase("remote-empty"),
            runtime,
            limits,
            empty=True,
        )
        _require_empty_generation(empty)
        workspace.require_same()
        copy_result = _copy(
            dependencies,
            config,
            generation_url,
            workspace.phase("copy"),
            runtime,
            limits,
        )
        workspace.require_same()
        verify_result = _verify(
            dependencies,
            config,
            generation_url,
            workspace.phase("verify"),
            runtime,
            limits,
        )
        workspace.require_same()
        after = _preflight(
            dependencies,
            config,
            drover_config,
            workspace.phase("after"),
            runtime,
            limits,
            after_copy=True,
        )
        workspace.require_same()
        remote = _capture_remote_snapshot(
            dependencies,
            config,
            remote_generation,
            workspace.phase("remote-after"),
            runtime,
            limits,
            empty=False,
        )
        workspace.require_same()
        _require_unchanged_local(before, after)
        _require_exact_remote(after.pond_snapshot, remote)
        source_digest, coverage_digest = _postflight_artifact_digests(after)
        remote_digest = _snapshot_artifact_digest(
            remote,
            workspace.phase("remote-after") / POND_INVENTORY_FILENAME,
            _VERIFY_ERROR,
        )
        local_digest = pond_inventory_content_sha256(after.pond_snapshot.root_inventory)
        runtime_evidence = _finish_runtime(runtime)
        resource_evidence = _aggregate_resource_evidence(
            config,
            before.resource_evidence,
            _pond_process_resource_evidence(storage_result),
            empty.resource_evidence,
            _pond_process_resource_evidence(copy_result),
            _pond_process_resource_evidence(verify_result),
            after.resource_evidence,
            remote.resource_evidence,
        )
        workspace.require_same()
        receipt, predecessor_digest = _build_verified_receipt(
            dependencies,
            config,
            workspace,
            generation_id,
            after,
            source_digest=source_digest,
            local_digest=local_digest,
            remote_digest=remote_digest,
            coverage_digest=coverage_digest,
            copy_result=copy_result,
            verify_result=verify_result,
            runtime_evidence=runtime_evidence,
            resource_evidence=resource_evidence,
        )
        workspace.require_same()
        _require_predecessor_unchanged(
            dependencies,
            config,
            workspace,
            predecessor_digest,
        )
        workspace.require_same()
        _write_verified_receipt(dependencies, config, workspace, receipt)
        return receipt


def backup_run_summary(receipt: BackupReceipt) -> dict[str, Any]:
    """Return only the aggregate receipt summary safe for operator output."""
    try:
        return backup_receipt_summary(receipt)
    except Exception:
        raise BackupRunError(_RECEIPT_ERROR) from None


def _new_generation_id(dependencies: _BackupDependencies) -> UUID:
    try:
        generation_id = dependencies.uuid4()
        if type(generation_id) is not UUID or generation_id.version != 4:
            raise ValueError
        return generation_id
    except Exception:
        raise BackupRunError(_PREFLIGHT_ERROR) from None


def _create_runtime(
    dependencies: _BackupDependencies, drover_config: DroverConfig
) -> _Runtime:
    try:
        runtime = dependencies.runtime_guard(drover_config)
        if not all(
            callable(getattr(runtime, method, None))
            for method in ("capture_baseline", "sample", "finish")
        ):
            raise ValueError
        return runtime
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except Exception:
        raise BackupRunError(_PREFLIGHT_ERROR) from None


def _capture_baseline(runtime: _Runtime) -> None:
    try:
        runtime.capture_baseline()
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except Exception:
        raise BackupRunError(_PREFLIGHT_ERROR) from None


def _preflight(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    drover_config: DroverConfig,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
    *,
    after_copy: bool,
) -> BackupPreflightResult:
    category = _LOCAL_CHANGED_ERROR if after_copy else _PREFLIGHT_ERROR
    try:
        result = dependencies.run_preflight(
            config,
            drover_config,
            workspace,
            runtime,
            resource_limits=limits,
        )
        if type(result) is not BackupPreflightResult:
            raise ValueError
        backup_preflight_summary(result)
        return result
    except BackupRunError:
        raise
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except PondProcessError as error:
        mapped = _RESOURCE_ERROR if error.category == "resource" else category
        raise BackupRunError(mapped) from None
    except Exception:
        raise BackupRunError(category) from None


def _resource_limits(config: BackupConfig) -> ResourceLimits:
    try:
        return ResourceLimits(
            config.max_rss_bytes,
            config.max_physical_bytes,
            config.max_swap_growth_bytes,
        )
    except Exception:
        raise BackupRunError(_RESOURCE_ERROR) from None


def _storage_check(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    generation_url: str,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
) -> PondProcessResult:
    try:
        result = dependencies.run_pond_process(
            config.pond_binary,
            (
                "--config-file",
                str(config.remote_pond_config),
                "storage",
                "check",
                generation_url,
                "--format",
                "json",
            ),
            timeout_seconds=float(config.copy_timeout_seconds),
            run_directory=workspace,
            label="storage-check",
            resource_limits=limits,
            progress_callback=runtime.sample,
        )
        _require_process_result(result, workspace, "storage-check")
        if result.returncode != 0:
            raise ValueError
        payload = _read_private_json_document(result.stdout_path)
        if (
            not isinstance(payload, dict)
            or not _STORAGE_REQUIRED_FIELDS <= set(payload)
            or payload["ok"] is not True
            or type(payload["exit_code"]) is not int
            or payload["exit_code"] != 0
            or payload["failure"] is not None
        ):
            raise ValueError
        return result
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except PondProcessError as error:
        category = _RESOURCE_ERROR if error.category == "resource" else _STORAGE_ERROR
        raise BackupRunError(category) from None
    except BackupRunError:
        raise
    except Exception:
        raise BackupRunError(_STORAGE_ERROR) from None


def _capture_remote_snapshot(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    storage: RemotePondGeneration,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
    *,
    empty: bool,
) -> PondStoreSnapshot:
    category = _STORAGE_ERROR if empty else _VERIFY_ERROR
    try:
        snapshot = dependencies.capture_pond_snapshot(
            config.pond_binary,
            storage=storage,
            pond_config=config.remote_pond_config,
            workspace=workspace,
            timeout_seconds=config.copy_timeout_seconds,
            progress_callback=runtime.sample,
            resource_limits=limits,
        )
        if type(snapshot) is not PondStoreSnapshot:
            raise ValueError
        snapshot.root_inventory.to_wire()
        snapshot.counts.to_wire()
        _snapshot_artifact_digest(
            snapshot,
            workspace / POND_INVENTORY_FILENAME,
            category,
        )
        return snapshot
    except BackupRunError:
        raise
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except PondProcessError as error:
        mapped = _RESOURCE_ERROR if error.category == "resource" else category
        raise BackupRunError(mapped) from None
    except Exception:
        raise BackupRunError(category) from None


def _require_empty_generation(snapshot: PondStoreSnapshot) -> None:
    try:
        if snapshot.root_inventory.records or any(
            value != 0 for value in snapshot.counts.to_wire().values()
        ):
            raise ValueError
    except Exception:
        raise BackupRunError(_STORAGE_ERROR) from None


def _copy(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    generation_url: str,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
) -> PondProcessResult:
    return _copy_command(
        dependencies,
        config,
        generation_url,
        workspace,
        runtime,
        limits,
        verify=False,
    )


def _verify(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    generation_url: str,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
) -> PondProcessResult:
    return _copy_command(
        dependencies,
        config,
        generation_url,
        workspace,
        runtime,
        limits,
        verify=True,
    )


def _copy_command(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    generation_url: str,
    workspace: Path,
    runtime: _Runtime,
    limits: ResourceLimits,
    *,
    verify: bool,
) -> PondProcessResult:
    label = "verify-only" if verify else "copy"
    category = _VERIFY_ERROR if verify else _COPY_ERROR
    command = [
        "--config-file",
        str(config.remote_pond_config),
        "copy",
    ]
    if verify:
        command.append("--verify-only")
    command.extend(
        (
            "--from",
            str(config.local_store),
            "--to",
            generation_url,
        )
    )
    try:
        result = dependencies.run_pond_process(
            config.pond_binary,
            tuple(command),
            timeout_seconds=float(config.copy_timeout_seconds),
            run_directory=workspace,
            label=label,
            resource_limits=limits,
            progress_callback=runtime.sample,
        )
        _require_process_result(result, workspace, label)
        if result.returncode != 0:
            raise ValueError
        return result
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except PondProcessError as error:
        mapped = _RESOURCE_ERROR if error.category == "resource" else category
        raise BackupRunError(mapped) from None
    except BackupRunError:
        raise
    except Exception:
        raise BackupRunError(category) from None


def _require_process_result(
    result: PondProcessResult,
    workspace: Path,
    label: str,
) -> None:
    if type(result) is not PondProcessResult:
        raise ValueError
    integers = (
        result.returncode,
        result.duration_ms,
        result.peak_rss_bytes,
        result.swap_delta_bytes,
    )
    if any(type(value) is not int for value in integers):
        raise ValueError
    if (
        result.duration_ms < 0
        or result.peak_rss_bytes < 0
        or result.swap_delta_bytes < 0
        or (
            result.peak_physical_bytes is not None
            and (
                type(result.peak_physical_bytes) is not int
                or result.peak_physical_bytes < 0
            )
        )
        or result.stdout_path != workspace / f"{label}.stdout"
        or result.stderr_path != workspace / f"{label}.stderr"
    ):
        raise ValueError


def _require_unchanged_local(
    before: BackupPreflightResult,
    after: BackupPreflightResult,
) -> None:
    try:
        before_source = before.source_inventory.to_wire()
        after_source = after.source_inventory.to_wire()
        before_source.pop("captured_at")
        after_source.pop("captured_at")
        if (
            before_source != after_source
            or before.pond_snapshot.root_inventory.records
            != after.pond_snapshot.root_inventory.records
            or pond_inventory_content_sha256(before.pond_snapshot.root_inventory)
            != pond_inventory_content_sha256(after.pond_snapshot.root_inventory)
            or before.pond_snapshot.counts != after.pond_snapshot.counts
            or before.coverage.to_wire() != after.coverage.to_wire()
            or dict(before.coverage_summary) != dict(after.coverage_summary)
            or before.source_not_archive_eligible != after.source_not_archive_eligible
            or before.eligibility_receipts_sha256 != after.eligibility_receipts_sha256
        ):
            raise ValueError
    except Exception:
        raise BackupRunError(_LOCAL_CHANGED_ERROR) from None


def _require_exact_remote(
    local: PondStoreSnapshot,
    remote: PondStoreSnapshot,
) -> None:
    try:
        remote_counts = remote.counts
        if (
            remote.root_inventory.records != local.root_inventory.records
            or pond_inventory_content_sha256(remote.root_inventory)
            != pond_inventory_content_sha256(local.root_inventory)
            or remote_counts.sessions != local.counts.sessions
            or remote_counts.messages != local.counts.messages
            or remote_counts.parts != local.counts.parts
            or remote_counts.disallowed_sessions != 0
            or remote_counts.logical_duplicate_groups != 0
            or remote_counts.sessions_in_logical_duplicate_groups != 0
        ):
            raise ValueError
    except Exception:
        raise BackupRunError(_VERIFY_ERROR) from None


def _postflight_artifact_digests(
    result: BackupPreflightResult,
) -> tuple[str, str]:
    try:
        source_payload, coverage_payload = _validate_preflight_artifacts(
            result,
            _LOCAL_CHANGED_ERROR,
        )
        return (
            private_json_sha256(source_payload),
            private_json_sha256(coverage_payload),
        )
    except Exception:
        raise BackupRunError(_LOCAL_CHANGED_ERROR) from None


def _validate_preflight_artifacts(
    result: BackupPreflightResult,
    category: str,
) -> tuple[Any, Any]:
    try:
        source_payload = _read_private_json_document(
            result.source_inventory_path,
            require_canonical=True,
        )
        pond_payload = _read_private_json_document(
            result.pond_inventory_path,
            require_canonical=True,
        )
        coverage_payload = _read_private_json_document(
            result.coverage_report_path,
            require_canonical=True,
        )
        if (
            source_payload != result.source_inventory.to_wire()
            or pond_payload != result.pond_snapshot.root_inventory.to_wire()
            or coverage_payload != result.coverage.to_wire()
        ):
            raise ValueError
        return source_payload, coverage_payload
    except BackupRunError:
        raise
    except Exception:
        raise BackupRunError(category) from None


def _snapshot_artifact_digest(
    snapshot: PondStoreSnapshot,
    path: Path,
    category: str,
) -> str:
    try:
        payload = _read_private_json_document(path, require_canonical=True)
        if payload != snapshot.root_inventory.to_wire():
            raise ValueError
        return pond_inventory_content_sha256(snapshot.root_inventory)
    except BackupRunError:
        raise
    except Exception:
        raise BackupRunError(category) from None


def _finish_runtime(runtime: _Runtime) -> RuntimeEvidence:
    try:
        evidence = runtime.finish()
        if (
            type(evidence) is not RuntimeEvidence
            or type(evidence.health_samples) is not int
            or evidence.health_samples < 30
            or type(evidence.health_p95_ms) is not float
            or not math.isfinite(evidence.health_p95_ms)
            or not 0 <= evidence.health_p95_ms < 100
        ):
            raise ValueError
        return evidence
    except BackupRuntimeError as error:
        raise BackupRunError(_runtime_category(error)) from None
    except BackupRunError:
        raise
    except Exception:
        raise BackupRunError(_RESOURCE_ERROR) from None


def _aggregate_resource_evidence(
    config: BackupConfig,
    *values: PondResourceEvidence,
) -> tuple[int, int, int]:
    try:
        evidence = _pond_aggregate_resource_evidence(*values)
        if evidence.peak_physical_bytes is None:
            raise ValueError
        peak_rss = evidence.peak_rss_bytes
        peak_physical = evidence.peak_physical_bytes
        swap_delta = evidence.swap_delta_bytes
        if (
            peak_rss > config.max_rss_bytes
            or peak_physical > config.max_physical_bytes
            or swap_delta > config.max_swap_growth_bytes
        ):
            raise ValueError
        return peak_rss, peak_physical, swap_delta
    except Exception:
        raise BackupRunError(_RESOURCE_ERROR) from None


def _build_verified_receipt(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    workspace: _PinnedRunWorkspace,
    generation_id: UUID,
    after: BackupPreflightResult,
    *,
    source_digest: str,
    local_digest: str,
    remote_digest: str,
    coverage_digest: str,
    copy_result: PondProcessResult,
    verify_result: PondProcessResult,
    runtime_evidence: RuntimeEvidence,
    resource_evidence: tuple[int, int, int],
) -> tuple[BackupReceipt, str | None]:
    try:
        predecessor = dependencies.latest_receipt(
            *workspace.receipt_binding(),
            config.store_scope_id,
        )
        predecessor_digest = (
            private_json_sha256(predecessor.to_wire())
            if predecessor is not None
            else None
        )
        created_at = dependencies.now()
        if (
            type(created_at) is not datetime
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            raise ValueError
        collisions = CollisionCounts(
            len(after.coverage.duplicate_source_groups),
            len(after.coverage.cross_harness_native_id_groups),
            len(after.coverage.archive_logical_duplicate_candidate_groups),
            len(after.coverage.archive_signature_unverifiable),
        )
        peak_rss, peak_physical, swap_delta = resource_evidence
        receipt = BackupReceipt(
            schema_version=1,
            created_at=created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            pond_version=POND_VERSION,
            store_scope_id=config.store_scope_id,
            generation_id=str(generation_id),
            previous_receipt_sha256=predecessor_digest,
            source_inventory_sha256=source_digest,
            local_pond_inventory_sha256=local_digest,
            remote_pond_inventory_sha256=remote_digest,
            coverage_report_sha256=coverage_digest,
            sessions=after.pond_snapshot.counts.sessions,
            messages=after.pond_snapshot.counts.messages,
            parts=after.pond_snapshot.counts.parts,
            source_not_archive_eligible=after.source_not_archive_eligible,
            collision_counts=collisions,
            copy_duration_ms=copy_result.duration_ms,
            verify_duration_ms=verify_result.duration_ms,
            health_samples=runtime_evidence.health_samples,
            health_p95_ms=runtime_evidence.health_p95_ms,
            peak_rss_bytes=peak_rss,
            peak_physical_bytes=peak_physical,
            swap_delta_bytes=swap_delta,
        )
        receipt.to_wire()
        return receipt, predecessor_digest
    except Exception:
        raise BackupRunError(_RECEIPT_ERROR) from None


def _require_predecessor_unchanged(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    workspace: _PinnedRunWorkspace,
    expected_digest: str | None,
) -> None:
    try:
        current = dependencies.latest_receipt(
            *workspace.receipt_binding(),
            config.store_scope_id,
        )
        current_digest = (
            private_json_sha256(current.to_wire()) if current is not None else None
        )
        if current_digest != expected_digest:
            raise ValueError
    except Exception:
        raise BackupRunError(_RECEIPT_ERROR) from None


def _write_verified_receipt(
    dependencies: _BackupDependencies,
    config: BackupConfig,
    workspace: _PinnedRunWorkspace,
    receipt: BackupReceipt,
) -> None:
    try:
        dependencies.write_receipt(
            *workspace.receipt_binding(),
            receipt,
            before_publish=workspace.prepare_publish,
        )
    except Exception:
        raise BackupRunError(_RECEIPT_ERROR) from None


def _runtime_category(error: BackupRuntimeError) -> str:
    category = str(error)
    return category if category in _ERRORS else _PREFLIGHT_ERROR


def _read_private_json_document(
    path: Path,
    *,
    require_canonical: bool = False,
) -> Any:
    descriptor = -1
    try:
        descriptor = _open_nofollow_path(path)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_INVENTORY_BYTES
        ):
            raise ValueError
        data = os.read(descriptor, MAX_INVENTORY_BYTES + 1)
        after = os.fstat(descriptor)
        final = path.stat(follow_symlinks=False)
        if (
            len(data) > MAX_INVENTORY_BYTES
            or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(final)
        ):
            raise ValueError
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
        if require_canonical and data != canonical_private_json_bytes(payload):
            raise ValueError
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


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
        metadata.st_ctime_ns,
    )


def _directory_node(identity: tuple[int, ...]) -> tuple[int, ...]:
    return identity[:4]


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


class _PinnedRunWorkspace:
    __slots__ = (
        "_active_phase",
        "_config",
        "_generation_id",
        "_phase_descriptors",
        "_phase_identities",
        "_receipt_descriptor",
        "_receipt_identity",
        "_runs_descriptor",
        "_runs_identity",
        "_run_descriptor",
        "_run_identity",
        "path",
    )

    def __init__(
        self,
        config: BackupConfig,
        generation_id: UUID,
        receipt_descriptor: int,
        receipt_identity: tuple[int, ...],
    ) -> None:
        self._active_phase: str | None = None
        self._config = config
        self._generation_id = generation_id
        self._phase_descriptors: dict[str, int] = {}
        self._phase_identities: dict[str, tuple[int, ...]] = {}
        self._receipt_descriptor = receipt_descriptor
        self._receipt_identity: tuple[int, ...] | None = receipt_identity
        self._runs_descriptor = -1
        self._runs_identity: tuple[int, ...] | None = None
        self._run_descriptor = -1
        self._run_identity: tuple[int, ...] | None = None
        self.path = config.receipt_directory / _RUNS_DIRECTORY / str(generation_id)

    def __repr__(self) -> str:
        return "_PinnedRunWorkspace(private)"

    def __enter__(self) -> _PinnedRunWorkspace:
        try:
            receipt = self._config.receipt_directory
            if not receipt.is_absolute() or receipt.resolve(strict=True) != receipt:
                raise ValueError
            receipt_metadata = os.fstat(self._receipt_descriptor)
            if (
                self._receipt_identity is None
                or not _is_private_directory(receipt_metadata)
                or _directory_identity(receipt_metadata) != self._receipt_identity
            ):
                raise ValueError
            self._require_root(self._receipt_identity)
            self._runs_descriptor = _open_or_create_private_directory(
                self._receipt_descriptor,
                _RUNS_DIRECTORY,
                exclusive=False,
            )
            self._run_descriptor = _open_or_create_private_directory(
                self._runs_descriptor,
                str(self._generation_id),
                exclusive=True,
            )
            for name in _PHASE_DIRECTORIES:
                descriptor = _open_or_create_private_directory(
                    self._run_descriptor,
                    name,
                    exclusive=True,
                )
                self._phase_descriptors[name] = descriptor
            self._receipt_identity = _directory_identity(
                os.fstat(self._receipt_descriptor)
            )
            self._runs_identity = _directory_identity(os.fstat(self._runs_descriptor))
            self._run_identity = _directory_identity(os.fstat(self._run_descriptor))
            self._phase_identities = {
                name: _directory_identity(os.fstat(descriptor))
                for name, descriptor in self._phase_descriptors.items()
            }
            self.require_same()
            return self
        except Exception:
            self.close()
            raise BackupRunError(_PREFLIGHT_ERROR) from None

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def phase(self, name: str) -> Path:
        if name not in _PHASE_DIRECTORIES:
            raise BackupRunError(_PREFLIGHT_ERROR)
        self.require_same()
        self._active_phase = name
        return self.path / name

    def receipt_binding(self) -> tuple[Path, int, tuple[int, ...]]:
        self.require_same()
        if self._receipt_identity is None:
            raise BackupRunError(_RECEIPT_ERROR)
        return (
            self._config.receipt_directory,
            self._receipt_descriptor,
            self._receipt_identity,
        )

    def prepare_publish(self) -> None:
        """Accept only the writer's temp-entry ctime change, then check all paths."""
        try:
            if self._receipt_identity is None:
                raise ValueError
            descriptor_identity = _directory_identity(
                os.fstat(self._receipt_descriptor)
            )
            path_descriptor = _open_nofollow_path(
                self._config.receipt_directory,
                flags=_directory_flags(),
            )
            try:
                path_identity = _directory_identity(os.fstat(path_descriptor))
            finally:
                os.close(path_descriptor)
            if descriptor_identity != path_identity or _directory_node(
                descriptor_identity
            ) != _directory_node(self._receipt_identity):
                raise ValueError
            self._receipt_identity = descriptor_identity
            self.require_same()
        except BackupRunError:
            raise
        except Exception:
            raise BackupRunError(_RECEIPT_ERROR) from None

    def require_same(self) -> None:
        try:
            if (
                self._receipt_identity is None
                or self._runs_identity is None
                or self._run_identity is None
                or self._receipt_descriptor < 0
                or self._runs_descriptor < 0
                or self._run_descriptor < 0
                or set(self._phase_descriptors) != set(_PHASE_DIRECTORIES)
                or set(self._phase_identities) != set(_PHASE_DIRECTORIES)
            ):
                raise ValueError
            self._require_root(self._receipt_identity)
            _require_directory_entry(
                self._receipt_descriptor,
                _RUNS_DIRECTORY,
                self._runs_descriptor,
                self._runs_identity,
            )
            _require_directory_entry(
                self._runs_descriptor,
                str(self._generation_id),
                self._run_descriptor,
                self._run_identity,
            )
            for name in _PHASE_DIRECTORIES:
                current = _require_directory_entry(
                    self._run_descriptor,
                    name,
                    self._phase_descriptors[name],
                    self._phase_identities[name],
                    allow_ctime_change=name == self._active_phase,
                )
                if name == self._active_phase:
                    self._phase_identities[name] = current
            self._active_phase = None
        except BackupRunError:
            raise
        except Exception:
            raise BackupRunError(_RECEIPT_ERROR) from None

    def close(self) -> None:
        for descriptor in self._phase_descriptors.values():
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self._phase_descriptors.clear()
        self._phase_identities.clear()
        for name in (
            "_run_descriptor",
            "_runs_descriptor",
            "_receipt_descriptor",
        ):
            descriptor = getattr(self, name)
            setattr(self, name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _require_root(self, expected: tuple[int, ...]) -> None:
        descriptor_identity = _directory_identity(os.fstat(self._receipt_descriptor))
        path_descriptor = _open_nofollow_path(
            self._config.receipt_directory,
            flags=_directory_flags(),
        )
        try:
            path_identity = _directory_identity(os.fstat(path_descriptor))
        finally:
            os.close(path_descriptor)
        if descriptor_identity != expected or path_identity != expected:
            raise ValueError


def _require_directory_entry(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected: tuple[int, ...],
    *,
    allow_ctime_change: bool = False,
) -> tuple[int, ...]:
    opened = os.fstat(descriptor)
    lexical = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not _is_private_directory(opened) or not _is_private_directory(lexical):
        raise ValueError
    opened_identity = _directory_identity(opened)
    lexical_identity = _directory_identity(lexical)
    if opened_identity != lexical_identity:
        raise ValueError
    if allow_ctime_change:
        if _directory_node(opened_identity) != _directory_node(expected):
            raise ValueError
    elif opened_identity != expected:
        raise ValueError
    return opened_identity


def _open_or_create_private_directory(
    parent_descriptor: int,
    name: str,
    *,
    exclusive: bool,
) -> int:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or os.path.basename(name) != name
    ):
        raise ValueError
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        if exclusive:
            raise
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError
        if created:
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
        final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not _is_private_directory(metadata) or _directory_identity(
            metadata
        ) != _directory_identity(final):
            raise ValueError
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _is_private_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )
