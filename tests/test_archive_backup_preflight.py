"""Strictly local backup preflight orchestration and public summary."""

from __future__ import annotations

import importlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

import drover.server.archive as archive_package
from drover.config import default_config
from drover.server.archive.backup_config import BackupConfig
from drover.server.archive.backup_preflight import (
    BackupPreflightResult,
    backup_preflight_summary,
    run_backup_preflight,
)
from drover.server.archive.coverage import (
    RegistryCandidate,
    build_coverage_report,
    coverage_summary,
    load_registry_candidates,
)
from drover.server.archive.inventory import (
    PondInventory,
    PondInventoryRecord,
    SourceEligibilityReceipt,
    write_private_json,
)
from drover.server.archive.native_inventory import discover_native_history_inventory
from drover.server.archive.pond_process import PondProcessResult
from drover.server.archive.pond_snapshot import PondCorpusCounts, PondStoreSnapshot
from drover.server.harness import daemon as harness_daemon

_ERROR = "archive backup preflight failed"
_HOST_ID = "host-private"
_CAPTURED_AT = "2026-08-29T12:00:00Z"
_PRIVATE_ADAPTER_PATH = "/private/native/history"
_CLEAN_DRY_RUN = {
    "dry_run": True,
    "adapters": [
        {
            "name": "claude-code",
            "path": _PRIVATE_ADAPTER_PATH + "/claude",
            "sessions": 1,
            "fresh": 1,
            "pending": 0,
        },
        {
            "name": "codex-cli",
            "path": _PRIVATE_ADAPTER_PATH + "/codex",
            "sessions": 1,
            "fresh": 1,
            "pending": 0,
        },
    ],
}


class _Runtime:
    def __init__(self, host_id: str = _HOST_ID) -> None:
        self._host_id = host_id
        self.calls = 0
        self.samples = 0

    def baseline_host_id(self) -> str:
        self.calls += 1
        return self._host_id

    def sample(self) -> None:
        self.samples += 1

    def __repr__(self) -> str:
        return "_Runtime(private)"


def _write_native_sources(home: Path, *, include_metadata_only: bool = False) -> None:
    claude = home / ".claude/projects/project-private/claude-private.jsonl"
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(
        '{"type":"user","message":"PRIVATE TRANSCRIPT BODY"}\n',
        encoding="utf-8",
    )
    codex = (
        home
        / ".codex/sessions/2026/08/29"
        / "rollout-2026-08-29T12-00-00-019ef2b6-7000-79c3-93c6-039d129b9513.jsonl"
    )
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(
        '{"type":"response_item","payload":"PRIVATE TRANSCRIPT BODY"}\n',
        encoding="utf-8",
    )
    if include_metadata_only:
        metadata_only = home / ".claude/projects/project-private/metadata-private.jsonl"
        metadata_only.write_text(
            '{"type":"ai-title","sessionId":"metadata-private","title":"private"}\n',
            encoding="utf-8",
        )


def _root_inventory() -> PondInventory:
    return PondInventory(
        schema_version=1,
        captured_at=_CAPTURED_AT,
        pond_version="0.16.3",
        records=(
            PondInventoryRecord(
                session_id="claude-private",
                source_agent="claude-code",
                created_at="2026-08-29T10:00:00Z",
                message_count=1,
                first_message_at="2026-08-29T10:01:00Z",
                last_message_at="2026-08-29T10:01:00Z",
            ),
            PondInventoryRecord(
                session_id="019ef2b6-7000-79c3-93c6-039d129b9513",
                source_agent="codex-cli",
                created_at="2026-08-29T11:00:00Z",
                message_count=1,
                first_message_at="2026-08-29T11:01:00Z",
                last_message_at="2026-08-29T11:01:00Z",
            ),
        ),
    )


def _snapshot(*, counts: PondCorpusCounts | None = None) -> PondStoreSnapshot:
    return PondStoreSnapshot(
        root_inventory=_root_inventory(),
        counts=counts
        or PondCorpusCounts(
            sessions=2,
            messages=2,
            parts=2,
            disallowed_sessions=0,
            logical_duplicate_groups=0,
            sessions_in_logical_duplicate_groups=0,
        ),
    )


