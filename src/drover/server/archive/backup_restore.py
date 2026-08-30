"""Verified restoration of one private Pond backup generation."""

from __future__ import annotations

import ctypes
import json
import math
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from drover.config import DroverConfig
from drover.server.archive.backup_config import (
    BackupConfig,
    generation_storage_url,
)
from drover.server.archive.backup_receipt import (
    BackupReceipt,
    _rename_noreplace_at,
    load_backup_receipt_chain,
)
from drover.server.archive.backup_runtime import (
    BackupRuntimeError,
    RuntimeEvidence,
    RuntimeGuard,
)
from drover.server.archive.coverage import build_coverage_report, coverage_summary
from drover.server.archive.inventory import MAX_INVENTORY_BYTES, _open_nofollow_path
from drover.server.archive.native_inventory import (
    MAX_NATIVE_INVENTORY_RECORDS,
    discover_native_history_inventory,
)
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
    _pin_pond_executable,
    _PinnedPondExecutable,
)
from drover.server.archive.pond_process import (
    _process_resource_evidence as _pond_process_resource_evidence,
)
from drover.server.archive.pond_process import (
    run_pond_process,
)
from drover.server.archive.pond_snapshot import (
    LocalPondStore,
    PondStoreSnapshot,
    _capture_pond_release,
    _capture_pond_store_snapshot,
    _PondReleaseEvidence,
    pond_inventory_content_sha256,
)

_RESTORE_ERROR = "archive backup restore failed"
_RESOURCE_ERROR = "archive backup resource limit"
_ERRORS = frozenset({_RESTORE_ERROR, _RESOURCE_ERROR})
_PHASES = ("release", "copy", "verify", "snapshot")
_COVERAGE_OUTCOMES = frozenset({"current", "stale", "unavailable"})
_MAX_PATH_ANCESTORS = 1024
_DARWIN_MNT_LOCAL = 0x00001000
_LINUX_LOCAL_FILESYSTEMS = frozenset(
    {
        "apfs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "overlay",
        "tmpfs",
        "xfs",
        "zfs",
    }
)
_MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024


