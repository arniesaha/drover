"""Fresh-directory restore orchestration and safety boundaries."""

from __future__ import annotations

import inspect
import os
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest

import drover.server.archive as archive_package
import drover.server.archive.backup_restore as backup_restore_module
from drover.config import default_config
from drover.server.archive.backup_config import BackupConfig
from drover.server.archive.backup_receipt import (
    BackupReceipt,
    CollisionCounts,
    load_backup_receipt_chain,
    write_backup_receipt,
)
from drover.server.archive.backup_restore import (
    BackupRestoreError,
    RestoreResult,
    _restore_backup,
    _RestoreDependencies,
    restore_backup,
    restore_summary,
    validate_restore_request,
)
from drover.server.archive.backup_runtime import BackupRuntimeError, RuntimeEvidence
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
)
from drover.server.archive.pond_process import (
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
)
from drover.server.archive.pond_snapshot import PondCorpusCounts, PondStoreSnapshot

_GENERATION_ID = UUID("48d862c3-787a-4970-9a1c-842f9098473e")
_SCOPE_ID = "536b300b-24ff-4dda-a3e9-52fde1154b59"
_CAPTURED_AT = "2026-08-29T12:00:00Z"
_PRIVATE_URL = (
    "s3+https://account-private.r2.cloudflarestorage.com/"
    "bucket-private/prefix-private"
)
_PRIVATE_VALUES = (
    _PRIVATE_URL,
    str(_GENERATION_ID),
    _SCOPE_ID,
    "claude-private",
    "codex-private",
    "host-private",
    "PRIVATE CHILD DETAIL",
)
_RESTORE_ERROR = "archive backup restore failed"
_RESOURCE_ERROR = "archive backup resource limit"