def _registry(path: Path, *, include_metadata_only: bool = False) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("""
            CREATE TABLE harness_sessions (
                session_id VARCHAR,
                host_id VARCHAR,
                harness VARCHAR,
                native_session_id VARCHAR,
                transcript_preview VARCHAR
            )
            """)
        rows = [
            (
                "wrapper-claude-private",
                _HOST_ID,
                "claude-code",
                "claude-private",
                "PRIVATE REGISTRY TRANSCRIPT",
            ),
            (
                "wrapper-codex-private",
                _HOST_ID,
                "codex",
                "019ef2b6-7000-79c3-93c6-039d129b9513",
                "PRIVATE REGISTRY TRANSCRIPT",
            ),
        ]
        if include_metadata_only:
            rows.append(
                (
                    "wrapper-metadata-private",
                    _HOST_ID,
                    "claude-code",
                    "metadata-private",
                    "PRIVATE REGISTRY TRANSCRIPT",
                )
            )
        connection.executemany(
            "INSERT INTO harness_sessions VALUES (?, ?, ?, ?, ?)", rows
        )


def _private_file(path: Path, content: str = "private") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _backup_config(tmp_path: Path) -> BackupConfig:
    binary = _private_file(tmp_path / "pond-private", "#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    local_config = _private_file(tmp_path / "local-config-private.toml")
    remote_config = _private_file(tmp_path / "remote-config-private.toml")
    local_store = tmp_path / "pond-store-private"
    local_store.mkdir()
    receipts = tmp_path / "receipts-private"
    receipts.mkdir(mode=0o700)
    return BackupConfig(
        schema_version=1,
        pond_binary=binary,
        local_pond_config=local_config,
        local_store=local_store,
        remote_pond_config=remote_config,
        backup_root_url=(
            "s3+https://account-private.r2.cloudflarestorage.com/"
            "bucket-private/prefix-private"
        ),
        store_scope_id="536b300b-24ff-4dda-a3e9-52fde1154b59",
        receipt_directory=receipts,
        copy_timeout_seconds=60,
        max_rss_bytes=1024,
        max_physical_bytes=2048,
        max_swap_growth_bytes=0,
    )


def _dependencies(
    tmp_path: Path,
    config: BackupConfig,
    *,
    dry_run: object = _CLEAN_DRY_RUN,
    snapshot: PondStoreSnapshot | None = None,
    include_metadata_only: bool = False,
    summary_override=None,
):
    home = tmp_path / "home-private"
    home.mkdir()
    _write_native_sources(home, include_metadata_only=include_metadata_only)
    registry = tmp_path / "live-registry-private.duckdb"
    _registry(registry, include_metadata_only=include_metadata_only)
    calls: list[tuple[object, ...]] = []
    loaded_snapshots: list[Path] = []

    def capture(binary, *, storage, pond_config, workspace, timeout_seconds):
        calls.append(
            ("snapshot", binary, storage, pond_config, workspace, timeout_seconds)
        )
        value = snapshot or _snapshot()
        write_private_json(
            workspace / "pond-inventory.json", value.root_inventory.to_wire()
        )
        return value

    def load_registry(path):
        copied = Path(path)
        assert copied != registry
        assert copied.exists()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        loaded_snapshots.append(copied)
        return load_registry_candidates(copied)

    def run_process(
        binary,
        arguments,
        *,
        timeout_seconds,
        run_directory,
        label,
        **kwargs,
    ):
        calls.append(
            (
                "process",
                binary,
                tuple(arguments),
                timeout_seconds,
                run_directory,
                label,
                kwargs,
            )
        )
        stdout = Path(run_directory) / f"{label}.stdout"
        stderr = Path(run_directory) / f"{label}.stderr"
        stdout.write_text(json.dumps(dry_run) + "\n", encoding="utf-8")
        stderr.write_bytes(b"")
        stdout.chmod(0o600)
        stderr.chmod(0o600)
        return PondProcessResult(0, 1, 0, None, 0, stdout, stderr)

    return SimpleNamespace(
        native_home=lambda: home,
        discover_native_inventory=discover_native_history_inventory,
        capture_pond_snapshot=capture,
        resolve_control_plane_path=lambda _path: registry,
        load_registry_candidates=load_registry,
        build_coverage_report=build_coverage_report,
        coverage_summary=summary_override or coverage_summary,
        run_pond_process=run_process,
        calls=calls,
        loaded_snapshots=loaded_snapshots,
        home=home,
        registry=registry,
    )


def _run(
    tmp_path: Path,
    *,
    dry_run: object = _CLEAN_DRY_RUN,
    snapshot: PondStoreSnapshot | None = None,
    include_metadata_only: bool = False,
    summary_override=None,
):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        config,
        dry_run=dry_run,
        snapshot=snapshot,
        include_metadata_only=include_metadata_only,
        summary_override=summary_override,
    )
    runtime = _Runtime()
    workspace = tmp_path / "workspace-private"
    drover_config = replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb")
    result = run_backup_preflight(
        config,
        drover_config,
        workspace,
        runtime,
        dependencies=dependencies,
    )
    return result, config, dependencies, runtime, workspace


