"""Immutable, fail-closed Pond backup generation orchestration."""

from __future__ import annotations

import inspect
import json
import os
import stat
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

import drover.server.archive as archive_package
import drover.server.archive.backup_runtime as backup_runtime_module
from drover.config import default_config
from drover.server.archive.backup_config import BackupConfig
from drover.server.archive.backup_preflight import (
    BackupPreflightError,
    BackupPreflightResult,
)
from drover.server.archive.backup_receipt import (
    BackupReceipt,
    CollisionCounts,
    _latest_backup_receipt_at,
    _write_backup_receipt_at,
    write_backup_receipt,
)
from drover.server.archive.backup_run import (
    BackupRunError,
    _BackupDependencies,
    _production_backup_dependencies,
    _run_backup,
    backup_run_summary,
    run_backup,
)
from drover.server.archive.backup_runtime import (
    BackupLock,
    BackupRuntimeError,
    RuntimeEvidence,
)
from drover.server.archive.coverage import (
    RegistryCandidate,
    build_coverage_report,
    coverage_summary,
)
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
    private_json_sha256,
    write_private_json,
)
from drover.server.archive.pond_process import (
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
)
from drover.server.archive.pond_snapshot import PondCorpusCounts, PondStoreSnapshot

_GENERATION_ID = UUID("48d862c3-787a-4970-9a1c-842f9098473e")
_PREVIOUS_ID = UUID("c632db33-0c57-4762-a42c-47b519c48a53")
_RACE_ID = UUID("558ad3bc-c111-4ee6-935a-1ea640315d43")
_SCOPE_ID = "536b300b-24ff-4dda-a3e9-52fde1154b59"
_CAPTURED_AT = "2026-08-29T12:00:00Z"
_PRIVATE_URL = (
    "s3+https://account-private.r2.cloudflarestorage.com/"
    "bucket-private/prefix-private"
)
_PRIVATE_BINDING = "credential-private"
_PRIVATE_MESSAGE = "remote-private-message"
_PRIVATE_TRANSCRIPT = "PRIVATE TRANSCRIPT BODY"
_ERRORS = {
    "archive backup preflight failed",
    "archive backup local changed",
    "archive backup storage unavailable",
    "archive backup copy failed",
    "archive backup verify failed",
    "archive backup receipt failed",
    "archive backup resource limit",
}


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
    local_store.mkdir()
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


def _native_inventory(*, fingerprint: str = "a" * 64) -> NativeInventory:
    return NativeInventory(
        schema_version=2,
        captured_at=_CAPTURED_AT,
        host_id="host-private",
        records=(
            NativeInventoryRecord(
                source_agent="claude-code",
                session_id="claude-private",
                updated_at="2026-08-29T10:03:00Z",
                size_bytes=100,
                source_copies=1,
                source_fingerprint=fingerprint,
            ),
            NativeInventoryRecord(
                source_agent="codex-cli",
                session_id="codex-private",
                updated_at="2026-08-29T11:03:00Z",
                size_bytes=200,
                source_copies=1,
                source_fingerprint="b" * 64,
            ),
        ),
    )


def _pond_inventory(
    *,
    records: tuple[PondInventoryRecord, ...] | None = None,
    captured_at: str = _CAPTURED_AT,
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
                message_count=1,
                first_message_at="2026-08-29T10:01:00Z",
                last_message_at="2026-08-29T10:01:00Z",
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
    resource_evidence: PondResourceEvidence | None = None,
) -> PondStoreSnapshot:
    return PondStoreSnapshot(
        inventory or _pond_inventory(),
        counts
        or PondCorpusCounts(
            sessions=2,
            messages=2,
            parts=2,
            disallowed_sessions=0,
            logical_duplicate_groups=0,
            sessions_in_logical_duplicate_groups=0,
        ),
        resource_evidence or PondResourceEvidence(300, 600, 10),
    )


def _empty_snapshot() -> PondStoreSnapshot:
    return PondStoreSnapshot(
        PondInventory(1, _CAPTURED_AT, "0.16.3", ()),
        PondCorpusCounts(0, 0, 0, 0, 0, 0),
        PondResourceEvidence(400, 800, 20),
    )


def _coverage(
    source: NativeInventory,
    pond: PondInventory,
    *,
    registry_suffix: str = "",
):
    registry = (
        RegistryCandidate(
            "wrapper-claude" + registry_suffix,
            source.host_id,
            "claude-code",
            "claude-private",
        ),
        RegistryCandidate(
            "wrapper-codex" + registry_suffix,
            source.host_id,
            "codex",
            "codex-private",
        ),
    )
    return build_coverage_report(registry, (source,), pond)


