"""Privacy-safe Click wiring for local archive inventory coverage."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import duckdb
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
    SourceEligibilityReceipt,
    load_source_eligibility_receipt,
    write_private_json,
)
from drover.server.archive.native_inventory import native_inventory_summary
from drover.server.archive.pond_inventory import pond_inventory_summary

_CAPTURED_AT = "2026-08-29T12:00:00Z"
_UPDATED_AT = "2026-08-29T11:00:00Z"
_CREATED_AT = "2026-08-29T10:00:00Z"
_FIRST_MESSAGE_AT = "2026-08-29T10:01:00Z"
_LAST_MESSAGE_AT = "2026-08-29T10:02:00Z"

_MINIMAL_POND = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

Path(os.environ["LOCAL_POND_MARKER"]).write_text("local", encoding="utf-8")
if sys.argv[1:] == ["--version"]:
    print("pond 0.16.3 (23c7d0e aarch64-macos)")
    raise SystemExit(0)

output = Path(sys.argv[sys.argv.index("--output-file") + 1])
sql = sys.argv[sys.argv.index("sql") + 1]
if "worst_case_ndjson_bytes" in sql:
    row = {"row_count": 1, "worst_case_ndjson_bytes": 512}
else:
    if os.environ.get("SWAP_FINAL_OUTPUT_PARENT"):
        final_parent = Path(os.environ["SWAP_FINAL_OUTPUT_PARENT"])
        final_parent.rename(Path(os.environ["SWAP_MOVED_OUTPUT_PARENT"]))
        final_parent.symlink_to(
            Path(os.environ["SWAP_OUTPUT_PARENT_TARGET"]),
            target_is_directory=True,
        )
    row = {
        "session_id": "native-private",
        "source_agent": "claude-code",
        "created_at": "2026-08-29T10:00:00Z",
        "message_count": 2,
        "first_message_at": "2026-08-29T10:01:00Z",
        "last_message_at": "2026-08-29T10:02:00Z",
    }
output.write_text(json.dumps(row) + "\n", encoding="utf-8")
"""