def test_archive_package_exports_only_the_completed_preflight_interfaces() -> None:
    expected = {
        "BackupPreflightResult",
        "LocalPondStore",
        "PondCorpusCounts",
        "PondStoreSnapshot",
        "RemotePondGeneration",
        "backup_preflight_summary",
        "capture_pond_store_snapshot",
        "pond_inventory_content_sha256",
        "run_backup_preflight",
    }

    assert expected <= set(archive_package.__all__)
    assert all(hasattr(archive_package, name) for name in expected)


def test_preflight_is_local_only_and_requires_a_clean_denominator(tmp_path):
    result, config, dependencies, runtime, workspace = _run(tmp_path)

    assert result.coverage.ready_for_next_writer is True
    assert result.pond_snapshot.counts.disallowed_sessions == 0
    assert backup_preflight_summary(result)["ready"] is True
    assert runtime.calls == 1
    process_calls = [call for call in dependencies.calls if call[0] == "process"]
    assert process_calls == [
        (
            "process",
            config.pond_binary,
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
            60.0,
            workspace,
            "local-dry-run",
            {"progress_callback": runtime.sample},
        )
    ]
    rendered_calls = repr(dependencies.calls)
    assert config.backup_root_url not in rendered_calls
    assert str(config.remote_pond_config) not in rendered_calls
    assert list(config.receipt_directory.glob("backup-*.json")) == []


def test_preflight_rejects_coverage_eligibility_count_mismatch(tmp_path):
    def faulty_summary(report):
        summary = coverage_summary(report)
        summary["current_source_coverage"]["source_not_archive_eligible"] = 1
        summary["ready_for_next_writer"] = True
        return summary

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        _run(tmp_path, summary_override=faulty_summary)


def test_preflight_rejects_nonaggregate_nested_coverage_summary(tmp_path):
    def faulty_summary(report):
        summary = coverage_summary(report)
        summary["candidate_coverage"]["private_session_id"] = "private-id"
        return summary

    with pytest.raises(ValueError, match=rf"^{_ERROR}$") as raised:
        _run(tmp_path, summary_override=faulty_summary)

    assert "private-id" not in str(raised.value)


def test_preflight_discovers_metadata_without_parsing_native_transcript_bodies(
    tmp_path, monkeypatch
):
    parsed: list[object] = []
    original_loads = harness_daemon.json.loads

    def reject_body_parse(value, *args, **kwargs):
        parsed.append(value)
        rendered = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        if "PRIVATE TRANSCRIPT BODY" in rendered:
            raise AssertionError("native transcript body was parsed")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(harness_daemon.json, "loads", reject_body_parse)

    result, _, dependencies, _, _ = _run(tmp_path)

    assert len(result.source_inventory.records) == 2
    assert all("PRIVATE TRANSCRIPT BODY" not in str(value) for value in parsed)
    assert "PRIVATE TRANSCRIPT BODY" not in repr(result)
    assert str(dependencies.home) not in repr(result)


def test_preflight_loads_exact_bound_eligibility_from_fixed_directory(tmp_path):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        config,
        include_metadata_only=True,
    )
    inventory = discover_native_history_inventory(dependencies.home, _HOST_ID)
    metadata = next(
        row for row in inventory.records if row.session_id == "metadata-private"
    )
    eligibility = config.receipt_directory / "eligibility"
    eligibility.mkdir(mode=0o700)
    receipt = SourceEligibilityReceipt(
        schema_version=1,
        assessed_at=_CAPTURED_AT,
        host_id=_HOST_ID,
        source_agent="claude-code",
        session_id="metadata-private",
        source_fingerprint=metadata.source_fingerprint or "",
        classification="source_not_archive_eligible",
    )
    write_private_json(eligibility / "metadata-private.json", receipt.to_wire())
    runtime = _Runtime()
    workspace = tmp_path / "workspace-private"

    result = run_backup_preflight(
        config,
        replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
        workspace,
        runtime,
        dependencies=dependencies,
    )

    assert result.source_not_archive_eligible == 1
    assert result.coverage_summary["current_source_coverage"] == {
        "discovered": 3,
        "matched": 2,
        "source_not_archive_eligible": 1,
        "discovered_not_synced": 0,
    }