def _preflight_result(
    workspace: Path,
    *,
    source: NativeInventory | None = None,
    snapshot: PondStoreSnapshot | None = None,
    registry_suffix: str = "",
    artifact_fault: str | None = None,
    resource_evidence: PondResourceEvidence | None = None,
    eligibility_receipts_sha256: str | None = None,
) -> BackupPreflightResult:
    source_value = source or _native_inventory()
    snapshot_value = snapshot or _snapshot()
    report = _coverage(
        source_value,
        snapshot_value.root_inventory,
        registry_suffix=registry_suffix,
    )
    source_path = workspace / "source-inventory.json"
    pond_path = workspace / "pond-inventory.json"
    coverage_path = workspace / "coverage-report.json"
    source_payload = source_value.to_wire()
    coverage_payload = report.to_wire()
    if artifact_fault == "source":
        source_payload = _native_inventory(fingerprint="e" * 64).to_wire()
    if artifact_fault == "coverage":
        coverage_payload = {**coverage_payload, "ready_for_next_writer": False}
    if artifact_fault == "source-noncanonical":
        source_path.write_text(
            json.dumps(source_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        source_path.chmod(0o600)
    else:
        write_private_json(source_path, source_payload)
    write_private_json(pond_path, snapshot_value.root_inventory.to_wire())
    write_private_json(coverage_path, coverage_payload)
    return BackupPreflightResult(
        source_inventory=source_value,
        pond_snapshot=snapshot_value,
        coverage=report,
        coverage_summary=coverage_summary(report),
        source_inventory_path=source_path,
        pond_inventory_path=pond_path,
        coverage_report_path=coverage_path,
        source_not_archive_eligible=0,
        resource_evidence=resource_evidence or PondResourceEvidence(500, 900, 40),
        eligibility_receipts_sha256=eligibility_receipts_sha256
        or private_json_sha256([]),
    )


def _receipt(config: BackupConfig, generation_id: UUID) -> BackupReceipt:
    return BackupReceipt(
        schema_version=1,
        created_at="2026-08-29T11:00:00Z",
        pond_version="0.16.3",
        store_scope_id=config.store_scope_id,
        generation_id=str(generation_id),
        previous_receipt_sha256=None,
        source_inventory_sha256="1" * 64,
        local_pond_inventory_sha256="2" * 64,
        remote_pond_inventory_sha256="3" * 64,
        coverage_report_sha256="4" * 64,
        sessions=2,
        messages=2,
        parts=2,
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
        self.samples = 0

    def capture_baseline(self) -> None:
        self.fixture.events.append("baseline")
        self.fixture.attempts["baseline"] += 1
        if self.fixture.fault == "baseline_health":
            raise BackupRuntimeError("archive backup preflight failed")

    def sample(self) -> None:
        self.samples += 1
        self.fixture.attempts["runtime-sample"] += 1
        if self.fixture.active_phase != "copy":
            return
        category = {
            "health_failure": "archive backup preflight failed",
            "dropped_events": "archive backup local changed",
            "listener_restart": "archive backup local changed",
            "runtime_resource": "archive backup resource limit",
        }.get(self.fixture.fault)
        if category is not None:
            raise BackupRuntimeError(category)

    def finish(self) -> RuntimeEvidence:
        self.fixture.events.append("runtime-finish")
        self.fixture.attempts["runtime-finish"] += 1
        if self.fixture.fault == "finish_resource":
            raise BackupRuntimeError("archive backup resource limit")
        return RuntimeEvidence(35, 4.5)


@dataclass(slots=True)
class _Fixture:
    config: BackupConfig
    drover_config: object
    fault: str | None
    commands: list[str]
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]]
    events: list[str]
    attempts: Counter[str]
    written_receipts: list[BackupReceipt]
    applied_limits: list[tuple[str, ResourceLimits | None]]
    latest_calls: int = 0
    active_phase: str | None = None
    runtime: _Runtime | None = None
    dependencies: _BackupDependencies | None = None
    previous: BackupReceipt | None = None
    swapped_paths: list[Path] | None = None


_WORKSPACE_PHASES = (
    "before",
    "storage",
    "remote-empty",
    "copy",
    "verify",
    "after",
    "remote-after",
)


def _replace_workspace_directory(fixture: _Fixture, target: str) -> None:
    if fixture.swapped_paths is None:
        fixture.swapped_paths = []
    receipt = fixture.config.receipt_directory
    runs = receipt / ".backup-runs"
    generation = runs / str(_GENERATION_ID)
    if target == "receipt":
        selected = receipt
        replacement_parent = receipt.parent
    elif target == "runs":
        selected = runs
        replacement_parent = receipt
    elif target == "generation":
        selected = generation
        replacement_parent = runs
    elif target in _WORKSPACE_PHASES:
        selected = generation / target
        replacement_parent = generation
    else:
        raise AssertionError("unknown workspace replacement target")
    moved = (
        replacement_parent / f"relocated-{selected.name}-{len(fixture.swapped_paths)}"
    )
    selected.rename(moved)
    selected.mkdir(mode=0o700)
    if target == "runs":
        generation = selected / str(_GENERATION_ID)
        generation.mkdir(mode=0o700)
        for phase in _WORKSPACE_PHASES:
            (generation / phase).mkdir(mode=0o700)
    elif target == "generation":
        for phase in _WORKSPACE_PHASES:
            (selected / phase).mkdir(mode=0o700)
    fixture.swapped_paths.append(moved)


def _maybe_replace_workspace(fixture: _Fixture, point: str) -> None:
    fault = fixture.fault
    if fixture.swapped_paths:
        return
    if fault == f"workspace-{point}":
        target = point if point in {"receipt", "runs", "generation"} else point
        _replace_workspace_directory(fixture, target)


def _remote_snapshot_for_fault(fault: str | None) -> PondStoreSnapshot:
    local = _snapshot()
    if fault == "remote_missing":
        record = local.root_inventory.records[:1]
        return _snapshot(
            inventory=_pond_inventory(records=record),
            counts=PondCorpusCounts(1, 1, 1, 0, 0, 0),
        )
    if fault == "remote_extra":
        extra = PondInventoryRecord(
            session_id="extra-private",
            source_agent="codex-cli",
            created_at="2026-08-29T12:00:00Z",
            message_count=1,
            first_message_at="2026-08-29T12:01:00Z",
            last_message_at="2026-08-29T12:01:00Z",
        )
        return _snapshot(
            inventory=_pond_inventory(records=(*local.root_inventory.records, extra)),
            counts=PondCorpusCounts(3, 3, 3, 0, 0, 0),
        )
    if fault == "remote_disallowed":
        return _snapshot(counts=PondCorpusCounts(2, 2, 2, 1, 0, 0))
    if fault == "remote_duplicate":
        return _snapshot(counts=PondCorpusCounts(2, 2, 2, 0, 1, 2))
    return local


def _fixture(
    tmp_path: Path,
    *,
    fault: str | None = None,
    with_previous: bool = False,
) -> _Fixture:
    config = _config(tmp_path)
    fixture = _Fixture(
        config=config,
        drover_config=replace(
            default_config(), duckdb_path=tmp_path / "registry-private.duckdb"
        ),
        fault=fault,
        commands=[],
        process_calls=[],
        events=[],
        attempts=Counter(),
        written_receipts=[],
        applied_limits=[],
        swapped_paths=[],
    )
    fixture.runtime = _Runtime(fixture)

    if with_previous:
        fixture.previous = _receipt(config, _PREVIOUS_ID)
        write_backup_receipt(config.receipt_directory, fixture.previous)

    if fault == "receipt_collision":
        collision = config.receipt_directory / f"backup-{_GENERATION_ID}.json"
        write_private_json(collision, {})

    if fault == "receipt_fork":
        write_backup_receipt(config.receipt_directory, _receipt(config, _PREVIOUS_ID))
        write_backup_receipt(config.receipt_directory, _receipt(config, _RACE_ID))

    @contextmanager
    def lock_factory(receipt_directory: Path):
        assert receipt_directory == config.receipt_directory
        fixture.events.append("lock-enter")
        fixture.attempts["lock"] += 1
        with BackupLock(receipt_directory) as lock:
            if fault == "workspace-receipt":
                _replace_workspace_directory(fixture, "receipt")
            try:
                yield lock
            finally:
                fixture.events.append("lock-exit")

    def runtime_guard(drover_config):
        assert drover_config is fixture.drover_config
        assert fixture.runtime is not None
        return fixture.runtime

    def preflight(
        received_config,
        drover_config,
        workspace,
        runtime,
        *,
        resource_limits=None,
    ):
        assert received_config is config
        assert drover_config is fixture.drover_config
        assert runtime is fixture.runtime
        before = fixture.attempts["preflight-before"] == 0
        phase = "preflight-before" if before else "preflight-after"
        fixture.attempts[phase] += 1
        fixture.commands.append("local-sync-dry-run")
        prefix = "before" if before else "after"
        for subprocess_phase in ("snapshot-version", "corpus-snapshot", "dry-run"):
            fixture.applied_limits.append(
                (f"{prefix}-{subprocess_phase}", resource_limits)
            )
        if before:
            _maybe_replace_workspace(fixture, "before")
            _maybe_replace_workspace(fixture, "runs")
            _maybe_replace_workspace(fixture, "generation")
            if fault == "preflight":
                raise BackupPreflightError("archive backup preflight failed")
            if fault in {"resource-before-version", "resource-before-corpus"}:
                raise PondProcessError("resource")
            return _preflight_result(
                workspace,
                artifact_fault="source" if fault == "before_source_artifact" else None,
                resource_evidence=PondResourceEvidence(
                    900,
                    None if fault == "physical_none_before" else 700,
                    30,
                ),
            )
        fixture.commands.append("local-postflight")
        _maybe_replace_workspace(fixture, "after")
        if fault in {"resource-after-version", "resource-after-corpus"}:
            raise PondProcessError("resource")
        if fault in {"eligibility_rebinding", "collision"}:
            raise BackupPreflightError("archive backup preflight failed")
        source = (
            _native_inventory(fingerprint="c" * 64)
            if fault == "source_fingerprint"
            else _native_inventory()
        )
        local_snapshot = _snapshot()
        if fault == "local_root":
            changed = replace(
                local_snapshot.root_inventory.records[0],
                created_at="2026-08-29T09:59:59Z",
            )
            local_snapshot = _snapshot(
                inventory=_pond_inventory(
                    records=(changed, local_snapshot.root_inventory.records[1])
                )
            )
        if fault == "corpus_count":
            local_snapshot = _snapshot(counts=PondCorpusCounts(2, 2, 3, 0, 0, 0))
        return _preflight_result(
            workspace,
            source=source,
            snapshot=local_snapshot,
            registry_suffix="-changed" if fault == "coverage_binding" else "",
            artifact_fault=(
                "source"
                if fault == "source_artifact"
                else (
                    "coverage"
                    if fault == "coverage_artifact"
                    else (
                        "source-noncanonical"
                        if fault == "source_artifact_noncanonical"
                        else None
                    )
                )
            ),
            resource_evidence=PondResourceEvidence(
                500,
                None if fault == "physical_none_after" else 900,
                40,
            ),
            eligibility_receipts_sha256=(
                "f" * 64 if fault == "eligibility_exact_bytes" else None
            ),
        )

    def run_process(
        binary,
        arguments,
        *,
        timeout_seconds,
        run_directory,
        label,
        **kwargs,
    ):
        assert binary == config.pond_binary
        fixture.commands.append(label)
        fixture.attempts[label] += 1
        fixture.active_phase = label
        phase_name = {
            "storage-check": "storage",
            "copy": "copy",
            "verify-only": "verify",
        }[label]
        _maybe_replace_workspace(fixture, phase_name)
        arguments_tuple = tuple(arguments)
        fixture.process_calls.append((arguments_tuple, dict(kwargs)))
        fixture.applied_limits.append((label, kwargs.get("resource_limits")))
        callback = kwargs.get("progress_callback")
        assert callback is not None
        callback()
        fixture.active_phase = None
        phase_fault = (
            {
                "storage-check": {
                    "storage_timeout": "timeout",
                    "storage_overflow": "size",
                    "storage_resource": "resource",
                },
                "copy": {
                    "copy_timeout": "timeout",
                    "copy_overflow": "size",
                    "copy_resource": "resource",
                },
                "verify-only": {
                    "verify_timeout": "timeout",
                    "verify_overflow": "size",
                    "verify_resource": "resource",
                },
            }
            .get(label, {})
            .get(fault)
        )
        if phase_fault is not None:
            raise PondProcessError(phase_fault)
        stdout = Path(run_directory) / f"{label}.stdout"
        stderr = Path(run_directory) / f"{label}.stderr"
        if label == "storage-check":
            payload = {
                "ok": fault != "storage_ok_false",
                "exit_code": 9 if fault == "storage_json_exit" else 0,
                "failure": (
                    {"message": _PRIVATE_MESSAGE}
                    if fault == "storage_json_failure"
                    else None
                ),
                "url": _PRIVATE_URL + "/generations/private",
                "binding": _PRIVATE_BINDING,
                "message": _PRIVATE_MESSAGE,
            }
            if fault == "storage_malformed":
                stdout.write_text("{private-invalid", encoding="utf-8")
                stdout.chmod(0o600)
            else:
                write_private_json(stdout, payload)
        else:
            stdout.write_bytes(b"")
            stdout.chmod(0o600)
        stderr.write_bytes(_PRIVATE_TRANSCRIPT.encode("utf-8"))
        stderr.chmod(0o600)
        returncode = 0
        if (label, fault) in {
            ("storage-check", "storage_process_exit"),
            ("copy", "copy_exit"),
            ("verify-only", "verify_exit_6"),
        }:
            returncode = 6 if label == "verify-only" else 17
        physical = 128
        rss = 64
        swap = 0
        if label == "storage-check":
            rss = 700
            physical = 1000
            swap = 50
        if label == "copy" and fault == "physical_none":
            physical = None
        if label == "storage-check" and fault == "physical_none_storage":
            physical = None
        if label == "verify-only" and fault == "physical_none_verify":
            physical = None
        if label == "copy" and fault == "rss_ceiling":
            rss = config.max_rss_bytes + 1
        if label == "verify-only" and fault == "physical_ceiling":
            physical = config.max_physical_bytes + 1
        if label == "verify-only" and fault == "swap_ceiling":
            swap = config.max_swap_growth_bytes + 1
        return PondProcessResult(
            returncode,
            20 if label == "copy" else 10,
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
        assert timeout_seconds == config.copy_timeout_seconds
        assert progress_callback is not None
        remote_after = fixture.attempts["remote-empty-snapshot"] == 1
        label = "remote-postflight" if remote_after else "remote-empty-snapshot"
        prefix = "final" if remote_after else "empty"
        fixture.applied_limits.extend(
            (
                (f"{prefix}-snapshot-version", resource_limits),
                (f"{prefix}-corpus-snapshot", resource_limits),
            )
        )
        _maybe_replace_workspace(
            fixture,
            "remote-after" if remote_after else "remote-empty",
        )
        fixture.commands.append(label)
        fixture.attempts[label] += 1
        fixture.active_phase = label
        progress_callback()
        fixture.active_phase = None
        if fault == "empty_snapshot_failure" and not remote_after:
            raise ValueError(_PRIVATE_URL)
        if not remote_after and fault in {
            "resource-empty-version",
            "resource-empty-corpus",
        }:
            raise PondProcessError("resource")
        if fault == "remote_snapshot_failure" and remote_after:
            raise ValueError(_PRIVATE_URL)
        if remote_after and fault in {
            "resource-final-version",
            "resource-final-corpus",
        }:
            raise PondProcessError("resource")
        if not remote_after:
            value = _snapshot() if fault == "nonempty_generation" else _empty_snapshot()
            value = replace(
                value,
                resource_evidence=PondResourceEvidence(
                    400,
                    None if fault == "physical_none_empty" else 1800,
                    20,
                ),
            )
        else:
            value = _remote_snapshot_for_fault(fault)
            value = replace(
                value,
                resource_evidence=PondResourceEvidence(
                    600,
                    None if fault == "physical_none_final" else 1000,
                    25,
                ),
            )
        write_private_json(
            Path(workspace) / "pond-inventory.json",
            value.root_inventory.to_wire(),
        )
        return value

    def latest_receipt(
        receipt_directory,
        receipt_descriptor,
        receipt_identity,
        store_scope_id,
    ):
        fixture.latest_calls += 1
        assert receipt_directory == config.receipt_directory
        assert store_scope_id == config.store_scope_id
        if fault == "workspace-predecessor" and fixture.latest_calls == 1:
            _replace_workspace_directory(fixture, "runs")
        if fault == "parent_swap" and fixture.latest_calls == 1:
            relocated = tmp_path / "relocated-receipts-private"
            config.receipt_directory.rename(relocated)
            config.receipt_directory.mkdir(mode=0o700)
            return None
        if fault == "chain_race" and fixture.latest_calls == 2:
            return _receipt(config, _RACE_ID)
        if fault == "wrong_scope_predecessor":
            return replace(
                _receipt(config, _PREVIOUS_ID),
                store_scope_id="sha256:" + "f" * 64,
            )
        return _latest_backup_receipt_at(
            receipt_directory,
            receipt_descriptor,
            receipt_identity,
            store_scope_id,
        )

    def write_receipt(
        receipt_directory,
        receipt_descriptor,
        receipt_identity,
        receipt,
        *,
        before_publish,
    ):
        fixture.attempts["receipt-write"] += 1
        final = receipt_directory / f"backup-{receipt.generation_id}.json"
        if fault != "receipt_collision":
            assert not final.exists()
        assert fixture.attempts["runtime-finish"] == 1
        assert fixture.latest_calls == 2
        if fault == "workspace-writer":
            _replace_workspace_directory(fixture, "generation")
        if fault == "receipt_write":
            raise ValueError(_PRIVATE_MESSAGE)
        path = _write_backup_receipt_at(
            receipt_directory,
            receipt_descriptor,
            receipt_identity,
            receipt,
            before_publish=before_publish,
        )
        fixture.written_receipts.append(receipt)
        return path

    fixture.dependencies = _BackupDependencies(
        backup_lock=lock_factory,
        uuid4=lambda: _GENERATION_ID,
        runtime_guard=runtime_guard,
        run_preflight=preflight,
        run_pond_process=run_process,
        capture_pond_snapshot=capture_snapshot,
        latest_receipt=latest_receipt,
        write_receipt=write_receipt,
        now=lambda: datetime(2026, 8, 29, 12, 30, tzinfo=timezone.utc),
    )
    return fixture


def _run(fixture: _Fixture) -> BackupReceipt:
    assert fixture.dependencies is not None
    return _run_backup(
        fixture.config,
        fixture.drover_config,
        dependencies=fixture.dependencies,
    )


def test_public_runner_has_no_dependency_override_and_package_exports_are_narrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    assert fixture.dependencies is not None
    monkeypatch.setattr(
        "drover.server.archive.backup_run._production_backup_dependencies",
        lambda: fixture.dependencies,
    )

    assert tuple(inspect.signature(run_backup).parameters) == (
        "config",
        "drover_config",
    )
    assert {
        "BackupRunError",
        "backup_run_summary",
        "run_backup",
    } <= set(archive_package.__all__)
    assert "_BackupDependencies" not in archive_package.__all__
    assert run_backup(fixture.config, fixture.drover_config).result == "verified"


def test_production_dependency_construction_is_lazy_and_private() -> None:
    dependencies = _production_backup_dependencies()

    assert type(dependencies) is _BackupDependencies
    assert dependencies.__slots__ == (
        "backup_lock",
        "uuid4",
        "runtime_guard",
        "run_preflight",
        "run_pond_process",
        "capture_pond_snapshot",
        "latest_receipt",
        "write_receipt",
        "now",
    )
    assert _PRIVATE_URL not in repr(dependencies)


def test_applied_backup_runs_every_gate_once_and_writes_one_linked_receipt(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, with_previous=True)

    receipt = _run(fixture)

    assert fixture.commands == [
        "local-sync-dry-run",
        "storage-check",
        "remote-empty-snapshot",
        "copy",
        "verify-only",
        "local-sync-dry-run",
        "local-postflight",
        "remote-postflight",
    ]
    assert fixture.events == [
        "lock-enter",
        "baseline",
        "runtime-finish",
        "lock-exit",
    ]
    assert fixture.latest_calls == 2
    assert fixture.attempts["receipt-write"] == 1
    assert fixture.previous is not None
    assert receipt.previous_receipt_sha256 == private_json_sha256(
        fixture.previous.to_wire()
    )
    assert receipt.result == "verified"
    assert receipt.generation_id == str(_GENERATION_ID)
    assert receipt.created_at == "2026-08-29T12:30:00Z"
    assert receipt.copy_duration_ms == 20
    assert receipt.verify_duration_ms == 10
    assert receipt.peak_rss_bytes == 900
    assert receipt.peak_physical_bytes == 1800
    assert receipt.swap_delta_bytes == 50
    assert len(fixture.written_receipts) == 1
    assert len(list(fixture.config.receipt_directory.glob("backup-*.json"))) == 2
    run_directory = (
        fixture.config.receipt_directory / ".backup-runs" / str(_GENERATION_ID)
    )
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700


def test_unlock_cleanup_cannot_override_a_successfully_published_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)

    def fail_unlock(_descriptor: int) -> None:
        raise BackupRuntimeError("archive backup preflight failed")

    monkeypatch.setattr(backup_runtime_module, "release_flock", fail_unlock)

    receipt = _run(fixture)

    final = fixture.config.receipt_directory / f"backup-{receipt.generation_id}.json"
    assert final.is_file()
    assert fixture.written_receipts == [receipt]


def test_runner_uses_exact_remote_config_argv_callbacks_and_limits(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    _run(fixture)

    generation = f"{_PRIVATE_URL}/generations/{_GENERATION_ID}"
    assert [call[0] for call in fixture.process_calls] == [
        (
            "--config-file",
            str(fixture.config.remote_pond_config),
            "storage",
            "check",
            generation,
            "--format",
            "json",
        ),
        (
            "--config-file",
            str(fixture.config.remote_pond_config),
            "copy",
            "--from",
            str(fixture.config.local_store),
            "--to",
            generation,
        ),
        (
            "--config-file",
            str(fixture.config.remote_pond_config),
            "copy",
            "--verify-only",
            "--from",
            str(fixture.config.local_store),
            "--to",
            generation,
        ),
    ]
    expected_limits = ResourceLimits(1024, 2048, 64)
    for _, kwargs in fixture.process_calls:
        assert kwargs["resource_limits"] == expected_limits
        assert kwargs["progress_callback"] is not None
        assert "env" not in kwargs


def test_applied_backup_limits_all_thirteen_processes_and_aggregates_all_peaks(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    receipt = _run(fixture)

    expected_limits = ResourceLimits(1024, 2048, 64)
    assert len(fixture.applied_limits) == 13
    assert all(limits == expected_limits for _, limits in fixture.applied_limits)
    assert receipt.peak_rss_bytes == 900
    assert receipt.peak_physical_bytes == 1800
    assert receipt.swap_delta_bytes == 50


@pytest.mark.parametrize(
    "fault",
    [
        "storage_ok_false",
        "storage_json_exit",
        "storage_json_failure",
        "storage_malformed",
        "storage_process_exit",
        "storage_timeout",
        "storage_overflow",
        "empty_snapshot_failure",
        "nonempty_generation",
    ],
)
def test_storage_and_empty_generation_fail_closed_without_copy(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=r"^archive backup storage unavailable$"):
        _run(fixture)

    assert fixture.attempts["storage-check"] <= 1
    assert fixture.attempts["remote-empty-snapshot"] <= 1
    assert fixture.attempts["copy"] == 0
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    ("fault", "category", "phase"),
    [
        ("copy_exit", "archive backup copy failed", "copy"),
        ("copy_timeout", "archive backup copy failed", "copy"),
        ("copy_overflow", "archive backup copy failed", "copy"),
        ("verify_exit_6", "archive backup verify failed", "verify-only"),
        ("verify_timeout", "archive backup verify failed", "verify-only"),
        ("verify_overflow", "archive backup verify failed", "verify-only"),
        (
            "remote_snapshot_failure",
            "archive backup verify failed",
            "remote-postflight",
        ),
    ],
)
def test_copy_verify_and_remote_capture_never_retry_or_publish(
    tmp_path: Path, fault: str, category: str, phase: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=rf"^{category}$"):
        _run(fixture)

    assert fixture.attempts[phase] == 1
    assert all(
        value <= 1 for key, value in fixture.attempts.items() if key != "runtime-sample"
    )
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    "fault",
    [
        "copy_resource",
        "storage_resource",
        "verify_resource",
        "physical_none",
        "physical_none_before",
        "physical_none_storage",
        "physical_none_empty",
        "physical_none_verify",
        "physical_none_after",
        "physical_none_final",
        "rss_ceiling",
        "physical_ceiling",
        "swap_ceiling",
        "runtime_resource",
        "finish_resource",
    ],
)
def test_missing_or_exceeded_resource_evidence_never_publishes(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=r"^archive backup resource limit$"):
        _run(fixture)

    assert fixture.attempts["copy"] <= 1
    assert fixture.attempts["verify-only"] <= 1
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    "fault",
    [
        "resource-before-version",
        "resource-before-corpus",
        "resource-empty-version",
        "resource-empty-corpus",
        "resource-after-version",
        "resource-after-corpus",
        "resource-final-version",
        "resource-final-corpus",
    ],
)
def test_snapshot_resource_failure_in_every_applied_phase_keeps_fixed_category(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=r"^archive backup resource limit$"):
        _run(fixture)

    assert fixture.attempts["receipt-write"] == 0
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    ("fault", "category"),
    [
        ("baseline_health", "archive backup preflight failed"),
        ("preflight", "archive backup preflight failed"),
        ("before_source_artifact", "archive backup preflight failed"),
        ("health_failure", "archive backup preflight failed"),
        ("dropped_events", "archive backup local changed"),
        ("listener_restart", "archive backup local changed"),
        ("source_fingerprint", "archive backup local changed"),
        ("eligibility_rebinding", "archive backup local changed"),
        ("eligibility_exact_bytes", "archive backup local changed"),
        ("coverage_binding", "archive backup local changed"),
        ("collision", "archive backup local changed"),
        ("local_root", "archive backup local changed"),
        ("corpus_count", "archive backup local changed"),
        ("source_artifact", "archive backup local changed"),
        ("source_artifact_noncanonical", "archive backup local changed"),
        ("coverage_artifact", "archive backup local changed"),
    ],
)
def test_runtime_source_eligibility_and_local_mutation_fail_fixed(
    tmp_path: Path, fault: str, category: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=rf"^{category}$"):
        _run(fixture)

    assert fixture.attempts["receipt-write"] == 0
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    "fault",
    ["remote_missing", "remote_extra", "remote_disallowed", "remote_duplicate"],
)
def test_remote_generation_requires_exact_rows_counts_and_zero_safety_counters(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=r"^archive backup verify failed$"):
        _run(fixture)

    assert fixture.attempts["remote-postflight"] == 1
    assert fixture.attempts["receipt-write"] == 0
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    "fault",
    [
        "receipt_write",
        "receipt_collision",
        "receipt_fork",
        "parent_swap",
        "chain_race",
        "wrong_scope_predecessor",
    ],
)
def test_receipt_fork_collision_swap_and_write_fail_before_publication(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError, match=r"^archive backup receipt failed$"):
        _run(fixture)

    assert fixture.written_receipts == []
    assert fixture.attempts["receipt-write"] <= 1