def _source_inventory(
    host_id: str = "host-private",
    session_id: str = "native-private",
    *,
    source_agent: str = "claude-code",
    source_fingerprint: str | None = None,
) -> NativeInventory:
    return NativeInventory(
        schema_version=2 if source_fingerprint is not None else 1,
        captured_at=_CAPTURED_AT,
        host_id=host_id,
        records=(
            NativeInventoryRecord(
                source_agent=source_agent,
                session_id=session_id,
                updated_at=_UPDATED_AT,
                size_bytes=123,
                source_copies=1,
                source_fingerprint=source_fingerprint,
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


def _write_receipt(
    path: Path,
    *,
    host_id: str = "host-private",
    session_id: str = "native-private",
    source_fingerprint: str = "a" * 64,
) -> None:
    receipt = SourceEligibilityReceipt(
        1,
        _CAPTURED_AT,
        host_id,
        "claude-code",
        session_id,
        source_fingerprint,
        "source_not_archive_eligible",
    )
    write_private_json(path, receipt.to_wire())


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _write_minimal_pond(path: Path) -> None:
    path.write_text(_MINIMAL_POND, encoding="utf-8")
    path.chmod(0o700)


def _write_isolated_config(tmp_path: Path) -> Path:
    config = tmp_path / "isolated-config.toml"
    config.write_text(
        f"""[paths]
incoming_dir = {json.dumps(str(tmp_path / 'incoming'))}
parquet_dir = {json.dumps(str(tmp_path / 'parquet'))}
duckdb_path = {json.dumps(str(tmp_path / 'configured.duckdb'))}
processed_retention_days = 7

[server]
otlp_grpc_port = 14317
mcp_http_port = 17077

[agent]
agent_id = "test"
principal_id = "test"
""",
        encoding="utf-8",
    )
    return config


def _write_empty_registry(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("""
            CREATE TABLE harness_sessions (
                session_id VARCHAR,
                host_id VARCHAR,
                harness VARCHAR,
                native_session_id VARCHAR
            )
            """)


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
    config: Path,
    db: Path | None = None,
    prior: tuple[Path, ...] = (),
    receipts: tuple[Path, ...] = (),
) -> list[str]:
    args: list[str] = ["--config", str(config)]
    args.extend(
        [
            "archive",
            "coverage",
            "--output",
            str(output),
            "--source-inventory",
            str(source),
            "--pond-inventory",
            str(pond),
        ]
    )
    if db is not None:
        args.extend(("--db", str(db)))
    for path in prior:
        args.extend(("--prior-source-inventory", str(path)))
    for path in receipts:
        args.extend(("--source-eligibility-receipt", str(path)))
    return args


def test_archive_help_exposes_only_the_local_operator_commands():
    runner = CliRunner()

    result = runner.invoke(main, ["archive", "--help"])

    assert result.exit_code == 0, result.output
    assert set(server_main.archive_cmd.commands) == {
        "backup",
        "source-inventory",
        "source-eligibility",
        "pond-inventory",
        "coverage",
    }

    def option_names(command_name):
        command = server_main.archive_cmd.commands[command_name]
        return {
            option
            for parameter in command.params
            for option in parameter.opts
            if option.startswith("--")
        }

    assert option_names("source-inventory") == {"--host-id", "--output"}
    assert option_names("source-eligibility") == {
        "--host-id",
        "--source",
        "--output",
    }
    assert option_names("pond-inventory") == {
        "--storage-path",
        "--output",
        "--pond-binary",
        "--timeout",
    }
    assert option_names("coverage") == {
        "--output",
        "--db",
        "--source-inventory",
        "--pond-inventory",
        "--prior-source-inventory",
        "--source-eligibility-receipt",
    }

    pond_help = runner.invoke(main, ["archive", "pond-inventory", "--help"])
    assert pond_help.exit_code == 0, pond_help.output
    assert "--timeout SECONDS" in pond_help.output


def test_source_eligibility_cli_writes_private_receipt_and_only_aggregate_stdout(
    tmp_path, caplog
):
    session_id = "metadata-only-private"
    source = tmp_path / ".claude/projects/project" / f"{session_id}.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "type": "ai-title",
                "sessionId": session_id,
                "title": "private title",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "private-eligibility-receipt.json"

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "source-eligibility",
            "--host-id",
            "host-private",
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        env={"HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "receipts": 1,
        "schema_version": 1,
        "source_not_archive_eligible": 1,
    }
    receipt = load_source_eligibility_receipt(output)
    assert receipt.session_id == session_id
    assert receipt.host_id == "host-private"
    assert _mode(output) == 0o600
    _assert_private_values_absent(
        result,
        caplog,
        source,
        output,
        session_id,
        "host-private",
        "private title",
        receipt.source_fingerprint,
    )


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


@pytest.mark.parametrize("resolution", ["explicit-relative", "environment-relative"])
def test_pond_inventory_cli_executes_the_validated_relative_binary(
    resolution, monkeypatch, tmp_path, caplog
):
    local_binary = tmp_path / "pond"
    _write_minimal_pond(local_binary)
    control = tmp_path / "fake-control"
    control.mkdir()
    local_marker = control / "local-pond-ran"
    path_bin = tmp_path / "hostile-path"
    path_bin.mkdir()
    namesake_marker = control / "path-namesake-ran"
    namesake = path_bin / "pond"
    namesake.write_text(
        '#!/bin/sh\nprintf namesake > "$PATH_NAMESAKE_MARKER"\n'
        "printf 'pond 9.9.9\\n'\n",
        encoding="utf-8",
    )
    namesake.chmod(0o700)
    storage = tmp_path / "private-store"
    storage.mkdir()
    output = tmp_path / "private-pond-output.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(path_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("LOCAL_POND_MARKER", str(local_marker))
    monkeypatch.setenv("PATH_NAMESAKE_MARKER", str(namesake_marker))
    args = [
        "archive",
        "pond-inventory",
        "--storage-path",
        str(storage),
        "--output",
        str(output),
    ]
    if resolution == "explicit-relative":
        monkeypatch.setenv("POND_BINARY", str(namesake))
        args.extend(("--pond-binary", "./pond"))
    else:
        monkeypatch.setenv("POND_BINARY", "pond")

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "archive_sessions": 1,
        "by_harness": {"claude-code": 1},
        "empty_sessions": 0,
        "pond_version": "0.16.3",
        "schema_version": 1,
    }
    assert local_marker.read_text(encoding="utf-8") == "local"
    assert not namesake_marker.exists()
    assert _mode(output) == 0o600
    _assert_private_values_absent(
        result, caplog, local_binary, local_marker, namesake, namesake_marker, output
    )


@pytest.mark.parametrize("output_kind", ["equal", "inside", "symlink-alias"])
def test_pond_inventory_cli_refuses_output_in_original_store_before_pond(
    output_kind, monkeypatch, tmp_path, caplog
):
    binary = tmp_path / "private-pond"
    _write_minimal_pond(binary)
    marker = tmp_path / "private-pond-ran"
    storage = tmp_path / "private-store"
    storage.mkdir()
    sentinel = storage / "opaque-store-data"
    sentinel.write_bytes(b"unchanged")
    alias = tmp_path / "existing-parent-alias"
    if output_kind == "equal":
        output = storage
    elif output_kind == "inside":
        output = storage / "private-inventory.json"
    else:
        alias.symlink_to(storage, target_is_directory=True)
        output = alias / "private-inventory.json"
    before_entries = tuple(sorted(path.name for path in storage.iterdir()))
    before_sentinel = sentinel.stat()
    monkeypatch.setenv("LOCAL_POND_MARKER", str(marker))

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
    assert result.output == "Error: archive pond inventory failed\n"
    assert not marker.exists()
    assert tuple(sorted(path.name for path in storage.iterdir())) == before_entries
    assert sentinel.read_bytes() == b"unchanged"
    after_sentinel = sentinel.stat()
    assert (
        after_sentinel.st_ino,
        after_sentinel.st_size,
        after_sentinel.st_mtime_ns,
        after_sentinel.st_ctime_ns,
    ) == (
        before_sentinel.st_ino,
        before_sentinel.st_size,
        before_sentinel.st_mtime_ns,
        before_sentinel.st_ctime_ns,
    )
    _assert_private_values_absent(result, caplog, binary, storage, output, marker)


def test_pond_inventory_cli_fails_closed_if_output_parent_is_swapped_to_store(
    monkeypatch, tmp_path, caplog
):
    binary = tmp_path / "private-pond"
    _write_minimal_pond(binary)
    marker = tmp_path / "private-pond-ran"
    storage = tmp_path / "private-store"
    storage.mkdir()
    output_parent = tmp_path / "initially-external-output"
    output_parent.mkdir()
    moved_parent = tmp_path / "moved-original-output"
    output = output_parent / "private-inventory.json"
    monkeypatch.setenv("LOCAL_POND_MARKER", str(marker))
    monkeypatch.setenv("SWAP_FINAL_OUTPUT_PARENT", str(output_parent))
    monkeypatch.setenv("SWAP_MOVED_OUTPUT_PARENT", str(moved_parent))
    monkeypatch.setenv("SWAP_OUTPUT_PARENT_TARGET", str(storage))

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
    assert result.output == "Error: archive pond inventory failed\n"
    assert marker.exists()
    assert output_parent.is_symlink()
    assert output_parent.resolve() == storage
    assert not (storage / output.name).exists()
    assert not (moved_parent / output.name).exists()
    assert not output.exists()
    _assert_private_values_absent(
        result,
        caplog,
        binary,
        marker,
        storage,
        output_parent,
        moved_parent,
        output,
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


@pytest.mark.parametrize(
    "raw_timeout",
    [
        "/SENSITIVE/private/timeout-token",
        "not-a-number-SENSITIVE",
        "nan",
        "inf",
        "-inf",
        "4.99",
        "601",
    ],
)
def test_pond_inventory_timeout_errors_never_disclose_the_raw_token(
    raw_timeout, monkeypatch, tmp_path, caplog
):
    storage = tmp_path / "private-store"
    storage.mkdir()
    output = tmp_path / "private-output.json"
    binary = tmp_path / "private-pond"
    export_called = False

    def unexpected_export(*_args, **_kwargs):
        nonlocal export_called
        export_called = True
        raise AssertionError("invalid timeout reached Pond export")

    monkeypatch.setattr(server_main, "export_pond_inventory", unexpected_export)

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
            "--timeout",
            raw_timeout,
        ],
    )

    assert result.exit_code != 0
    assert result.output == "Error: archive pond inventory invalid timeout\n"
    assert export_called is False
    assert not output.exists()
    _assert_private_values_absent(
        result, caplog, raw_timeout, storage, output, binary, "SENSITIVE"
    )


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
    config = _write_isolated_config(tmp_path)
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
            config=config,
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
        config,
        resolved_registry,
        snapshot,
        "drover-private",
        "host-private",
        "native-private",
        pond_session_id,
    )