class BackupRestoreError(ValueError):
    """One fixed public restore failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category if category in _ERRORS else _RESTORE_ERROR)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    verified: bool
    sessions: int
    messages: int
    parts: int
    current_source_coverage: Literal["current", "stale", "unavailable"]
    health_samples: int
    health_p95_ms: float
    peak_rss_bytes: int
    peak_physical_bytes: int
    swap_delta_bytes: int

    def __post_init__(self) -> None:
        if self.verified is not True:
            raise BackupRestoreError(_RESTORE_ERROR)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.sessions,
                self.messages,
                self.parts,
                self.health_samples,
                self.peak_rss_bytes,
                self.peak_physical_bytes,
                self.swap_delta_bytes,
            )
        ):
            raise BackupRestoreError(_RESTORE_ERROR)
        if (
            self.current_source_coverage not in _COVERAGE_OUTCOMES
            or self.health_samples < 30
            or type(self.health_p95_ms) is not float
            or not math.isfinite(self.health_p95_ms)
            or not 0 <= self.health_p95_ms < 100
        ):
            raise BackupRestoreError(_RESTORE_ERROR)


class _Runtime(Protocol):
    def capture_baseline(self) -> None: ...

    def baseline_host_id(self) -> str: ...

    def sample(self) -> None: ...

    def finish(self) -> RuntimeEvidence: ...


class _LoadReceiptChain(Protocol):
    def __call__(
        self,
        receipt_path: str | os.PathLike[str],
        receipt_directory: str | os.PathLike[str],
    ) -> tuple[BackupReceipt, ...]: ...


class _RuntimeFactory(Protocol):
    def __call__(self, config: DroverConfig) -> _Runtime: ...


class _RunPond(Protocol):
    def __call__(
        self,
        binary: Path | _PinnedPondExecutable,
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
        binary: Path | _PinnedPondExecutable,
        *,
        storage: LocalPondStore,
        pond_config: Path,
        workspace: Path,
        timeout_seconds: float,
        progress_callback: Callable[[], None] | None = None,
        resource_limits: ResourceLimits | None = None,
        release_evidence: _PondReleaseEvidence | None = None,
    ) -> PondStoreSnapshot: ...


class _CaptureRelease(Protocol):
    def __call__(
        self,
        binary: Path | _PinnedPondExecutable,
        *,
        workspace: Path,
        progress_callback: Callable[[], None] | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> _PondReleaseEvidence: ...


class _CurrentSourceCoverage(Protocol):
    def __call__(
        self,
        snapshot: PondStoreSnapshot,
        receipt: BackupReceipt,
        drover_config: DroverConfig,
        host_id: str,
    ) -> Literal["current", "stale", "unavailable"]: ...


class _LocalFilesystemCheck(Protocol):
    def __call__(self, descriptor: int, path: Path) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class _RestoreDependencies:
    load_receipt_chain: _LoadReceiptChain = field(repr=False)
    runtime_guard: _RuntimeFactory = field(repr=False)
    run_pond_process: _RunPond = field(repr=False)
    pin_pond_executable: Callable[[Path], _PinnedPondExecutable] = field(repr=False)
    capture_pond_release: _CaptureRelease = field(repr=False)
    capture_pond_snapshot: _CaptureSnapshot = field(repr=False)
    current_source_coverage: _CurrentSourceCoverage = field(repr=False)
    workspace_uuid: Callable[[], UUID] = field(repr=False)
    is_local_filesystem: _LocalFilesystemCheck = field(repr=False)


def _production_restore_dependencies() -> _RestoreDependencies:
    """Build fixed production callbacks without contacting a service."""
    return _RestoreDependencies(
        load_receipt_chain=load_backup_receipt_chain,
        runtime_guard=RuntimeGuard,
        run_pond_process=run_pond_process,
        pin_pond_executable=_pin_pond_executable,
        capture_pond_release=_capture_pond_release,
        capture_pond_snapshot=_capture_pond_store_snapshot,
        current_source_coverage=_production_current_source_coverage,
        workspace_uuid=uuid4,
        is_local_filesystem=_is_local_filesystem,
    )


def validate_restore_request(
    config: BackupConfig,
    receipt_path: Path,
    destination: Path,
) -> None:
    """Validate local restore inputs without creating the destination."""
    try:
        with _validate_restore_request(
            config,
            receipt_path,
            destination,
            dependencies=_production_restore_dependencies(),
        ):
            return
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def restore_backup(
    config: BackupConfig,
    receipt_path: Path,
    destination: Path,
    drover_config: DroverConfig,
) -> RestoreResult:
    """Restore through fixed production dependencies and leave the store stopped."""
    return _restore_backup(
        config,
        receipt_path,
        destination,
        drover_config,
        dependencies=_production_restore_dependencies(),
    )


def _restore_backup(
    config: BackupConfig,
    receipt_path: Path,
    destination: Path,
    drover_config: DroverConfig,
    *,
    dependencies: _RestoreDependencies,
) -> RestoreResult:
    """Private exact dependency seam for deterministic restore tests."""
    if (
        type(config) is not BackupConfig
        or type(drover_config) is not DroverConfig
        or type(dependencies) is not _RestoreDependencies
    ):
        raise BackupRestoreError(_RESTORE_ERROR)
    try:
        with _validate_restore_request(
            config,
            receipt_path,
            destination,
            dependencies=dependencies,
        ) as request:
            runtime = _create_runtime(dependencies, drover_config)
            _capture_baseline(runtime)
            request.require_same()
            request.create_workspace(dependencies.workspace_uuid)
            limits = _resource_limits(config)
            progress = _restore_progress_callback(request, runtime)
            with dependencies.pin_pond_executable(config.pond_binary) as executable:
                release = _capture_release(
                    dependencies,
                    config,
                    request,
                    executable,
                    limits,
                    progress,
                )
                request.create_destination(
                    dependencies.workspace_uuid,
                    dependencies.is_local_filesystem,
                    config.local_store,
                )
                copy_result = _copy_generation(
                    dependencies,
                    config,
                    request,
                    executable,
                    limits,
                    progress,
                    verify=False,
                )
                verify_result = _copy_generation(
                    dependencies,
                    config,
                    request,
                    executable,
                    limits,
                    progress,
                    verify=True,
                )
                snapshot = _capture_restored_snapshot(
                    dependencies,
                    config,
                    request,
                    executable,
                    release,
                    limits,
                    progress,
                )
                _require_receipt_match(request.receipt, snapshot)
                resource_evidence = _restore_resource_evidence(
                    config,
                    copy_result,
                    verify_result,
                    snapshot.resource_evidence,
                )
                current_coverage = _current_source_coverage(
                    dependencies,
                    snapshot,
                    request.receipt,
                    drover_config,
                    runtime,
                )
                request.require_same()
                runtime_evidence = _finish_runtime(runtime)
                executable.require_same()
                request.require_same()
                peak_rss, peak_physical, swap_delta = resource_evidence
                return RestoreResult(
                    verified=True,
                    sessions=snapshot.counts.sessions,
                    messages=snapshot.counts.messages,
                    parts=snapshot.counts.parts,
                    current_source_coverage=current_coverage,
                    health_samples=runtime_evidence.health_samples,
                    health_p95_ms=runtime_evidence.health_p95_ms,
                    peak_rss_bytes=peak_rss,
                    peak_physical_bytes=peak_physical,
                    swap_delta_bytes=swap_delta,
                )
    except BackupRestoreError:
        raise
    except PondProcessError as error:
        category = _RESOURCE_ERROR if error.category == "resource" else _RESTORE_ERROR
        raise BackupRestoreError(category) from None
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def restore_summary(result: RestoreResult) -> dict[str, object]:
    """Return one aggregate-only restore summary."""
    try:
        if type(result) is not RestoreResult:
            raise ValueError
        result.__post_init__()
        return {
            "schema_version": 1,
            "verified": result.verified,
            "sessions": result.sessions,
            "messages": result.messages,
            "parts": result.parts,
            "current_source_coverage": result.current_source_coverage,
            "health_samples": result.health_samples,
            "health_p95_ms": result.health_p95_ms,
            "peak_rss_bytes": result.peak_rss_bytes,
            "peak_physical_bytes": result.peak_physical_bytes,
            "swap_delta_bytes": result.swap_delta_bytes,
            "store_started": False,
        }
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


class _PinnedRestoreRequest:
    __slots__ = (
        "_closed",
        "_destination_descriptor",
        "_destination_fresh",
        "_destination_identity",
        "_parent_descriptor",
        "_parent_identity",
        "_phase_bindings",
        "_receipt_descriptor",
        "_receipt_directory_descriptor",
        "_receipt_directory_identity",
        "_receipt_identity",
        "_workspace_descriptor",
        "_workspace_identity",
        "destination",
        "destination_parent",
        "generation_url",
        "receipt",
        "receipt_directory",
        "receipt_path",
        "workspace",
    )

    def __init__(
        self,
        *,
        receipt: BackupReceipt,
        generation_url: str,
        receipt_directory: Path,
        receipt_directory_descriptor: int,
        receipt_directory_identity: tuple[int, ...],
        receipt_path: Path,
        receipt_descriptor: int,
        receipt_identity: tuple[int, ...],
        destination: Path,
        destination_parent: Path,
        parent_descriptor: int,
        parent_identity: tuple[int, ...],
    ) -> None:
        self.receipt = receipt
        self.generation_url = generation_url
        self.receipt_directory = receipt_directory
        self._receipt_directory_descriptor = receipt_directory_descriptor
        self._receipt_directory_identity = receipt_directory_identity
        self.receipt_path = receipt_path
        self._receipt_descriptor = receipt_descriptor
        self._receipt_identity = receipt_identity
        self.destination = destination
        self.destination_parent = destination_parent
        self._parent_descriptor = parent_descriptor
        self._parent_identity = parent_identity
        self._destination_descriptor = -1
        self._destination_fresh = True
        self._destination_identity: tuple[int, ...] | None = None
        self.workspace: Path | None = None
        self._workspace_descriptor = -1
        self._workspace_identity: tuple[int, ...] | None = None
        self._phase_bindings: tuple[tuple[Path, int, tuple[int, ...]], ...] = ()
        self._closed = False

    def __enter__(self) -> _PinnedRestoreRequest:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = [binding[1] for binding in self._phase_bindings]
        descriptors.extend(
            (
                self._workspace_descriptor,
                self._destination_descriptor,
                self._parent_descriptor,
                self._receipt_descriptor,
                self._receipt_directory_descriptor,
            )
        )
        for descriptor in descriptors:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def require_same(self) -> None:
        if self._closed:
            raise BackupRestoreError(_RESTORE_ERROR)
        _require_directory_same(
            self.receipt_directory,
            self._receipt_directory_descriptor,
            self._receipt_directory_identity,
            exact_ctime=True,
        )
        _require_file_same_at(
            self._receipt_directory_descriptor,
            self.receipt_path.name,
            self._receipt_descriptor,
            self._receipt_identity,
        )
        _require_directory_same(
            self.destination_parent,
            self._parent_descriptor,
            self._parent_identity,
            exact_ctime=True,
        )
        if self._destination_descriptor >= 0:
            self._destination_identity = _require_child_directory_same(
                self._parent_descriptor,
                self.destination.name,
                self._destination_descriptor,
                self._destination_identity,
                exact_ctime=self._destination_fresh,
            )
        if self.workspace is not None and self._workspace_descriptor >= 0:
            self._workspace_identity = _require_child_directory_same(
                self._parent_descriptor,
                self.workspace.name,
                self._workspace_descriptor,
                self._workspace_identity,
                exact_ctime=True,
            )
        refreshed: list[tuple[Path, int, tuple[int, ...]]] = []
        for path, descriptor, identity in self._phase_bindings:
            refreshed.append(
                (
                    path,
                    descriptor,
                    _require_child_directory_same(
                        self._workspace_descriptor,
                        path.name,
                        descriptor,
                        identity,
                        exact_ctime=False,
                    ),
                )
            )
        self._phase_bindings = tuple(refreshed)

    def create_workspace(
        self,
        workspace_uuid: Callable[[], UUID],
    ) -> None:
        self.require_same()
        bindings: list[tuple[Path, int, tuple[int, ...]]] = []
        staging_descriptor = -1
        try:
            token = _new_restore_uuid(workspace_uuid)
            workspace_name = f".drover-restore-{token}"
            staging_name = f".drover-restore-workspace-staging-{token}"
            _require_absent_at(self._parent_descriptor, workspace_name)
            _require_absent_at(self._parent_descriptor, staging_name)
            os.mkdir(staging_name, 0o700, dir_fd=self._parent_descriptor)
            self._refresh_parent_identity()
            staging_descriptor, staging_identity = _open_created_directory_at(
                self._parent_descriptor,
                staging_name,
            )
            _rename_noreplace_at(
                self._parent_descriptor,
                staging_name,
                self._parent_descriptor,
                workspace_name,
            )
            staging_identity = _current_directory_identity(staging_descriptor)
            self.workspace = self.destination_parent / workspace_name
            self._workspace_descriptor = staging_descriptor
            self._workspace_identity = staging_identity
            staging_descriptor = -1
            self._refresh_parent_identity()
            self._workspace_identity = _require_child_directory_same(
                self._parent_descriptor,
                workspace_name,
                self._workspace_descriptor,
                self._workspace_identity,
                exact_ctime=True,
            )
            for phase in _PHASES:
                os.mkdir(phase, 0o700, dir_fd=self._workspace_descriptor)
                descriptor, identity = _open_created_directory_at(
                    self._workspace_descriptor,
                    phase,
                )
                bindings.append((self.workspace / phase, descriptor, identity))
            self._workspace_identity = _current_directory_identity(
                self._workspace_descriptor
            )
            self._phase_bindings = tuple(bindings)
            bindings.clear()
            self.require_same()
        except BackupRestoreError:
            raise
        except Exception:
            raise BackupRestoreError(_RESTORE_ERROR) from None
        finally:
            if staging_descriptor >= 0:
                try:
                    os.close(staging_descriptor)
                except OSError:
                    pass
            for _, descriptor, _ in bindings:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def create_destination(
        self,
        workspace_uuid: Callable[[], UUID],
        is_local_filesystem: _LocalFilesystemCheck,
        local_store: Path,
    ) -> None:
        self.require_same()
        _require_absent_at(self._parent_descriptor, self.destination.name)
        staging_descriptor = -1
        try:
            token = _new_restore_uuid(workspace_uuid)
            staging_name = f".drover-restore-destination-{token}"
            staging_path = self.destination_parent / staging_name
            _require_absent_at(self._parent_descriptor, staging_name)
            os.mkdir(staging_name, 0o700, dir_fd=self._parent_descriptor)
            self._refresh_parent_identity()
            staging_descriptor, staging_identity = _open_created_directory_at(
                self._parent_descriptor,
                staging_name,
            )
            if is_local_filesystem(staging_descriptor, staging_path) is not True:
                raise ValueError
            _require_opened_not_live_store(local_store, staging_descriptor)
            _rename_noreplace_at(
                self._parent_descriptor,
                staging_name,
                self._parent_descriptor,
                self.destination.name,
            )
            staging_identity = _current_directory_identity(staging_descriptor)
            self._destination_descriptor = staging_descriptor
            self._destination_identity = staging_identity
            staging_descriptor = -1
            self._refresh_parent_identity()
            self._destination_identity = _require_child_directory_same(
                self._parent_descriptor,
                self.destination.name,
                self._destination_descriptor,
                self._destination_identity,
                exact_ctime=True,
            )
            if (
                is_local_filesystem(
                    self._destination_descriptor,
                    self.destination,
                )
                is not True
            ):
                raise ValueError
            _require_opened_not_live_store(
                local_store,
                self._destination_descriptor,
            )
            self.require_same()
        except BackupRestoreError:
            raise
        except Exception:
            raise BackupRestoreError(_RESTORE_ERROR) from None
        finally:
            if staging_descriptor >= 0:
                try:
                    os.close(staging_descriptor)
                except OSError:
                    pass

    def phase(self, name: str) -> Path:
        if name not in _PHASES or self.workspace is None:
            raise BackupRestoreError(_RESTORE_ERROR)
        path = self.workspace / name
        if all(binding[0] != path for binding in self._phase_bindings):
            raise BackupRestoreError(_RESTORE_ERROR)
        return path

    def require_fresh_destination(self) -> None:
        self.require_same()
        if self._destination_identity is None:
            raise BackupRestoreError(_RESTORE_ERROR)
        _require_child_directory_same(
            self._parent_descriptor,
            self.destination.name,
            self._destination_descriptor,
            self._destination_identity,
            exact_ctime=True,
        )

        try:
            with os.scandir(self._destination_descriptor) as entries:
                if next(entries, None) is not None:
                    raise ValueError
        except Exception:
            raise BackupRestoreError(_RESTORE_ERROR) from None
        _require_child_directory_same(
            self._parent_descriptor,
            self.destination.name,
            self._destination_descriptor,
            self._destination_identity,
            exact_ctime=True,
        )

    def begin_copy(self) -> None:
        self.require_fresh_destination()
        self._destination_fresh = False

    def _refresh_parent_identity(self) -> None:
        current = _opened_directory_identity(self._parent_descriptor)
        lexical_descriptor = -1
        try:
            lexical_descriptor = _open_nofollow_path(
                self.destination_parent,
                flags=_directory_flags(),
            )
            lexical = _opened_directory_identity(lexical_descriptor)
        except (OSError, TypeError, ValueError):
            raise BackupRestoreError(_RESTORE_ERROR) from None
        finally:
            if lexical_descriptor >= 0:
                try:
                    os.close(lexical_descriptor)
                except OSError:
                    pass
        if _directory_node_identity(current) != _directory_node_identity(
            self._parent_identity
        ) or _directory_node_identity(lexical) != _directory_node_identity(current):
            raise BackupRestoreError(_RESTORE_ERROR)
        self._parent_identity = current


def _validate_restore_request(
    config: BackupConfig,
    receipt_path: Path,
    destination: Path,
    *,
    dependencies: _RestoreDependencies,
) -> _PinnedRestoreRequest:
    receipt_directory_descriptor = -1
    receipt_descriptor = -1
    parent_descriptor = -1
    try:
        if (
            type(config) is not BackupConfig
            or type(dependencies) is not _RestoreDependencies
        ):
            raise ValueError
        receipt_directory = config.receipt_directory
        selected = _require_selected_receipt_path(receipt_path, receipt_directory)
        receipt_directory_descriptor = _open_nofollow_path(
            receipt_directory,
            flags=_directory_flags(),
        )
        receipt_directory_identity = _require_private_directory(
            receipt_directory_descriptor,
            exact_mode=0o700,
        )
        receipt_descriptor = os.open(
            selected.name,
            _file_flags(),
            dir_fd=receipt_directory_descriptor,
        )
        receipt_identity, selected_payload = _read_pinned_receipt(receipt_descriptor)
        chain = dependencies.load_receipt_chain(selected, receipt_directory)
        if (
            not isinstance(chain, tuple)
            or not chain
            or any(type(receipt) is not BackupReceipt for receipt in chain)
        ):
            raise ValueError
        receipt = chain[-1]
        if (
            receipt.store_scope_id != config.store_scope_id
            or selected.name != f"backup-{receipt.generation_id}.json"
            or selected_payload != receipt.to_wire()
        ):
            raise ValueError
        reread_identity, reread_payload = _read_pinned_receipt(receipt_descriptor)
        if reread_identity != receipt_identity or reread_payload != selected_payload:
            raise ValueError
        _require_directory_same(
            receipt_directory,
            receipt_directory_descriptor,
            receipt_directory_identity,
            exact_ctime=True,
        )
        _require_file_same_at(
            receipt_directory_descriptor,
            selected.name,
            receipt_descriptor,
            receipt_identity,
        )
        generation_id = UUID(receipt.generation_id)
        generation_url = generation_storage_url(config, generation_id)
        destination_path, parent = _require_destination_path(destination)
        parent_descriptor = _open_nofollow_path(parent, flags=_directory_flags())
        parent_identity = _require_safe_destination_parent(parent_descriptor)
        if dependencies.is_local_filesystem(parent_descriptor, parent) is not True:
            raise ValueError
        _require_absent_at(parent_descriptor, destination_path.name)
        _require_not_live_store(config.local_store, parent_descriptor, destination_path)
        if _directory_node_identity(parent_identity) == _directory_node_identity(
            receipt_directory_identity
        ):
            raise ValueError
        request = _PinnedRestoreRequest(
            receipt=receipt,
            generation_url=generation_url,
            receipt_directory=receipt_directory,
            receipt_directory_descriptor=receipt_directory_descriptor,
            receipt_directory_identity=receipt_directory_identity,
            receipt_path=selected,
            receipt_descriptor=receipt_descriptor,
            receipt_identity=receipt_identity,
            destination=destination_path,
            destination_parent=parent,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
        )
        request.require_same()
        _require_absent_at(request._parent_descriptor, destination_path.name)
        receipt_directory_descriptor = -1
        receipt_descriptor = -1
        parent_descriptor = -1
        return request
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    finally:
        for descriptor in (
            parent_descriptor,
            receipt_descriptor,
            receipt_directory_descriptor,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _create_runtime(
    dependencies: _RestoreDependencies,
    drover_config: DroverConfig,
) -> _Runtime:
    try:
        runtime = dependencies.runtime_guard(drover_config)
        if not all(
            callable(getattr(runtime, name, None))
            for name in (
                "capture_baseline",
                "baseline_host_id",
                "sample",
                "finish",
            )
        ):
            raise ValueError
        return runtime
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _capture_baseline(runtime: _Runtime) -> None:
    try:
        runtime.capture_baseline()
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _resource_limits(config: BackupConfig) -> ResourceLimits:
    try:
        return ResourceLimits(
            config.max_rss_bytes,
            config.max_physical_bytes,
            config.max_swap_growth_bytes,
        )
    except Exception:
        raise BackupRestoreError(_RESOURCE_ERROR) from None


def _restore_progress_callback(
    request: _PinnedRestoreRequest,
    runtime: _Runtime,
) -> Callable[[], None]:
    def progress() -> None:
        request.require_same()
        try:
            runtime.sample()
        except BackupRuntimeError as error:
            category = (
                "resource"
                if _runtime_category(error) == _RESOURCE_ERROR
                else "subprocess"
            )
            raise PondProcessError(category) from None
        except Exception:
            raise PondProcessError("subprocess") from None
        request.require_same()

    return progress


def _copy_generation(
    dependencies: _RestoreDependencies,
    config: BackupConfig,
    request: _PinnedRestoreRequest,
    executable: _PinnedPondExecutable,
    limits: ResourceLimits,
    progress: Callable[[], None],
    *,
    verify: bool,
) -> PondProcessResult:
    if not verify:
        request.begin_copy()
    label = "verify-only" if verify else "copy-from-generation"
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
            request.generation_url,
            "--to",
            str(request.destination),
        )
    )
    request.require_same()
    executable.require_same()
    try:
        result = dependencies.run_pond_process(
            executable,
            tuple(command),
            timeout_seconds=float(config.copy_timeout_seconds),
            run_directory=request.phase("verify" if verify else "copy"),
            label=label,
            resource_limits=limits,
            progress_callback=progress,
        )
    except BackupRestoreError:
        raise
    except PondProcessError as error:
        category = _RESOURCE_ERROR if error.category == "resource" else _RESTORE_ERROR
        raise BackupRestoreError(category) from None
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    executable.require_same()
    request.require_same()
    _require_process_result(
        result,
        request.phase("verify" if verify else "copy"),
        label,
    )
    _require_process_resources(config, result)
    if result.returncode != 0:
        raise BackupRestoreError(_RESTORE_ERROR)
    return result


def _capture_release(
    dependencies: _RestoreDependencies,
    config: BackupConfig,
    request: _PinnedRestoreRequest,
    executable: _PinnedPondExecutable,
    limits: ResourceLimits,
    progress: Callable[[], None],
) -> _PondReleaseEvidence:
    request.require_same()
    executable.require_same()
    try:
        release = dependencies.capture_pond_release(
            executable,
            workspace=request.phase("release"),
            progress_callback=progress,
            resource_limits=limits,
        )
        if type(release) is not _PondReleaseEvidence:
            raise ValueError
    except BackupRestoreError:
        raise
    except PondProcessError as error:
        category = _RESOURCE_ERROR if error.category == "resource" else _RESTORE_ERROR
        raise BackupRestoreError(category) from None
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    executable.require_same()
    request.require_same()
    _require_resource_evidence(config, release.resource_evidence)
    return release


def _capture_restored_snapshot(
    dependencies: _RestoreDependencies,
    config: BackupConfig,
    request: _PinnedRestoreRequest,
    executable: _PinnedPondExecutable,
    release: _PondReleaseEvidence,
    limits: ResourceLimits,
    progress: Callable[[], None],
) -> PondStoreSnapshot:
    request.require_same()
    executable.require_same()
    try:
        snapshot = dependencies.capture_pond_snapshot(
            executable,
            storage=LocalPondStore(request.destination),
            pond_config=config.remote_pond_config,
            workspace=request.phase("snapshot"),
            timeout_seconds=config.copy_timeout_seconds,
            progress_callback=progress,
            resource_limits=limits,
            release_evidence=release,
        )
        if type(snapshot) is not PondStoreSnapshot:
            raise ValueError
        snapshot.root_inventory.to_wire()
        snapshot.counts.to_wire()
    except BackupRestoreError:
        raise
    except PondProcessError as error:
        category = _RESOURCE_ERROR if error.category == "resource" else _RESTORE_ERROR
        raise BackupRestoreError(category) from None
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    executable.require_same()
    request.require_same()
    return snapshot


def _require_process_result(
    result: PondProcessResult,
    workspace: Path,
    label: str,
) -> None:
    try:
        if type(result) is not PondProcessResult:
            raise ValueError
        values = (
            result.returncode,
            result.duration_ms,
            result.peak_rss_bytes,
            result.swap_delta_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError
        if (
            result.duration_ms < 0
            or result.peak_rss_bytes < 0
            or result.swap_delta_bytes < 0
            or (
                result.peak_physical_bytes is not None
                and (
                    isinstance(result.peak_physical_bytes, bool)
                    or not isinstance(result.peak_physical_bytes, int)
                    or result.peak_physical_bytes < 0
                )
            )
            or result.stdout_path != workspace / f"{label}.stdout"
            or result.stderr_path != workspace / f"{label}.stderr"
        ):
            raise ValueError
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _require_process_resources(
    config: BackupConfig,
    result: PondProcessResult,
) -> None:
    try:
        evidence = _pond_process_resource_evidence(result)
        if (
            evidence.peak_physical_bytes is None
            or evidence.peak_rss_bytes > config.max_rss_bytes
            or evidence.peak_physical_bytes > config.max_physical_bytes
            or evidence.swap_delta_bytes > config.max_swap_growth_bytes
        ):
            raise ValueError
    except Exception:
        raise BackupRestoreError(_RESOURCE_ERROR) from None


def _require_resource_evidence(
    config: BackupConfig,
    evidence: PondResourceEvidence,
) -> None:
    try:
        if (
            type(evidence) is not PondResourceEvidence
            or evidence.peak_physical_bytes is None
            or evidence.peak_rss_bytes > config.max_rss_bytes
            or evidence.peak_physical_bytes > config.max_physical_bytes
            or evidence.swap_delta_bytes > config.max_swap_growth_bytes
        ):
            raise ValueError
    except Exception:
        raise BackupRestoreError(_RESOURCE_ERROR) from None


def _require_receipt_match(
    receipt: BackupReceipt,
    snapshot: PondStoreSnapshot,
) -> None:
    try:
        counts = snapshot.counts
        if (
            pond_inventory_content_sha256(snapshot.root_inventory)
            != receipt.remote_pond_inventory_sha256
            or counts.sessions != receipt.sessions
            or counts.messages != receipt.messages
            or counts.parts != receipt.parts
            or counts.disallowed_sessions != 0
            or counts.logical_duplicate_groups != 0
            or counts.sessions_in_logical_duplicate_groups != 0
            or any(value != 0 for value in receipt.collision_counts.to_wire().values())
        ):
            raise ValueError
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _restore_resource_evidence(
    config: BackupConfig,
    copy_result: PondProcessResult,
    verify_result: PondProcessResult,
    snapshot_evidence: PondResourceEvidence,
) -> tuple[int, int, int]:
    try:
        evidence = _pond_aggregate_resource_evidence(
            _pond_process_resource_evidence(copy_result),
            _pond_process_resource_evidence(verify_result),
            snapshot_evidence,
        )
        if evidence.peak_physical_bytes is None:
            raise ValueError
        if (
            evidence.peak_rss_bytes > config.max_rss_bytes
            or evidence.peak_physical_bytes > config.max_physical_bytes
            or evidence.swap_delta_bytes > config.max_swap_growth_bytes
        ):
            raise ValueError
        return (
            evidence.peak_rss_bytes,
            evidence.peak_physical_bytes,
            evidence.swap_delta_bytes,
        )
    except Exception:
        raise BackupRestoreError(_RESOURCE_ERROR) from None


def _current_source_coverage(
    dependencies: _RestoreDependencies,
    snapshot: PondStoreSnapshot,
    receipt: BackupReceipt,
    drover_config: DroverConfig,
    runtime: _Runtime,
) -> Literal["current", "stale", "unavailable"]:
    try:
        host_id = runtime.baseline_host_id()
        if not isinstance(host_id, str) or not host_id:
            raise ValueError
        outcome = dependencies.current_source_coverage(
            snapshot,
            receipt,
            drover_config,
            host_id,
        )
        if outcome not in _COVERAGE_OUTCOMES:
            raise ValueError
        return outcome
    except Exception:
        return "unavailable"


def _finish_runtime(runtime: _Runtime) -> RuntimeEvidence:
    try:
        evidence = runtime.finish()
        if (
            type(evidence) is not RuntimeEvidence
            or isinstance(evidence.health_samples, bool)
            or type(evidence.health_samples) is not int
            or evidence.health_samples < 30
            or type(evidence.health_p95_ms) is not float
            or not math.isfinite(evidence.health_p95_ms)
            or not 0 <= evidence.health_p95_ms < 100
        ):
            raise ValueError
        return evidence
    except BackupRuntimeError as error:
        raise BackupRestoreError(_runtime_category(error)) from None
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _production_current_source_coverage(
    snapshot: PondStoreSnapshot,
    receipt: BackupReceipt,
    drover_config: DroverConfig,
    host_id: str,
) -> Literal["current", "stale", "unavailable"]:
    """Compare current metadata identities without touching source contents."""
    del drover_config
    try:
        current = discover_native_history_inventory(
            Path.home(),
            host_id,
            max_records=MAX_NATIVE_INVENTORY_RECORDS,
        )
        report = build_coverage_report(
            (),
            (current,),
            snapshot.root_inventory,
        )
        summary = coverage_summary(report)
        current_summary = summary["current_source_coverage"]
        if not isinstance(current_summary, dict):
            raise ValueError
        discovered = _exact_nonnegative(current_summary.get("discovered"))
        matched = _exact_nonnegative(current_summary.get("matched"))
        unmatched = discovered - matched
        root_sessions = len(snapshot.root_inventory.records)
        if (
            not report.duplicate_source_groups
            and not report.cross_harness_native_id_groups
            and matched == root_sessions
            and unmatched == receipt.source_not_archive_eligible
        ):
            return "current"
        return "stale"
    except Exception:
        return "unavailable"


def _runtime_category(error: BackupRuntimeError) -> str:
    return _RESOURCE_ERROR if str(error) == _RESOURCE_ERROR else _RESTORE_ERROR


def _exact_nonnegative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _require_selected_receipt_path(path: Path, directory: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.parent != directory:
        raise ValueError
    if path.name in {"", ".", ".."} or "\x00" in path.name:
        raise ValueError
    return path


def _require_destination_path(path: Path) -> tuple[Path, Path]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError
    parent = path.parent
    if path.name in {"", ".", ".."} or "\x00" in path.name:
        raise ValueError
    try:
        if parent.resolve(strict=True) != parent:
            raise ValueError
    except (OSError, RuntimeError):
        raise ValueError from None
    return path, parent


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
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


def _directory_node_identity(identity: tuple[int, ...]) -> tuple[int, ...]:
    return identity[:4]


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


def _current_directory_identity(descriptor: int) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupRestoreError(_RESTORE_ERROR)
    return _directory_identity(metadata)


def _opened_directory_identity(descriptor: int) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise BackupRestoreError(_RESTORE_ERROR)
    return _directory_identity(metadata)


def _require_private_directory(
    descriptor: int,
    *,
    exact_mode: int,
) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != exact_mode
    ):
        raise ValueError
    return _directory_identity(metadata)


def _require_safe_destination_parent(descriptor: int) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError
    return _directory_identity(metadata)


def _read_pinned_receipt(descriptor: int) -> tuple[tuple[int, ...], Any]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
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
        if len(data) > MAX_INVENTORY_BYTES or _file_identity(before) != _file_identity(
            after
        ):
            raise ValueError
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
        return _file_identity(after), payload
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _require_directory_same(
    path: Path,
    descriptor: int,
    expected: tuple[int, ...],
    *,
    exact_ctime: bool,
) -> tuple[int, ...]:
    lexical_descriptor = -1
    try:
        opened = _opened_directory_identity(descriptor)
        lexical_descriptor = _open_nofollow_path(path, flags=_directory_flags())
        lexical = _opened_directory_identity(lexical_descriptor)
        if _directory_node_identity(opened) != _directory_node_identity(expected):
            raise ValueError
        if _directory_node_identity(lexical) != _directory_node_identity(opened):
            raise ValueError
        if exact_ctime and (opened != expected or lexical != expected):
            raise ValueError
        return opened
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    finally:
        if lexical_descriptor >= 0:
            try:
                os.close(lexical_descriptor)
            except OSError:
                pass


def _require_file_same_at(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    expected: tuple[int, ...],
) -> None:
    try:
        opened = os.fstat(descriptor)
        lexical = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(opened) != expected or _file_identity(lexical) != expected:
            raise ValueError
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _require_absent_at(directory_descriptor: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    raise BackupRestoreError(_RESTORE_ERROR)


def _new_restore_uuid(factory: Callable[[], UUID]) -> UUID:
    try:
        token = factory()
        if type(token) is not UUID or token.version != 4:
            raise ValueError
        return token
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _open_created_directory_at(
    parent_descriptor: int,
    name: str,
) -> tuple[int, tuple[int, ...]]:
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = _current_directory_identity(descriptor)
        lexical = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        lexical_identity = _directory_identity(lexical)
        if lexical_identity != opened:
            raise ValueError
        return descriptor, opened
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _require_child_directory_same(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected: tuple[int, ...] | None,
    *,
    exact_ctime: bool,
) -> tuple[int, ...]:
    try:
        if expected is None:
            raise ValueError
        opened = _current_directory_identity(descriptor)
        lexical = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        lexical_identity = _directory_identity(lexical)
        if (
            _directory_node_identity(opened) != _directory_node_identity(expected)
            or _directory_node_identity(lexical_identity)
            != _directory_node_identity(opened)
            or (exact_ctime and (opened != expected or lexical_identity != expected))
        ):
            raise ValueError
        return opened
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None


def _require_not_live_store(
    local_store: Path,
    parent_descriptor: int,
    destination: Path,
) -> None:
    live_descriptor = -1
    current_descriptor = -1
    try:
        if (
            not local_store.is_absolute()
            or local_store.resolve(strict=True) != local_store
        ):
            raise ValueError
        live_descriptor = _open_nofollow_path(local_store, flags=_directory_flags())
        live = os.fstat(live_descriptor)
        if not stat.S_ISDIR(live.st_mode):
            raise ValueError
        if destination == local_store or local_store in destination.parents:
            raise ValueError
        live_key = (live.st_dev, live.st_ino)
        current_descriptor = os.dup(parent_descriptor)
        for _ in range(_MAX_PATH_ANCESTORS):
            current = os.fstat(current_descriptor)
            if (current.st_dev, current.st_ino) == live_key:
                raise ValueError
            parent = os.open("..", _directory_flags(), dir_fd=current_descriptor)
            parent_metadata = os.fstat(parent)
            if (parent_metadata.st_dev, parent_metadata.st_ino) == (
                current.st_dev,
                current.st_ino,
            ):
                os.close(parent)
                return
            os.close(current_descriptor)
            current_descriptor = parent
        raise ValueError
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    finally:
        for descriptor in (current_descriptor, live_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _require_opened_not_live_store(
    local_store: Path,
    destination_descriptor: int,
) -> None:
    live_descriptor = -1
    current_descriptor = -1
    try:
        if (
            not local_store.is_absolute()
            or local_store.resolve(strict=True) != local_store
        ):
            raise ValueError
        live_descriptor = _open_nofollow_path(
            local_store,
            flags=_directory_flags(),
        )
        live = os.fstat(live_descriptor)
        if not stat.S_ISDIR(live.st_mode):
            raise ValueError
        live_key = (live.st_dev, live.st_ino)
        current_descriptor = os.dup(destination_descriptor)
        for _ in range(_MAX_PATH_ANCESTORS):
            current = os.fstat(current_descriptor)
            if (current.st_dev, current.st_ino) == live_key:
                raise ValueError
            parent = os.open("..", _directory_flags(), dir_fd=current_descriptor)
            parent_metadata = os.fstat(parent)
            if (parent_metadata.st_dev, parent_metadata.st_ino) == (
                current.st_dev,
                current.st_ino,
            ):
                os.close(parent)
                return
            os.close(current_descriptor)
            current_descriptor = parent
        raise ValueError
    except BackupRestoreError:
        raise
    except Exception:
        raise BackupRestoreError(_RESTORE_ERROR) from None
    finally:
        for descriptor in (current_descriptor, live_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class _DarwinFsid(ctypes.Structure):
    _fields_ = (("values", ctypes.c_int32 * 2),)


class _DarwinStatFs(ctypes.Structure):
    _fields_ = (
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", _DarwinFsid),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    )


def _is_local_filesystem(descriptor: int, path: Path) -> bool:
    try:
        if sys.platform == "darwin":
            result = _DarwinStatFs()
            library = ctypes.CDLL(None, use_errno=True)
            function = library.fstatfs
            function.argtypes = (ctypes.c_int, ctypes.POINTER(_DarwinStatFs))
            function.restype = ctypes.c_int
            return function(descriptor, ctypes.byref(result)) == 0 and bool(
                result.f_flags & _DARWIN_MNT_LOCAL
            )
        if sys.platform.startswith("linux"):
            return _linux_local_filesystem(path)
        return False
    except Exception:
        return False


def _linux_local_filesystem(path: Path) -> bool:
    descriptor = -1
    try:
        descriptor = os.open("/proc/self/mountinfo", _file_flags())
        data = os.read(descriptor, _MAX_MOUNTINFO_BYTES + 1)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(data) > _MAX_MOUNTINFO_BYTES:
        return False
    selected: tuple[int, str] | None = None
    path_text = str(path)
    for line in data.splitlines():
        try:
            fields = line.decode("utf-8").split()
            separator = fields.index("-")
            mountpoint = _decode_mountinfo_path(fields[4])
            filesystem = fields[separator + 1]
        except (UnicodeDecodeError, ValueError, IndexError):
            return False
        if path_text == mountpoint or path_text.startswith(
            mountpoint.rstrip("/") + "/"
        ):
            length = len(mountpoint)
            if selected is None or length > selected[0]:
                selected = (length, filesystem)
    return selected is not None and selected[1] in _LINUX_LOCAL_FILESYSTEMS


def _decode_mountinfo_path(value: str) -> str:
    for escaped, plain in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, plain)
    return value