@pytest.mark.parametrize(
    "fault",
    [
        "workspace-receipt",
        "workspace-runs",
        "workspace-generation",
        "workspace-before",
        "workspace-storage",
        "workspace-remote-empty",
        "workspace-copy",
        "workspace-verify",
        "workspace-after",
        "workspace-remote-after",
        "workspace-predecessor",
        "workspace-writer",
    ],
)
def test_lock_and_every_workspace_level_remain_one_descriptor_bound_transaction(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = _fixture(tmp_path, fault=fault)

    with pytest.raises(BackupRunError):
        _run(fixture)

    assert list(tmp_path.rglob("backup-*.json")) == []
    assert fixture.written_receipts == []


@pytest.mark.parametrize(
    "private_value",
    [_PRIVATE_URL, _PRIVATE_BINDING, _PRIVATE_MESSAGE, _PRIVATE_TRANSCRIPT],
)
def test_errors_repr_summary_stdout_and_logs_never_disclose_private_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    private_value: str,
) -> None:
    fixture = _fixture(tmp_path, fault="storage_json_failure")

    with pytest.raises(BackupRunError) as captured:
        _run(fixture)

    public = str(captured.value) + repr(captured.value)
    streams = capsys.readouterr()
    assert str(captured.value) in _ERRORS
    assert private_value not in public
    assert private_value not in streams.out
    assert private_value not in streams.err
    assert private_value not in caplog.text


def test_summary_is_aggregate_only_and_rejects_nonreceipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _run(fixture)

    summary = backup_run_summary(receipt)
    encoded = json.dumps(summary, sort_keys=True)

    assert summary["result"] == "verified"
    assert summary["sessions"] == 2
    for private_value in (
        _PRIVATE_URL,
        str(_GENERATION_ID),
        _SCOPE_ID,
        str(fixture.config.receipt_directory),
        receipt.source_inventory_sha256,
    ):
        assert private_value not in encoded
    with pytest.raises(BackupRunError, match=r"^archive backup receipt failed$"):
        backup_run_summary(object())


def test_dependency_type_is_exact_and_failure_directory_is_preserved(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, fault="copy_exit")
    assert fixture.dependencies is not None
    with pytest.raises(BackupRunError, match=r"^archive backup preflight failed$"):
        _run_backup(
            fixture.config,
            fixture.drover_config,
            dependencies=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(BackupRunError, match=r"^archive backup copy failed$"):
        _run(fixture)
    run_directory = (
        fixture.config.receipt_directory / ".backup-runs" / str(_GENERATION_ID)
    )
    assert run_directory.is_dir()
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert list(run_directory.iterdir())