def test_preflight_rejects_stale_eligibility_binding_with_fixed_error(tmp_path):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        config,
        include_metadata_only=True,
    )
    eligibility = config.receipt_directory / "eligibility"
    eligibility.mkdir(mode=0o700)
    receipt = SourceEligibilityReceipt(
        schema_version=1,
        assessed_at=_CAPTURED_AT,
        host_id=_HOST_ID,
        source_agent="claude-code",
        session_id="metadata-private",
        source_fingerprint="0" * 64,
        classification="source_not_archive_eligible",
    )
    write_private_json(eligibility / "metadata-private.json", receipt.to_wire())

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            tmp_path / "workspace-private",
            _Runtime(),
            dependencies=dependencies,
        )


@pytest.mark.parametrize(
    "adapters",
    [
        _CLEAN_DRY_RUN["adapters"][:1],
        [*_CLEAN_DRY_RUN["adapters"], _CLEAN_DRY_RUN["adapters"][0]],
        [
            *_CLEAN_DRY_RUN["adapters"],
            {
                "name": "unknown-private",
                "path": "/private/unknown",
                "sessions": 0,
                "fresh": 0,
                "pending": 0,
            },
        ],
    ],
)
def test_preflight_requires_exact_enabled_adapter_denominator(adapters, tmp_path):
    dry_run = {"dry_run": True, "adapters": adapters}

    with pytest.raises(ValueError, match=rf"^{_ERROR}$") as raised:
        _run(tmp_path, dry_run=dry_run)

    assert "unknown-private" not in str(raised.value)
    assert "/private" not in str(raised.value)


@pytest.mark.parametrize("pending", [None, True, -1, 1, "0"])
def test_preflight_requires_exact_zero_pending_for_every_adapter(pending, tmp_path):
    adapters = [dict(row) for row in _CLEAN_DRY_RUN["adapters"]]
    adapters[1]["pending"] = pending

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        _run(tmp_path, dry_run={"dry_run": True, "adapters": adapters})


@pytest.mark.parametrize(
    "dry_run",
    [
        {"dry_run": False, "adapters": _CLEAN_DRY_RUN["adapters"]},
        {"dry_run": True, "adapters": [], "extra": 1},
        {"dry_run": True},
        [],
    ],
)
def test_preflight_requires_exact_dry_run_json_shape(dry_run, tmp_path):
    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        _run(tmp_path, dry_run=dry_run)


@pytest.mark.parametrize(
    "fault_path",
    [
        ("misses", "discovered_not_synced"),
        ("misses", "source_absent_after_prior_inventory"),
        ("misses", "unverifiable"),
        ("current_source_coverage", "discovered_not_synced"),
        ("collisions", "duplicate_source_groups"),
        ("collisions", "cross_harness_native_id_groups"),
        ("collisions", "archive_logical_duplicate_candidate_groups"),
        ("collisions", "archive_signature_unverifiable"),
        ("unsupported_harness_sessions",),
        ("ready_for_next_writer",),
    ],
)
def test_preflight_checks_every_coverage_counter_not_only_ready_boolean(
    fault_path, tmp_path
):
    def faulty_summary(report):
        summary = coverage_summary(report)
        target = summary
        for key in fault_path[:-1]:
            target = target[key]
        target[fault_path[-1]] = (
            False if fault_path[-1] == "ready_for_next_writer" else 1
        )
        if fault_path[-1] != "ready_for_next_writer":
            summary["ready_for_next_writer"] = True
        return summary

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        _run(tmp_path, summary_override=faulty_summary)


@pytest.mark.parametrize(
    "counts",
    [
        replace(_snapshot().counts, disallowed_sessions=1),
        replace(
            _snapshot().counts,
            logical_duplicate_groups=1,
            sessions_in_logical_duplicate_groups=2,
        ),
    ],
)
def test_preflight_requires_every_full_store_safety_counter_zero(counts, tmp_path):
    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        _run(tmp_path, snapshot=_snapshot(counts=counts))


def test_preflight_registry_snapshot_exists_only_during_candidate_load(tmp_path):
    result, _, dependencies, _, workspace = _run(tmp_path)

    assert result.coverage.ready_for_next_writer is True
    assert len(dependencies.loaded_snapshots) == 1
    snapshot_path = dependencies.loaded_snapshots[0]
    assert snapshot_path.parent == workspace
    assert not snapshot_path.exists()