def test_coverage_cli_applies_private_eligibility_receipt_to_matching_v2_source(
    tmp_path, caplog
):
    fingerprint = "a" * 64
    config = _write_isolated_config(tmp_path)
    registry = tmp_path / "isolated.registry.duckdb"
    _write_empty_registry(registry)
    current_path = tmp_path / "current-private.json"
    pond_path = tmp_path / "pond-private.json"
    receipt_path = tmp_path / "receipt-private.json"
    output = tmp_path / "coverage-private.json"
    _write_native(
        current_path,
        _source_inventory(source_fingerprint=fingerprint),
    )
    _write_pond(pond_path, PondInventory(1, _CAPTURED_AT, "0.16.3", ()))
    _write_receipt(receipt_path, source_fingerprint=fingerprint)

    result = CliRunner().invoke(
        main,
        _coverage_args(
            output=output,
            source=current_path,
            pond=pond_path,
            config=config,
            db=registry,
            receipts=(receipt_path,),
        ),
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["current_source_coverage"] == {
        "discovered": 1,
        "matched": 0,
        "source_not_archive_eligible": 1,
        "discovered_not_synced": 0,
    }
    assert summary["ready_for_next_writer"] is True
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["current_source_details"][0]["status"] == (
        "source_not_archive_eligible"
    )
    _assert_private_values_absent(
        result,
        caplog,
        config,
        registry,
        current_path,
        pond_path,
        receipt_path,
        output,
        fingerprint,
        "host-private",
        "native-private",
    )


@pytest.mark.parametrize("manifest_role", ["pond", "prior"])
@pytest.mark.parametrize("failure_kind", ["missing", "unsafe", "wrong-kind"])
def test_coverage_independently_rejects_invalid_pond_and_prior_manifests(
    manifest_role, failure_kind, tmp_path, caplog
):
    config = _write_isolated_config(tmp_path)
    registry = tmp_path / "isolated.registry.duckdb"
    _write_empty_registry(registry)
    current_path = tmp_path / "current-private.json"
    pond_path = tmp_path / "pond-private.json"
    prior_path = tmp_path / "prior-private.json"
    output = tmp_path / "coverage-private.json"
    _write_native(current_path)
    if manifest_role != "pond" or failure_kind != "missing":
        if manifest_role == "pond" and failure_kind == "wrong-kind":
            _write_native(pond_path)
        else:
            _write_pond(pond_path)
    if manifest_role != "prior" or failure_kind != "missing":
        if manifest_role == "prior" and failure_kind == "wrong-kind":
            _write_pond(prior_path)
        else:
            _write_native(prior_path, _source_inventory("prior-private-host"))
    invalid_path = pond_path if manifest_role == "pond" else prior_path
    if failure_kind == "unsafe":
        invalid_path.chmod(0o644)

    result = CliRunner().invoke(
        main,
        _coverage_args(
            output=output,
            source=current_path,
            pond=pond_path,
            config=config,
            db=registry,
            prior=(prior_path,),
        ),
    )

    assert result.exit_code != 0
    assert result.output == "Error: archive coverage failed\n"
    assert not output.exists()
    _assert_private_values_absent(
        result,
        caplog,
        config,
        registry,
        current_path,
        pond_path,
        prior_path,
        output,
        "host-private",
        "prior-private-host",
        "native-private",
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
    config = _write_isolated_config(tmp_path)
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
        config=config,
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
        config,
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
    config = _write_isolated_config(tmp_path)
    registry = tmp_path / "isolated.registry.duckdb"
    _write_empty_registry(registry)
    _write_native(current_path)
    _write_native(prior_path, _source_inventory("prior-private-host"))
    _write_pond(pond_path)
    real_resolve_config = server_main._resolve_config

    def reject_operator_config(config_path):
        assert config_path is not None, "test reached the default operator config"
        return real_resolve_config(config_path)

    monkeypatch.setattr(server_main, "_resolve_config", reject_operator_config)

    success = CliRunner().invoke(
        main,
        _coverage_args(
            output=output,
            source=current_path,
            pond=pond_path,
            config=config,
            db=registry,
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
            config=config,
            db=registry,
        )
        + ["--apply"],
    )

    assert rejected.exit_code != 0
    assert "No such option" in rejected.output
    assert "--apply" in rejected.output
    assert not mutation_output.exists()
    _assert_private_values_absent(
        rejected,
        caplog,
        current_path,
        prior_path,
        pond_path,
        config,
        registry,
        mutation_output,
        "host-private",
        "native-private",
    )