def _private_file(path: Path, content: str = "private") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _config(tmp_path: Path) -> BackupConfig:
    binary = _private_file(tmp_path / "pond-private", "#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    local_config = _private_file(tmp_path / "local-private.toml")
    remote_config = _private_file(tmp_path / "remote-private.toml")
    local_store = tmp_path / "local-store-private"
    local_store.mkdir(mode=0o700)
    receipts = tmp_path / "receipts-private"
    receipts.mkdir(mode=0o700)
    return BackupConfig(
        schema_version=1,
        pond_binary=binary,
        local_pond_config=local_config,
        local_store=local_store,
        remote_pond_config=remote_config,
        backup_root_url=_PRIVATE_URL,
        store_scope_id=_SCOPE_ID,
        receipt_directory=receipts,
        copy_timeout_seconds=60,
        max_rss_bytes=1024,
        max_physical_bytes=2048,
        max_swap_growth_bytes=64,
    )


def _inventory(
    *,
    captured_at: str = _CAPTURED_AT,
    records: tuple[PondInventoryRecord, ...] | None = None,
) -> PondInventory:
    return PondInventory(
        schema_version=1,
        captured_at=captured_at,
        pond_version="0.16.3",
        records=records
        or (
            PondInventoryRecord(
                session_id="claude-private",
                source_agent="claude-code",
                created_at="2026-08-29T10:00:00Z",
                message_count=2,
                first_message_at="2026-08-29T10:01:00Z",
                last_message_at="2026-08-29T10:02:00Z",
            ),
            PondInventoryRecord(
                session_id="codex-private",
                source_agent="codex-cli",
                created_at="2026-08-29T11:00:00Z",
                message_count=1,
                first_message_at="2026-08-29T11:01:00Z",
                last_message_at="2026-08-29T11:01:00Z",
            ),
        ),
    )


def _snapshot(
    *,
    inventory: PondInventory | None = None,
    counts: PondCorpusCounts | None = None,
    resources: PondResourceEvidence | None = None,
) -> PondStoreSnapshot:
    return PondStoreSnapshot(
        inventory or _inventory(),
        counts or PondCorpusCounts(2, 3, 5, 0, 0, 0),
        resources or PondResourceEvidence(300, 600, 20),
    )


def _native_inventory(
    identities: tuple[tuple[str, str], ...],
) -> NativeInventory:
    return NativeInventory(
        schema_version=2,
        captured_at=_CAPTURED_AT,
        host_id="host-private",
        records=tuple(
            NativeInventoryRecord(
                source_agent=source_agent,
                session_id=session_id,
                updated_at="2026-08-29T12:00:00Z",
                size_bytes=100 + index,
                source_copies=1,
                source_fingerprint=f"{index + 1:064x}",
            )
            for index, (source_agent, session_id) in enumerate(identities)
        ),
    )


def _receipt(config: BackupConfig, *, sessions: int = 2) -> BackupReceipt:
    from drover.server.archive.pond_snapshot import pond_inventory_content_sha256

    return BackupReceipt(
        schema_version=1,
        created_at="2026-08-29T12:30:00Z",
        pond_version="0.16.3",
        store_scope_id=config.store_scope_id,
        generation_id=str(_GENERATION_ID),
        previous_receipt_sha256=None,
        source_inventory_sha256="1" * 64,
        local_pond_inventory_sha256="2" * 64,
        remote_pond_inventory_sha256=pond_inventory_content_sha256(_inventory()),
        coverage_report_sha256="4" * 64,
        sessions=sessions,
        messages=3,
        parts=5,
        source_not_archive_eligible=0,
        collision_counts=CollisionCounts(0, 0, 0, 0),
        copy_duration_ms=10,
        verify_duration_ms=5,
        health_samples=30,
        health_p95_ms=2.0,
        peak_rss_bytes=64,
        peak_physical_bytes=128,
        swap_delta_bytes=0,
    )


class _Runtime:
    def __init__(self, fixture: "_Fixture") -> None:
        self.fixture = fixture

    def capture_baseline(self) -> None:
        self.fixture.events.append("baseline")
        self.fixture.attempts["baseline"] += 1
        callback = self.fixture.baseline_callback
        if callback is not None:
            callback()
        if self.fixture.fault == "baseline":
            raise BackupRuntimeError(_RESTORE_ERROR)

    def baseline_host_id(self) -> str:
        return "host-private"

    def sample(self) -> None:
        self.fixture.attempts["runtime-sample"] += 1
        if self.fixture.fault == "runtime-resource":
            raise BackupRuntimeError(_RESOURCE_ERROR)
        if self.fixture.fault == "runtime-health":
            raise BackupRuntimeError(_RESTORE_ERROR)

    def finish(self) -> RuntimeEvidence:
        self.fixture.events.append("runtime-finish")
        self.fixture.attempts["runtime-finish"] += 1
        if self.fixture.fault == "finish-resource":
            raise BackupRuntimeError(_RESOURCE_ERROR)
        if self.fixture.fault == "finish-health":
            raise BackupRuntimeError(_RESTORE_ERROR)
        if self.fixture.fault == "finish-low-samples":
            return RuntimeEvidence(29, 4.5)
        return RuntimeEvidence(35, 4.5)


@dataclass(slots=True)
class _Fixture:
    config: BackupConfig
    receipt_path: Path
    destination: Path
    drover_config: object
    fault: str | None
    events: list[str]
    attempts: Counter[str]
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]]
    limits: list[tuple[str, ResourceLimits | None]]
    runtime: _Runtime | None = None
    dependencies: _RestoreDependencies | None = None
    current_status: str = "current"
    baseline_callback: object = None


