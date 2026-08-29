"""Privacy-safe Click wiring for local archive inventory coverage."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from drover.server import __main__ as server_main
from drover.server.__main__ import main
from drover.server.archive.coverage import RegistryCandidate
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
    write_private_json,
)
from drover.server.archive.native_inventory import native_inventory_summary
from drover.server.archive.pond_inventory import pond_inventory_summary

_CAPTURED_AT = "2026-08-29T12:00:00Z"
_UPDATED_AT = "2026-08-29T11:00:00Z"
_CREATED_AT = "2026-08-29T10:00:00Z"
_FIRST_MESSAGE_AT = "2026-08-29T10:01:00Z"
_LAST_MESSAGE_AT = "2026-08-29T10:02:00Z"


def _source_inventory(
    host_id: str = "host-private",
    session_id: str = "native-private",
    *,
    source_agent: str = "claude-code",
) -> NativeInventory:
    return NativeInventory(
        schema_version=1,
        captured_at=_CAPTURED_AT,
        host_id=host_id,
        records=(
            NativeInventoryRecord(
                source_agent=source_agent,
                session_id=session_id,
                updated_at=_UPDATED_AT,
                size_bytes=123,
                source_copies=1,
            ),
        ),
    )


def _pond_inventory(
    session_id: str = "native-private",
    *,
    source_agent: str = "claude-code",
) -> PondInventory:
    return PondInventory(
        schema_version=1,
        captured_at=_CAPTURED_AT,
        pond_version="0.16.3",
        records=(
            PondInventoryRecord(
                session_id=session_id,
                source_agent=source_agent,
                created_at=_CREATED_AT,
                message_count=2,
                first_message_at=_FIRST_MESSAGE_AT,
                last_message_at=_LAST_MESSAGE_AT,
            ),
        ),
    )


def _write_native(path: Path, inventory: NativeInventory | None = None) -> None:
    write_private_json(path, (inventory or _source_inventory()).to_wire())


def _write_pond(path: Path, inventory: PondInventory | None = None) -> None:
    write_private_json(path, (inventory or _pond_inventory()).to_wire())


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _assert_private_values_absent(result, caplog, *values: object) -> None:
    rendered = "\n".join(
        (
            result.output,
            str(result.exception or ""),
            caplog.text,
        )
    )
    for value in values:
        assert str(value) not in rendered


def _coverage_args(
    *,
    output: Path,
    source: Path,
    pond: Path,
    db: Path | None = None,
    prior: tuple[Path, ...] = (),
) -> list[str]:
    args = [
        "archive",
        "coverage",
        "--output",
        str(output),
        "--source-inventory",
        str(source),
        "--pond-inventory",
        str(pond),
    ]
    if db is not None:
        args.extend(("--db", str(db)))
    for path in prior:
        args.extend(("--prior-source-inventory", str(path)))
    return args


def test_archive_help_exposes_only_the_three_local_operator_commands():
    runner = CliRunner()

    result = runner.invoke(main, ["archive", "--help"])

    assert result.exit_code == 0, result.output
    assert {"source-inventory", "pond-inventory", "coverage"} <= set(
        result.output.split()
    )

    source_help = runner.invoke(main, ["archive", "source-inventory", "--help"])
    assert source_help.exit_code == 0, source_help.output
    assert "--host-id HOST" in source_help.output
    assert "--output FILE" in source_help.output

    pond_help = runner.invoke(main, ["archive", "pond-inventory", "--help"])
    assert pond_help.exit_code == 0, pond_help.output
    assert "--storage-path DIRECTORY" in pond_help.output
    assert "--output FILE" in pond_help.output
    assert "--pond-binary FILE" in pond_help.output
    assert "--timeout SECONDS" in pond_help.output

    coverage_help = runner.invoke(main, ["archive", "coverage", "--help"])
    assert coverage_help.exit_code == 0, coverage_help.output
    assert "--output FILE" in coverage_help.output
    assert "--db FILE" in coverage_help.output
    assert "--source-inventory FILE" in coverage_help.output
    assert "--pond-inventory FILE" in coverage_help.output
    assert "--prior-source-inventory FILE" in coverage_help.output
    assert "--apply" not in coverage_help.output


def test_source_inventory_writes_private_manifest_and_prints_only_sorted_summary(
    monkeypatch, tmp_path, caplog
):
    inventory = _source_inventory()
    output = tmp_path / "private-source-manifest.json"
    monkeypatch.setattr(
        server_main,
        "discover_native_history_inventory",
        lambda _home, _host_id: inventory,
    )

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "source-inventory",
            "--host-id",
            inventory.host_id,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        result.output
        == json.dumps(native_inventory_summary(inventory), sort_keys=True) + "\n"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == inventory.to_wire()
    assert _mode(output) == 0o600
    _assert_private_values_absent(
        result,
        caplog,
        inventory.host_id,
        inventory.records[0].session_id,
        output,
    )


@pytest.mark.parametrize("failure_kind", ["capture", "existing-output"])
def test_source_inventory_failures_are_sanitized_and_do_not_replace_output(
    failure_kind, monkeypatch, tmp_path, caplog
):
    output = tmp_path / "sensitive-source-output.json"
    original = b"existing-private-artifact\n"
    if failure_kind == "existing-output":
        output.write_bytes(original)
        output.chmod(0o600)
        monkeypatch.setattr(
            server_main,
            "discover_native_history_inventory",
            lambda _home, _host_id: _source_inventory(),
        )
    else:

        def fail_capture(_home, _host_id):
            raise ValueError("capture leaked-native-id")

        monkeypatch.setattr(
            server_main, "discover_native_history_inventory", fail_capture
        )

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "source-inventory",
            "--host-id",
            "leaked-host-id",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code != 0
    assert "archive source inventory failed" in result.output
    if failure_kind == "capture":
        assert not output.exists()
    else:
        assert output.read_bytes() == original
    _assert_private_values_absent(
        result, caplog, "leaked-native-id", "leaked-host-id", output
    )


@pytest.mark.parametrize("resolution", ["explicit", "environment", "path"])
def test_pond_inventory_resolves_binary_in_documented_order_and_writes_once(
    resolution, monkeypatch, tmp_path, caplog
):
    explicit = tmp_path / "explicit-private-pond"
    environment = tmp_path / "environment-private-pond"
    discovered = tmp_path / "discovered-private-pond"
    expected = {
        "explicit": explicit,
        "environment": environment,
        "path": discovered,
    }[resolution]
    if resolution in {"explicit", "environment"}:
        monkeypatch.setenv("POND_BINARY", str(environment))
    else:
        monkeypatch.delenv("POND_BINARY", raising=False)
    monkeypatch.setattr(server_main.shutil, "which", lambda _name: str(discovered))
    inventory = _pond_inventory()
    output = tmp_path / "private-pond-manifest.json"
    storage = tmp_path / "private-pond-store"
    storage.mkdir()

    def fake_export(binary, final_output, *, storage_path, timeout_seconds):
        assert binary == expected
        assert storage_path == storage
        assert timeout_seconds == 17.0
        write_private_json(final_output, inventory.to_wire())
        return inventory

    monkeypatch.setattr(server_main, "export_pond_inventory", fake_export)
    args = [
        "archive",
        "pond-inventory",
        "--storage-path",
        str(storage),
        "--output",
        str(output),
        "--timeout",
        "17",
    ]
    if resolution == "explicit":
        args.extend(("--pond-binary", str(explicit)))

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert (
        result.output
        == json.dumps(pond_inventory_summary(inventory), sort_keys=True) + "\n"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == inventory.to_wire()
    assert _mode(output) == 0o600
    _assert_private_values_absent(
        result,
        caplog,
        explicit,
        environment,
        discovered,
        storage,
        output,
        inventory.records[0].session_id,
    )


def test_pond_inventory_refuses_absent_binary_without_creating_output(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.delenv("POND_BINARY", raising=False)
    monkeypatch.setattr(server_main.shutil, "which", lambda _name: None)
    storage = tmp_path / "private-store"
    storage.mkdir()
    output = tmp_path / "private-output.json"

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "pond-inventory",
            "--storage-path",
            str(storage),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code != 0
    assert "archive pond inventory binary unavailable" in result.output
    assert not output.exists()
    _assert_private_values_absent(result, caplog, storage, output)


@pytest.mark.parametrize("storage_kind", ["relative", "url", "file", "missing"])
def test_pond_inventory_refuses_unsafe_or_nonlocal_storage_without_running_pond(
    storage_kind, monkeypatch, tmp_path, caplog
):
    binary = tmp_path / "private-pond-binary"
    binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    binary.chmod(0o700)
    existing_file = tmp_path / "not-a-directory"
    existing_file.write_text("private", encoding="utf-8")
    storage = {
        "relative": Path("relative-private-store"),
        "url": Path("s3://private-bucket"),
        "file": existing_file,
        "missing": tmp_path / "missing-private-store",
    }[storage_kind]
    output = tmp_path / "private-output.json"

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "pond-inventory",
            "--storage-path",
            str(storage),
            "--output",
            str(output),
            "--pond-binary",
            str(binary),
        ],
    )

    assert result.exit_code != 0
    assert "archive pond inventory failed" in result.output
    assert not output.exists()
    _assert_private_values_absent(result, caplog, binary, storage, output)


@pytest.mark.parametrize("failure_kind", ["export", "existing-output"])
def test_pond_inventory_export_failures_are_sanitized_and_do_not_replace_output(
    failure_kind, monkeypatch, tmp_path, caplog
):
    binary = tmp_path / "private-pond-binary"
    storage = tmp_path / "private-pond-store"
    storage.mkdir()
    output = tmp_path / "private-pond-output.json"
    original = b"existing-private-artifact\n"
    if failure_kind == "existing-output":
        output.write_bytes(original)
        output.chmod(0o600)

    def fail_export(_binary, final_output, **_kwargs):
        if failure_kind == "existing-output":
            write_private_json(final_output, _pond_inventory().to_wire())
        raise ValueError("export leaked-pond-session")

    monkeypatch.setattr(server_main, "export_pond_inventory", fail_export)

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "pond-inventory",
            "--storage-path",
            str(storage),
            "--output",
            str(output),
            "--pond-binary",
            str(binary),
        ],
    )

    assert result.exit_code != 0
    assert "archive pond inventory failed" in result.output
    if failure_kind == "export":
        assert not output.exists()
    else:
        assert output.read_bytes() == original
    _assert_private_values_absent(
        result, caplog, binary, storage, output, "leaked-pond-session"
    )


@pytest.mark.parametrize(
    ("pond_session_id", "expected_exit", "ready"),
    [("native-private", 0, True), ("different-private", 2, False)],
)
def test_coverage_snapshots_resolved_registry_writes_report_before_safe_exit(
    pond_session_id,
    expected_exit,
    ready,
    monkeypatch,
    tmp_path,
    caplog,
):
    current_path = tmp_path / "current-private.json"
    pond_path = tmp_path / "pond-private.json"
    output = tmp_path / "coverage-private.json"
    requested_db = tmp_path / "requested-private.duckdb"
    resolved_registry = tmp_path / "resolved-private.registry.duckdb"
    snapshot = tmp_path / "snapshot-private.registry.duckdb"
    _write_native(current_path)
    _write_pond(pond_path, _pond_inventory(pond_session_id))
    calls: list[tuple[str, Path]] = []

    def resolve_registry(path):
        calls.append(("resolve", Path(path)))
        return resolved_registry

    @contextmanager
    def snapshot_registry(path):
        calls.append(("snapshot", Path(path)))
        yield snapshot

    def load_snapshot(path):
        calls.append(("load", Path(path)))
        return (
            RegistryCandidate(
                "drover-private",
                "host-private",
                "claude-code",
                "native-private",
            ),
        )

    monkeypatch.setattr(server_main, "control_plane_path", resolve_registry)
    monkeypatch.setattr(server_main, "_diagnostic_db_path", snapshot_registry)
    monkeypatch.setattr(server_main, "load_registry_candidates", load_snapshot)

    result = CliRunner().invoke(
        main,
        _coverage_args(
            output=output,
            source=current_path,
            pond=pond_path,
            db=requested_db,
        ),
    )

    assert result.exit_code == expected_exit, result.output
    summary = json.loads(result.output)
    assert summary["ready_for_next_writer"] is ready
    assert result.output == json.dumps(summary, sort_keys=True) + "\n"
    assert calls == [
        ("resolve", requested_db),
        ("snapshot", resolved_registry),
        ("load", snapshot),
    ]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["kind"] == "archive_coverage_report"
    assert report["ready_for_next_writer"] is ready
    assert report["certified_coverage"] == {
        "certified": 0,
        "status": "not_implemented",
    }
    assert _mode(output) == 0o600
    _assert_private_values_absent(
        result,
        caplog,
        current_path,
        pond_path,
        output,
        requested_db,
        resolved_registry,
        snapshot,
        "drover-private",
        "host-private",
        "native-private",
        pond_session_id,
    )


@pytest.mark.parametrize(
    "failure_kind",
    [
        "missing-source",
        "unsafe-source",
        "wrong-source-kind",
        "duplicate-current-host",
        "malformed-registry",
        "existing-output",
    ],
)
def test_coverage_input_registry_and_output_failures_write_no_new_report(
    failure_kind, monkeypatch, tmp_path, caplog
):
    current_path = tmp_path / "current-private.json"
    second_current = tmp_path / "second-current-private.json"
    pond_path = tmp_path / "pond-private.json"
    output = tmp_path / "coverage-private.json"
    db = tmp_path / "malformed-private.registry.duckdb"
    _write_native(current_path)
    _write_pond(pond_path)
    db.write_bytes(b"not a DuckDB catalog")
    db.chmod(0o600)
    original: bytes | None = None
    if failure_kind != "malformed-registry":
        monkeypatch.setattr(server_main, "load_registry_candidates", lambda _path: ())

    if failure_kind == "missing-source":
        current_path.unlink()
    elif failure_kind == "unsafe-source":
        current_path.chmod(0o644)
    elif failure_kind == "wrong-source-kind":
        current_path.unlink()
        _write_pond(current_path)
    elif failure_kind == "duplicate-current-host":
        _write_native(second_current)
    elif failure_kind == "existing-output":
        original = b"existing-private-report\n"
        output.write_bytes(original)
        output.chmod(0o600)

    args = _coverage_args(
        output=output,
        source=current_path,
        pond=pond_path,
        db=db,
    )
    if failure_kind == "duplicate-current-host":
        args.extend(("--source-inventory", str(second_current)))

    result = CliRunner().invoke(main, args)

    assert result.exit_code != 0
    assert "archive coverage failed" in result.output
    if original is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == original
    _assert_private_values_absent(
        result,
        caplog,
        current_path,
        second_current,
        pond_path,
        output,
        db,
        "host-private",
        "native-private",
    )


def test_coverage_accepts_private_prior_inventories_but_no_apply_option(
    monkeypatch, tmp_path, caplog
):
    current_path = tmp_path / "current-private.json"
    prior_path = tmp_path / "prior-private.json"
    pond_path = tmp_path / "pond-private.json"
    output = tmp_path / "coverage-private.json"
    _write_native(current_path)
    _write_native(prior_path, _source_inventory("prior-private-host"))
    _write_pond(pond_path)
    monkeypatch.setattr(server_main, "load_registry_candidates", lambda _path: ())

    success = CliRunner().invoke(
        main,
        _coverage_args(
            output=output,
            source=current_path,
            pond=pond_path,
            prior=(prior_path,),
        ),
    )

    assert success.exit_code == 0, success.output
    assert json.loads(success.output)["ready_for_next_writer"] is True
    assert _mode(output) == 0o600

    mutation_output = tmp_path / "mutation-private.json"
    rejected = CliRunner().invoke(
        main,
        _coverage_args(
            output=mutation_output,
            source=current_path,
            pond=pond_path,
        )
        + ["--apply"],
    )

    assert rejected.exit_code != 0
    assert "No such option: --apply" in rejected.output
    assert not mutation_output.exists()
    _assert_private_values_absent(
        rejected,
        caplog,
        current_path,
        prior_path,
        pond_path,
        mutation_output,
        "host-private",
        "native-private",
    )