def test_preflight_discards_partial_registry_snapshot_when_copy_fails(
    tmp_path, monkeypatch
):
    preflight_module = importlib.import_module("drover.server.archive.backup_preflight")

    def fail_write(_descriptor, _data):
        raise OSError("private copy failure")

    monkeypatch.setattr(preflight_module.os, "write", fail_write)

    with pytest.raises(ValueError, match=rf"^{_ERROR}$") as raised:
        _run(tmp_path)

    assert "private copy failure" not in str(raised.value)
    assert not (tmp_path / "workspace-private/registry-snapshot.duckdb").exists()


def test_preflight_requires_capture_to_publish_the_exact_private_pond_artifact(
    tmp_path,
):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(tmp_path, config)

    def capture_without_artifact(*_args, **_kwargs):
        return _snapshot()

    dependencies.capture_pond_snapshot = capture_without_artifact

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            tmp_path / "workspace-private",
            _Runtime(),
            dependencies=dependencies,
        )


def test_preflight_rejects_dry_run_result_paths_outside_the_workspace(tmp_path):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(tmp_path, config)
    original_run = dependencies.run_pond_process
    outside = tmp_path / "outside-private"
    outside.mkdir(mode=0o700)

    def wrong_result_path(*args, **kwargs):
        result = original_run(*args, **kwargs)
        external = outside / result.stdout_path.name
        external.write_bytes(result.stdout_path.read_bytes())
        external.chmod(0o600)
        return replace(result, stdout_path=external)

    dependencies.run_pond_process = wrong_result_path

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            tmp_path / "workspace-private",
            _Runtime(),
            dependencies=dependencies,
        )


def test_preflight_writes_only_private_artifacts_and_returns_aggregate_summary(
    tmp_path,
):
    result, config, _, _, workspace = _run(tmp_path)

    assert isinstance(result, BackupPreflightResult)
    assert result.source_inventory_path == workspace / "source-inventory.json"
    assert result.pond_inventory_path == workspace / "pond-inventory.json"
    assert result.coverage_report_path == workspace / "coverage-report.json"
    for path in workspace.iterdir():
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    summary = backup_preflight_summary(result)
    encoded = json.dumps(summary, sort_keys=True)
    assert summary["schema_version"] == 1
    assert summary["ready"] is True
    assert summary["pond_corpus"] == {
        "sessions": 2,
        "messages": 2,
        "parts": 2,
        "disallowed_sessions": 0,
        "logical_duplicate_groups": 0,
        "sessions_in_logical_duplicate_groups": 0,
    }
    for private in (
        _HOST_ID,
        "claude-private",
        "019ef2b6",
        str(workspace),
        config.backup_root_url,
        _PRIVATE_ADAPTER_PATH,
    ):
        assert private not in encoded
        assert private not in repr(result)


def test_preflight_rejects_non_private_or_symlinked_workspace(tmp_path):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(tmp_path, config)
    target = tmp_path / "target-private"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "workspace-private"
    symlink.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            symlink,
            _Runtime(),
            dependencies=dependencies,
        )


def test_preflight_rejects_more_than_one_hundred_thousand_eligibility_entries(
    tmp_path, monkeypatch
):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(tmp_path, config)
    eligibility = config.receipt_directory / "eligibility"
    eligibility.mkdir(mode=0o700)
    real_listdir = os.listdir

    def oversized_listdir(path):
        if isinstance(path, int):
            return [f"receipt-{index}.json" for index in range(100_001)]
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", oversized_listdir)

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            tmp_path / "workspace-private",
            _Runtime(),
            dependencies=dependencies,
        )


def test_preflight_rejects_eligibility_directory_changes_during_load(
    tmp_path, monkeypatch
):
    config = _backup_config(tmp_path)
    dependencies = _dependencies(tmp_path, config)
    eligibility = config.receipt_directory / "eligibility"
    eligibility.mkdir(mode=0o700)
    real_listdir = os.listdir

    def racing_listdir(path):
        names = real_listdir(path)
        if isinstance(path, int):
            metadata = eligibility.stat()
            os.utime(
                eligibility,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
            )
        return names

    monkeypatch.setattr(os, "listdir", racing_listdir)

    with pytest.raises(ValueError, match=rf"^{_ERROR}$"):
        run_backup_preflight(
            config,
            replace(default_config(), duckdb_path=tmp_path / "ignored.duckdb"),
            tmp_path / "workspace-private",
            _Runtime(),
            dependencies=dependencies,
        )