def _fixture(tmp_path: Path, *, fault: str | None = None) -> _Fixture:
    config = _config(tmp_path)
    receipt_path = write_backup_receipt(
        config.receipt_directory,
        _receipt(config),
    )
    destination_parent = tmp_path / "restore-parent-private"
    destination_parent.mkdir(mode=0o700)
    fixture = _Fixture(
        config=config,
        receipt_path=receipt_path,
        destination=destination_parent / "restored-private",
        drover_config=replace(
            default_config(),
            duckdb_path=tmp_path / "registry-private.duckdb",
        ),
        fault=fault,
        events=[],
        attempts=Counter(),
        process_calls=[],
        limits=[],
    )
    fixture.runtime = _Runtime(fixture)

    def runtime_guard(received_config):
        assert received_config is fixture.drover_config
        assert fixture.runtime is not None
        return fixture.runtime

    def run_process(
        binary,
        arguments,
        *,
        timeout_seconds,
        run_directory,
        label,
        resource_limits=None,
        progress_callback=None,
        **kwargs,
    ):
        assert binary == config.pond_binary
        assert timeout_seconds == 60.0
        assert progress_callback is not None
        assert kwargs == {}
        fixture.attempts[label] += 1
        fixture.events.append(label)
        fixture.process_calls.append((tuple(arguments), {"label": label}))
        fixture.limits.append((label, resource_limits))
        if fault == "destination-swap" and label == "copy-from-generation":
            moved = fixture.destination.with_name("moved-restored-private")
            fixture.destination.rename(moved)
            fixture.destination.mkdir(mode=0o700)
        if fault == "destination-swap-restore" and label == "copy-from-generation":
            moved = fixture.destination.with_name("moved-restored-private")
            fixture.destination.rename(moved)
            moved.rename(fixture.destination)
        progress_callback()
        category = {
            ("copy-from-generation", "copy-failure"): "subprocess",
            ("verify-only", "verify-failure"): "subprocess",
            ("copy-from-generation", "copy-resource"): "resource",
            ("verify-only", "verify-resource"): "resource",
        }.get((label, fault))
        if category is not None:
            raise PondProcessError(category)
        stdout = Path(run_directory) / f"{label}.stdout"
        stderr = Path(run_directory) / f"{label}.stderr"
        stdout.write_bytes(b"")
        stdout.chmod(0o600)
        stderr.write_text("PRIVATE CHILD DETAIL", encoding="utf-8")
        stderr.chmod(0o600)
        physical = None if fault == f"{label}-physical-none" else 500
        rss = config.max_rss_bytes + 1 if fault == f"{label}-rss" else 250
        swap = config.max_swap_growth_bytes + 1 if fault == f"{label}-swap" else 10
        return PondProcessResult(
            0,
            20 if label == "copy-from-generation" else 10,
            rss,
            physical,
            swap,
            stdout,
            stderr,
        )

    def capture_snapshot(
        binary,
        *,
        storage,
        pond_config,
        workspace,
        timeout_seconds,
        progress_callback=None,
        resource_limits=None,
    ):
        assert binary == config.pond_binary
        assert pond_config == config.remote_pond_config
        assert storage.path == fixture.destination
        assert timeout_seconds == 60
        assert progress_callback is not None
        for phase in ("snapshot-version", "snapshot-corpus"):
            fixture.attempts[phase] += 1
            fixture.events.append(phase)
            fixture.limits.append((phase, resource_limits))
            progress_callback()
            if fault == f"{phase}-resource":
                raise PondProcessError("resource")
        inventory = _inventory()
        counts = PondCorpusCounts(2, 3, 5, 0, 0, 0)
        resources = PondResourceEvidence(300, 600, 20)
        if fault == "inventory-digest":
            changed = replace(inventory.records[0], created_at="2026-08-29T09:59:00Z")
            inventory = _inventory(records=(changed, inventory.records[1]))
        if fault == "sessions":
            counts = PondCorpusCounts(3, 3, 5, 0, 0, 0)
        if fault == "messages":
            counts = PondCorpusCounts(2, 4, 5, 0, 0, 0)
        if fault == "parts":
            counts = PondCorpusCounts(2, 3, 6, 0, 0, 0)
        if fault == "disallowed":
            counts = PondCorpusCounts(2, 3, 5, 1, 0, 0)
        if fault == "logical-duplicates":
            counts = PondCorpusCounts(2, 3, 5, 0, 1, 2)
        if fault == "snapshot-physical-none":
            resources = PondResourceEvidence(300, None, 20)
        if fault == "snapshot-rss":
            resources = PondResourceEvidence(config.max_rss_bytes + 1, 600, 20)
        return PondStoreSnapshot(inventory, counts, resources)

    def current_coverage(snapshot, receipt, drover_config, host_id):
        assert type(snapshot) is PondStoreSnapshot
        assert type(receipt) is BackupReceipt
        assert drover_config is fixture.drover_config
        assert host_id == "host-private"
        fixture.events.append("current-coverage")
        if fault == "coverage-error":
            raise ValueError("private current source failure")
        return fixture.current_status

    fixture.dependencies = _RestoreDependencies(
        load_receipt_chain=load_backup_receipt_chain,
        runtime_guard=runtime_guard,
        run_pond_process=run_process,
        capture_pond_snapshot=capture_snapshot,
        current_source_coverage=current_coverage,
        workspace_uuid=lambda: UUID("8db84b0e-1d8e-421b-a060-00443d03422f"),
        is_local_filesystem=lambda descriptor, path: True,
    )
    return fixture


def _restore(fixture: _Fixture) -> RestoreResult:
    assert fixture.dependencies is not None
    return _restore_backup(
        fixture.config,
        fixture.receipt_path,
        fixture.destination,
        fixture.drover_config,
        dependencies=fixture.dependencies,
    )


def test_public_restore_boundary_has_no_dependency_override():
    assert tuple(inspect.signature(restore_backup).parameters) == (
        "config",
        "receipt_path",
        "destination",
        "drover_config",
    )
    assert tuple(inspect.signature(validate_restore_request).parameters) == (
        "config",
        "receipt_path",
        "destination",
    )


def test_restore_dependencies_are_private_frozen_and_slotted(tmp_path):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None
    assert not hasattr(fixture.dependencies, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        fixture.dependencies.runtime_guard = lambda _: None
    assert "load_receipt_chain" not in repr(fixture.dependencies)
    assert "_RestoreDependencies" not in archive_package.__all__


def test_restore_validates_receipt_chain_before_runtime_or_destination(tmp_path):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None

    def fail_chain(*args):
        fixture.events.append("receipt-chain")
        raise ValueError("private receipt detail")

    fixture.dependencies = replace(
        fixture.dependencies,
        load_receipt_chain=fail_chain,
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == ["receipt-chain"]
    assert not fixture.destination.exists()


def test_restore_pins_exact_selected_receipt_against_swap_restore(tmp_path):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None

    def swapped_chain(path, directory):
        moved = path.with_name("moved-private.json")
        path.rename(moved)
        write_backup_receipt(directory, _receipt(fixture.config, sessions=3))
        try:
            return load_backup_receipt_chain(path, directory)
        finally:
            path.unlink()
            moved.rename(path)

    fixture.dependencies = replace(
        fixture.dependencies,
        load_receipt_chain=swapped_chain,
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []
    assert not fixture.destination.exists()


def test_restore_rejects_receipt_scope_mismatch_before_runtime(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.config = replace(
        fixture.config,
        store_scope_id="3b11224e-05fa-4db3-90cb-5fa36580ac12",
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_restore_rejects_receipt_path_traversal_before_runtime(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.receipt_path = (
        fixture.config.receipt_directory / ".." / fixture.receipt_path.name
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_restore_reconstructs_only_the_scoped_generation_url(tmp_path):
    fixture = _fixture(tmp_path)
    _restore(fixture)
    expected_generation = f"{_PRIVATE_URL}/generations/{_GENERATION_ID}"
    copy, verify = fixture.process_calls
    assert copy[0] == (
        "--config-file",
        str(fixture.config.remote_pond_config),
        "copy",
        "--from",
        expected_generation,
        "--to",
        str(fixture.destination),
    )
    assert verify[0] == (
        "--config-file",
        str(fixture.config.remote_pond_config),
        "copy",
        "--verify-only",
        "--from",
        expected_generation,
        "--to",
        str(fixture.destination),
    )
    assert str(fixture.config.local_store) not in repr(fixture.process_calls)
    assert str(fixture.config.local_pond_config) not in repr(fixture.process_calls)


def test_restore_with_read_only_credentials_never_runs_a_write_probe(tmp_path):
    fixture = _fixture(tmp_path)
    result = _restore(fixture)
    assert result.verified is True
    assert fixture.events == [
        "baseline",
        "copy-from-generation",
        "verify-only",
        "snapshot-version",
        "snapshot-corpus",
        "current-coverage",
        "runtime-finish",
    ]
    assert "storage-check" not in fixture.events
    assert "sync" not in repr(fixture.process_calls)
    assert "optimize" not in repr(fixture.process_calls)


def test_missing_read_credential_fails_once_and_retains_stopped_destination(tmp_path):
    fixture = _fixture(tmp_path, fault="copy-failure")
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.attempts["copy-from-generation"] == 1
    assert fixture.attempts["verify-only"] == 0
    assert fixture.destination.is_dir()
    assert stat_mode(fixture.destination) == 0o700


def stat_mode(path: Path) -> int:
    return path.stat(follow_symlinks=False).st_mode & 0o777


@pytest.mark.parametrize("location", ["equal", "descendant"])
def test_restore_rejects_live_local_store_containment(tmp_path, location):
    fixture = _fixture(tmp_path)
    fixture.destination = (
        fixture.config.local_store
        if location == "equal"
        else fixture.config.local_store / "restore-child-private"
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_restore_rejects_symlinked_destination_parent(tmp_path):
    fixture = _fixture(tmp_path)
    real_parent = fixture.destination.parent
    alias = tmp_path / "restore-parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    fixture.destination = alias / fixture.destination.name
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


@pytest.mark.parametrize("kind", ["empty-directory", "file", "symlink"])
def test_restore_rejects_every_existing_destination(tmp_path, kind):
    fixture = _fixture(tmp_path)
    if kind == "empty-directory":
        fixture.destination.mkdir(mode=0o700)
    elif kind == "file":
        fixture.destination.write_bytes(b"")
    else:
        fixture.destination.symlink_to(fixture.config.local_store)
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_restore_rejects_nonlocal_destination_before_runtime(tmp_path):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None
    fixture.dependencies = replace(
        fixture.dependencies,
        is_local_filesystem=lambda descriptor, path: False,
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_restore_accepts_owner_controlled_nonwritable_parent_mode(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.destination.parent.chmod(0o755)
    result = _restore(fixture)
    assert result.verified is True
    assert stat_mode(fixture.destination) == 0o700


def test_restore_rejects_group_or_world_writable_parent(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.destination.parent.chmod(0o777)
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.events == []


def test_linux_local_filesystem_reads_mount_metadata_with_a_hard_cap(monkeypatch):
    calls = []

    def bounded_open(path, flags):
        assert path == "/proc/self/mountinfo"
        calls.append(("open", flags))
        return 91

    def bounded_read(descriptor, size):
        assert descriptor == 91
        assert size == backup_restore_module._MAX_MOUNTINFO_BYTES + 1
        calls.append(("read", size))
        return b"36 25 0:32 / /private rw - ext4 disk rw\n"

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("unbounded read")),
    )
    monkeypatch.setattr(backup_restore_module.os, "open", bounded_open)
    monkeypatch.setattr(backup_restore_module.os, "read", bounded_read)
    monkeypatch.setattr(
        backup_restore_module.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )

    assert backup_restore_module._linux_local_filesystem(Path("/private/restore"))
    assert [call[0] for call in calls] == ["open", "read", "close"]


def test_restore_creates_owner_only_destination_once_after_baseline(tmp_path):
    fixture = _fixture(tmp_path)

    def baseline_check():
        assert not fixture.destination.exists()

    fixture.baseline_callback = baseline_check
    result = _restore(fixture)
    assert result.verified is True
    assert fixture.destination.is_dir()
    assert fixture.destination.stat().st_uid == os.geteuid()
    assert stat_mode(fixture.destination) == 0o700


def test_restore_closes_partial_workspace_descriptors_on_creation_failure(
    tmp_path,
    monkeypatch,
):
    fixture = _fixture(tmp_path)
    real_mkdir = backup_restore_module.os.mkdir
    real_open = backup_restore_module.os.open
    real_close = backup_restore_module.os.close
    phase_descriptors = []
    closed = []

    def failing_mkdir(path, mode=0o777, *, dir_fd=None):
        if path == "verify":
            raise OSError("private workspace failure")
        return real_mkdir(path, mode, dir_fd=dir_fd)

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "copy":
            phase_descriptors.append(descriptor)
        return descriptor

    def tracking_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(backup_restore_module.os, "mkdir", failing_mkdir)
    monkeypatch.setattr(backup_restore_module.os, "open", tracking_open)
    monkeypatch.setattr(backup_restore_module.os, "close", tracking_close)

    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert len(phase_descriptors) == 1
    assert phase_descriptors[0] in closed
    with pytest.raises(OSError):
        os.fstat(phase_descriptors[0])
    assert fixture.destination.is_dir()
    assert fixture.attempts["copy-from-generation"] == 0


@pytest.mark.parametrize("restore_empty", [False, True])
def test_restore_requires_the_command_created_destination_to_remain_fresh(
    tmp_path,
    restore_empty,
):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None

    def mutate_fresh_destination():
        unexpected = fixture.destination / "unexpected-private"
        unexpected.write_bytes(b"private")
        if restore_empty:
            unexpected.unlink()
        return UUID("8db84b0e-1d8e-421b-a060-00443d03422f")

    fixture.dependencies = replace(
        fixture.dependencies,
        workspace_uuid=mutate_fresh_destination,
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.destination.is_dir()
    assert fixture.attempts["copy-from-generation"] == 0


def test_restore_detects_destination_parent_swap_before_creation(tmp_path):
    fixture = _fixture(tmp_path)

    def swap_parent():
        parent = fixture.destination.parent
        moved = parent.with_name("moved-parent-private")
        parent.rename(moved)
        parent.mkdir(mode=0o700)

    fixture.baseline_callback = swap_parent
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.attempts["copy-from-generation"] == 0
    assert not fixture.destination.exists()


def test_restore_closes_pinned_descriptors_when_final_request_check_fails(tmp_path):
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None
    parent_descriptors = []

    def swap_parent(descriptor, path):
        parent_descriptors.append(descriptor)
        moved = path.with_name("moved-parent-private")
        path.rename(moved)
        path.mkdir(mode=0o700)
        return True

    fixture.dependencies = replace(
        fixture.dependencies,
        is_local_filesystem=swap_parent,
    )
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert len(parent_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(parent_descriptors[0])
    assert fixture.events == []


@pytest.mark.parametrize("fault", ["destination-swap", "destination-swap-restore"])
def test_restore_detects_destination_entry_swaps_during_process(tmp_path, fault):
    fixture = _fixture(tmp_path, fault=fault)
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.attempts["copy-from-generation"] == 1
    assert fixture.attempts["verify-only"] == 0


def test_restore_detects_selected_receipt_swap_before_destination_creation(tmp_path):
    fixture = _fixture(tmp_path)

    def swap_restore_receipt():
        moved = fixture.receipt_path.with_name("moved-receipt-private.json")
        fixture.receipt_path.rename(moved)
        moved.rename(fixture.receipt_path)

    fixture.baseline_callback = swap_restore_receipt
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert not fixture.destination.exists()


def test_restore_matches_receipt_and_leaves_store_stopped(tmp_path):
    fixture = _fixture(tmp_path)
    result = _restore(fixture)
    assert result == RestoreResult(
        verified=True,
        sessions=2,
        messages=3,
        parts=5,
        current_source_coverage="current",
        health_samples=35,
        health_p95_ms=4.5,
        peak_rss_bytes=300,
        peak_physical_bytes=600,
        swap_delta_bytes=20,
    )
    assert restore_summary(result) == {
        "schema_version": 1,
        "verified": True,
        "sessions": 2,
        "messages": 3,
        "parts": 5,
        "current_source_coverage": "current",
        "health_samples": 35,
        "health_p95_ms": 4.5,
        "peak_rss_bytes": 300,
        "peak_physical_bytes": 600,
        "swap_delta_bytes": 20,
        "store_started": False,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "inventory-digest",
        "sessions",
        "messages",
        "parts",
        "disallowed",
        "logical-duplicates",
    ],
)
def test_restore_rejects_every_receipt_or_safety_mismatch(tmp_path, fault):
    fixture = _fixture(tmp_path, fault=fault)
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert fixture.destination.is_dir()
    assert fixture.attempts["runtime-finish"] == 0


@pytest.mark.parametrize("status", ["current", "stale", "unavailable"])
def test_current_source_coverage_is_separate_from_corpus_integrity(tmp_path, status):
    fixture = _fixture(tmp_path)
    fixture.current_status = status
    result = _restore(fixture)
    assert result.verified is True
    assert result.current_source_coverage == status


def test_current_source_discovery_failure_is_optional_and_unavailable(tmp_path):
    fixture = _fixture(tmp_path, fault="coverage-error")
    result = _restore(fixture)
    assert result.verified is True
    assert result.current_source_coverage == "unavailable"


def test_current_source_outcome_cannot_hide_corrupt_corpus(tmp_path):
    fixture = _fixture(tmp_path, fault="inventory-digest")
    fixture.current_status = "unavailable"
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)
    assert "current-coverage" not in fixture.events


@pytest.mark.parametrize(
    ("identities", "ineligible", "expected"),
    [
        (
            (("claude-code", "claude-private"), ("codex-cli", "codex-private")),
            0,
            "current",
        ),
        ((("claude-code", "claude-private"),), 0, "stale"),
        (
            (
                ("claude-code", "claude-private"),
                ("codex-cli", "codex-private"),
                ("codex-cli", "newer-private"),
            ),
            0,
            "stale",
        ),
        (
            (
                ("claude-code", "claude-private"),
                ("codex-cli", "codex-private"),
                ("codex-cli", "ineligible-private"),
            ),
            1,
            "current",
        ),
    ],
)
def test_production_current_source_coverage_is_metadata_only_and_separate(
    tmp_path,
    monkeypatch,
    identities,
    ineligible,
    expected,
):
    fixture = _fixture(tmp_path)
    current = _native_inventory(identities)
    monkeypatch.setattr(
        backup_restore_module,
        "discover_native_history_inventory",
        lambda *args, **kwargs: current,
    )
    receipt = replace(
        _receipt(fixture.config),
        source_not_archive_eligible=ineligible,
    )
    assert (
        backup_restore_module._production_current_source_coverage(
            _snapshot(),
            receipt,
            fixture.drover_config,
            "host-private",
        )
        == expected
    )


def test_production_current_source_discovery_failure_is_unavailable(
    tmp_path,
    monkeypatch,
):
    fixture = _fixture(tmp_path)

    def unavailable(*args, **kwargs):
        raise OSError("private metadata source unavailable")

    monkeypatch.setattr(
        backup_restore_module,
        "discover_native_history_inventory",
        unavailable,
    )
    assert (
        backup_restore_module._production_current_source_coverage(
            _snapshot(),
            _receipt(fixture.config),
            fixture.drover_config,
            "host-private",
        )
        == "unavailable"
    )


def test_restore_applies_limits_and_callbacks_to_all_four_processes(tmp_path):
    fixture = _fixture(tmp_path)
    _restore(fixture)
    expected = ResourceLimits(1024, 2048, 64)
    assert fixture.limits == [
        ("copy-from-generation", expected),
        ("verify-only", expected),
        ("snapshot-version", expected),
        ("snapshot-corpus", expected),
    ]
    assert fixture.attempts["runtime-sample"] == 4


@pytest.mark.parametrize(
    ("fault", "verify_attempts", "snapshot_attempts"),
    [
        ("copy-from-generation-physical-none", 0, 0),
        ("copy-from-generation-rss", 0, 0),
        ("verify-only-physical-none", 1, 0),
        ("verify-only-swap", 1, 0),
    ],
)
def test_restore_stops_at_the_first_returned_process_resource_breach(
    tmp_path,
    fault,
    verify_attempts,
    snapshot_attempts,
):
    fixture = _fixture(tmp_path, fault=fault)
    with pytest.raises(BackupRestoreError, match=f"^{_RESOURCE_ERROR}$"):
        _restore(fixture)
    assert fixture.attempts["verify-only"] == verify_attempts
    assert fixture.attempts["snapshot-version"] == snapshot_attempts


@pytest.mark.parametrize(
    "fault",
    [
        "copy-resource",
        "verify-resource",
        "snapshot-version-resource",
        "snapshot-corpus-resource",
        "copy-from-generation-physical-none",
        "verify-only-physical-none",
        "snapshot-physical-none",
        "copy-from-generation-rss",
        "verify-only-swap",
        "snapshot-rss",
        "finish-resource",
        "runtime-resource",
    ],
)
def test_restore_maps_every_resource_breach_to_fixed_resource_error(tmp_path, fault):
    fixture = _fixture(tmp_path, fault=fault)
    with pytest.raises(BackupRestoreError, match=f"^{_RESOURCE_ERROR}$"):
        _restore(fixture)
    if fixture.destination.exists():
        assert fixture.destination.is_dir()


@pytest.mark.parametrize(
    "fault",
    [
        "copy-failure",
        "verify-failure",
        "finish-health",
        "finish-low-samples",
        "runtime-health",
    ],
)
def test_restore_maps_every_nonresource_failure_to_fixed_restore_error(tmp_path, fault):
    fixture = _fixture(tmp_path, fault=fault)
    with pytest.raises(BackupRestoreError, match=f"^{_RESTORE_ERROR}$"):
        _restore(fixture)


def test_restore_finishes_runtime_only_after_all_comparisons(tmp_path):
    fixture = _fixture(tmp_path)
    _restore(fixture)
    assert fixture.events[-2:] == ["current-coverage", "runtime-finish"]


def test_validate_restore_request_is_local_only_and_does_not_create(tmp_path):
    fixture = _fixture(tmp_path)
    validate_restore_request(
        fixture.config,
        fixture.receipt_path,
        fixture.destination,
    )
    assert not fixture.destination.exists()


def test_restore_public_outputs_and_errors_never_disclose_private_values(
    tmp_path, caplog, capsys
):
    fixture = _fixture(tmp_path)
    result = _restore(fixture)
    public = repr(result) + repr(restore_summary(result))
    captured = capsys.readouterr()
    public += captured.out + captured.err + caplog.text
    for value in (
        *_PRIVATE_VALUES,
        str(fixture.destination),
        str(fixture.receipt_path),
    ):
        assert value not in public

    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    failed = _fixture(failed_root, fault="copy-failure")
    with pytest.raises(BackupRestoreError) as captured_error:
        _restore(failed)
    assert str(captured_error.value) == _RESTORE_ERROR
    for value in (*_PRIVATE_VALUES, str(failed.destination), str(failed.receipt_path)):
        assert value not in str(captured_error.value)


def test_archive_package_exports_only_completed_public_restore_interfaces():
    assert archive_package.BackupRestoreError is BackupRestoreError
    assert archive_package.RestoreResult is RestoreResult
    assert archive_package.restore_backup is restore_backup
    assert archive_package.restore_summary is restore_summary
    assert archive_package.validate_restore_request is validate_restore_request
    assert {
        "BackupRestoreError",
        "RestoreResult",
        "restore_backup",
        "restore_summary",
        "validate_restore_request",
    } <= set(archive_package.__all__)
