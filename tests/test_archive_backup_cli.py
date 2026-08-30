"""CLI contracts for the private Pond backup workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from drover.server import __main__ as server_main
from drover.server.__main__ import main

_ERROR_CATEGORIES = (
    "archive backup config failed",
    "archive backup preflight failed",
    "archive backup local changed",
    "archive backup storage unavailable",
    "archive backup copy failed",
    "archive backup verify failed",
    "archive backup receipt failed",
    "archive backup restore failed",
    "archive backup resource limit",
)
_PRIVATE_CONFIG = "/private/operator/backup config.toml"
_PRIVATE_RECEIPT = "/private/operator/receipts/backup-private-id.json"
_PRIVATE_DESTINATION = "/private/operator/restores/private-generation"


def _command_names(output: str) -> set[str]:
    return set(re.findall(r"^  ([a-z][a-z-]*)\s{2,}", output, re.MULTILINE))


def _option_names(output: str) -> set[str]:
    return set(re.findall(r"(?<!\w)--[a-z][a-z-]*", output))


def _global_args(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "isolated-drover.toml")]


def _assert_one_sorted_json(result, expected: dict[str, object]) -> None:
    assert result.exit_code == 0, result.stderr
    assert result.stdout == json.dumps(expected, sort_keys=True) + "\n"
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1


def _install_happy_services(monkeypatch, tmp_path: Path) -> dict[str, object]:
    values: dict[str, object] = {
        "backup_config": object(),
        "drover_config": object(),
        "preflight_result": object(),
        "receipt": object(),
        "restore_result": object(),
    }
    preflight_summary = {
        "coverage": {"ready_for_next_writer": True},
        "pond_corpus": {"messages": 3, "parts": 5, "sessions": 2},
        "ready": True,
        "schema_version": 1,
        "source_inventory": {"records": 2},
        "source_not_archive_eligible": 0,
    }
    receipt_summary = {
        "collision_counts": {
            "archive_logical_duplicate_candidate_groups": 0,
            "archive_signature_unverifiable": 0,
            "cross_harness_native_id_groups": 0,
            "duplicate_source_groups": 0,
        },
        "copy_duration_ms": 10,
        "health_p95_ms": 2.0,
        "health_samples": 30,
        "messages": 3,
        "parts": 5,
        "peak_physical_bytes": 128,
        "peak_rss_bytes": 64,
        "pond_version": "0.16.3",
        "result": "verified",
        "schema_version": 1,
        "sessions": 2,
        "source_not_archive_eligible": 0,
        "swap_delta_bytes": 0,
        "verify_duration_ms": 5,
    }
    restore_summary = {
        "current_source_coverage": "current",
        "health_p95_ms": 2.0,
        "health_samples": 30,
        "messages": 3,
        "parts": 5,
        "peak_physical_bytes": 128,
        "peak_rss_bytes": 64,
        "schema_version": 1,
        "sessions": 2,
        "store_started": False,
        "swap_delta_bytes": 0,
        "verified": True,
    }
    values.update(
        {
            "preflight_summary": preflight_summary,
            "receipt_summary": receipt_summary,
            "restore_summary": restore_summary,
            "calls": [],
        }
    )

    def load_backup_config(path: str):
        values["calls"].append(("load-config", path))
        return values["backup_config"]

    def resolve_config(path: str):
        values["calls"].append(("resolve-config", path))
        return values["drover_config"]

    def preflight(config, drover_config):
        values["calls"].append(("preflight", config, drover_config))
        return preflight_summary

    def run_backup(config, drover_config):
        values["calls"].append(("run", config, drover_config))
        return values["receipt"]

    def summarize_backup(receipt):
        values["calls"].append(("summarize-backup", receipt))
        return receipt_summary

    def validate_restore(config, receipt: Path, destination: Path):
        values["calls"].append(("validate-restore", config, receipt, destination))

    def restore(config, receipt: Path, destination: Path, drover_config):
        values["calls"].append(("restore", config, receipt, destination, drover_config))
        return values["restore_result"]

    def summarize_restore(result):
        values["calls"].append(("summarize-restore", result))
        return restore_summary

    def load_receipt(path: str):
        values["calls"].append(("load-receipt", path))
        return values["receipt"]

    def summarize_receipt(receipt):
        values["calls"].append(("summarize-receipt", receipt))
        return receipt_summary

    monkeypatch.setattr(
        server_main, "load_backup_config", load_backup_config, raising=False
    )
    monkeypatch.setattr(server_main, "_resolve_config", resolve_config)
    monkeypatch.setattr(
        server_main, "_run_backup_preflight_for_cli", preflight, raising=False
    )
    monkeypatch.setattr(server_main, "run_backup", run_backup, raising=False)
    monkeypatch.setattr(
        server_main, "backup_run_summary", summarize_backup, raising=False
    )
    monkeypatch.setattr(
        server_main,
        "validate_restore_request",
        validate_restore,
        raising=False,
    )
    monkeypatch.setattr(server_main, "restore_backup", restore, raising=False)
    monkeypatch.setattr(
        server_main, "restore_summary", summarize_restore, raising=False
    )
    monkeypatch.setattr(server_main, "load_backup_receipt", load_receipt, raising=False)
    monkeypatch.setattr(
        server_main,
        "backup_receipt_summary",
        summarize_receipt,
        raising=False,
    )
    return values


def test_archive_backup_help_is_exact() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["archive", "backup", "--help"])

    assert result.exit_code == 0
    assert _command_names(result.stdout) == {
        "inspect-receipt",
        "preflight",
        "restore",
        "run",
    }
    expected_options = {
        "preflight": {"--config", "--help"},
        "run": {"--apply", "--config", "--help"},
        "restore": {
            "--apply",
            "--config",
            "--destination",
            "--help",
            "--receipt",
        },
        "inspect-receipt": {"--help", "--receipt"},
    }
    for command, options in expected_options.items():
        help_result = runner.invoke(main, ["archive", "backup", command, "--help"])
        assert help_result.exit_code == 0
        assert _option_names(help_result.stdout) == options


def test_cli_preflight_composer_uses_a_private_temporary_workspace_and_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    config = SimpleNamespace(receipt_directory=receipts)
    drover_config = object()
    preflight_result = object()
    summary = {"ready": True, "schema_version": 1}
    events: list[object] = []
    workspace_path: Path | None = None

    class RecordingRuntime:
        def __init__(self, supplied_config) -> None:
            events.append(("runtime", supplied_config))

        def capture_baseline(self) -> None:
            events.append("baseline")

        def finish(self) -> None:
            events.append("finish")

    def preflight(supplied_config, supplied_drover_config, workspace, runtime):
        nonlocal workspace_path
        workspace_path = workspace
        events.append(
            (
                "preflight",
                supplied_config,
                supplied_drover_config,
                runtime,
                workspace.stat().st_mode & 0o777,
            )
        )
        return preflight_result

    def summarize(result):
        events.append(("summary", result))
        return summary

    monkeypatch.setattr(server_main, "RuntimeGuard", RecordingRuntime)
    monkeypatch.setattr(server_main, "run_backup_preflight", preflight)
    monkeypatch.setattr(server_main, "backup_preflight_summary", summarize)

    assert server_main._run_backup_preflight_for_cli(config, drover_config) == summary
    assert events[0:2] == [("runtime", drover_config), "baseline"]
    assert events[2][0:4] == (
        "preflight",
        config,
        drover_config,
        events[2][3],
    )
    assert events[2][4] == 0o700
    assert events[3:] == ["finish", ("summary", preflight_result)]
    assert workspace_path is not None
    assert workspace_path.parent == receipts
    assert not workspace_path.exists()


def test_preflight_emits_one_aggregate_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + ["archive", "backup", "preflight", "--config", _PRIVATE_CONFIG],
    )

    _assert_one_sorted_json(result, services["preflight_summary"])
    assert services["calls"] == [
        ("load-config", _PRIVATE_CONFIG),
        ("resolve-config", str(tmp_path / "isolated-drover.toml")),
        (
            "preflight",
            services["backup_config"],
            services["drover_config"],
        ),
    ]


def test_preflight_not_ready_emits_report_then_exits_two(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)
    report = {"ready": False, "schema_version": 1}
    monkeypatch.setattr(
        server_main, "_run_backup_preflight_for_cli", lambda *_args: report
    )

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + ["archive", "backup", "preflight", "--config", _PRIVATE_CONFIG],
    )

    assert result.exit_code == 2
    assert result.stdout == json.dumps(report, sort_keys=True) + "\n"
    assert result.stderr == ""
    assert all(call[0] not in {"run", "restore"} for call in services["calls"])


def test_run_without_apply_is_local_only(tmp_path: Path, monkeypatch) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + ["archive", "backup", "run", "--config", _PRIVATE_CONFIG],
    )

    _assert_one_sorted_json(
        result,
        {
            "mode": "dry-run",
            "preflight_ready": True,
            "remote_contacted": False,
            "schema_version": 1,
        },
    )
    assert [call[0] for call in services["calls"]] == [
        "load-config",
        "resolve-config",
        "preflight",
    ]


def test_run_with_apply_uses_only_the_aggregate_receipt_summary(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + [
            "archive",
            "backup",
            "run",
            "--config",
            _PRIVATE_CONFIG,
            "--apply",
        ],
    )

    _assert_one_sorted_json(result, services["receipt_summary"])
    assert [call[0] for call in services["calls"]] == [
        "load-config",
        "resolve-config",
        "run",
        "summarize-backup",
    ]


def test_restore_without_apply_validates_only_local_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)
    destination = tmp_path / "not-created"

    def forbid_runtime_config(_path: str):
        raise AssertionError("restore dry-run must not resolve runtime config")

    monkeypatch.setattr(server_main, "_resolve_config", forbid_runtime_config)

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + [
            "archive",
            "backup",
            "restore",
            "--config",
            _PRIVATE_CONFIG,
            "--receipt",
            _PRIVATE_RECEIPT,
            "--destination",
            str(destination),
        ],
    )

    _assert_one_sorted_json(
        result,
        {
            "destination_valid": True,
            "mode": "dry-run",
            "receipt_chain_valid": True,
            "remote_contacted": False,
            "schema_version": 1,
            "store_started": False,
        },
    )
    assert services["calls"] == [
        ("load-config", _PRIVATE_CONFIG),
        (
            "validate-restore",
            services["backup_config"],
            Path(_PRIVATE_RECEIPT),
            destination,
        ),
    ]
    assert not destination.exists()


def test_restore_with_apply_uses_only_the_aggregate_restore_summary(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        _global_args(tmp_path)
        + [
            "archive",
            "backup",
            "restore",
            "--config",
            _PRIVATE_CONFIG,
            "--receipt",
            _PRIVATE_RECEIPT,
            "--destination",
            _PRIVATE_DESTINATION,
            "--apply",
        ],
    )

    _assert_one_sorted_json(result, services["restore_summary"])
    assert [call[0] for call in services["calls"]] == [
        "load-config",
        "resolve-config",
        "restore",
        "summarize-restore",
    ]


def test_inspect_receipt_accepts_a_raw_path_and_emits_only_aggregate_summary(
    tmp_path: Path, monkeypatch
) -> None:
    services = _install_happy_services(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "backup",
            "inspect-receipt",
            "--receipt",
            _PRIVATE_RECEIPT,
        ],
    )

    _assert_one_sorted_json(result, services["receipt_summary"])
    assert services["calls"] == [
        ("load-receipt", _PRIVATE_RECEIPT),
        ("summarize-receipt", services["receipt"]),
    ]


@pytest.mark.parametrize("category", _ERROR_CATEGORIES)
def test_each_service_error_maps_to_exactly_one_fixed_category(
    category: str, tmp_path: Path, monkeypatch
) -> None:
    _install_happy_services(monkeypatch, tmp_path)

    def fail(*_args, **_kwargs):
        raise ValueError(category)

    if category == "archive backup config failed":
        monkeypatch.setattr(server_main, "load_backup_config", fail)
        command = ["archive", "backup", "preflight", "--config", _PRIVATE_CONFIG]
    elif category == "archive backup preflight failed":
        monkeypatch.setattr(server_main, "_run_backup_preflight_for_cli", fail)
        command = ["archive", "backup", "preflight", "--config", _PRIVATE_CONFIG]
    elif category == "archive backup receipt failed":
        monkeypatch.setattr(server_main, "load_backup_receipt", fail)
        command = [
            "archive",
            "backup",
            "inspect-receipt",
            "--receipt",
            _PRIVATE_RECEIPT,
        ]
    elif category == "archive backup restore failed":
        monkeypatch.setattr(server_main, "validate_restore_request", fail)
        command = [
            "archive",
            "backup",
            "restore",
            "--config",
            _PRIVATE_CONFIG,
            "--receipt",
            _PRIVATE_RECEIPT,
            "--destination",
            _PRIVATE_DESTINATION,
        ]
    else:
        monkeypatch.setattr(server_main, "run_backup", fail)
        command = [
            "archive",
            "backup",
            "run",
            "--config",
            _PRIVATE_CONFIG,
            "--apply",
        ]

    result = CliRunner().invoke(main, _global_args(tmp_path) + command)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: {category}\n"
    assert sum(error in result.stderr for error in _ERROR_CATEGORIES) == 1


@pytest.mark.parametrize(
    ("stage", "expected_category"),
    (
        ("load-config", "archive backup config failed"),
        ("resolve-config", "archive backup config failed"),
        ("preflight", "archive backup preflight failed"),
        ("run", "archive backup preflight failed"),
        ("summarize-backup", "archive backup receipt failed"),
        ("validate-restore", "archive backup restore failed"),
        ("restore", "archive backup restore failed"),
        ("summarize-restore", "archive backup restore failed"),
        ("load-receipt", "archive backup receipt failed"),
        ("summarize-receipt", "archive backup receipt failed"),
    ),
)
def test_service_failures_never_expose_private_values(
    stage: str, expected_category: str, tmp_path: Path, monkeypatch
) -> None:
    _install_happy_services(monkeypatch, tmp_path)
    private_values = (
        _PRIVATE_CONFIG,
        _PRIVATE_RECEIPT,
        _PRIVATE_DESTINATION,
        "s3+https://private-account.r2.cloudflarestorage.com/private-bucket",
        "token_sk-private-0123456789",
        "child stdout transcript-private child stderr",
        "123e4567-e89b-42d3-a456-426614174000",
        "a" * 64,
        "host-private source-private session-private",
    )
    secret = " | ".join(private_values)

    def fail(*_args, **_kwargs):
        raise RuntimeError(secret)

    attribute_by_stage = {
        "load-config": "load_backup_config",
        "resolve-config": "_resolve_config",
        "preflight": "_run_backup_preflight_for_cli",
        "run": "run_backup",
        "summarize-backup": "backup_run_summary",
        "validate-restore": "validate_restore_request",
        "restore": "restore_backup",
        "summarize-restore": "restore_summary",
        "load-receipt": "load_backup_receipt",
        "summarize-receipt": "backup_receipt_summary",
    }
    monkeypatch.setattr(server_main, attribute_by_stage[stage], fail)

    if stage in {"load-receipt", "summarize-receipt"}:
        command = [
            "archive",
            "backup",
            "inspect-receipt",
            "--receipt",
            _PRIVATE_RECEIPT,
        ]
    elif stage in {"validate-restore"}:
        command = [
            "archive",
            "backup",
            "restore",
            "--config",
            _PRIVATE_CONFIG,
            "--receipt",
            _PRIVATE_RECEIPT,
            "--destination",
            _PRIVATE_DESTINATION,
        ]
    elif stage in {"restore", "summarize-restore"}:
        command = [
            "archive",
            "backup",
            "restore",
            "--config",
            _PRIVATE_CONFIG,
            "--receipt",
            _PRIVATE_RECEIPT,
            "--destination",
            _PRIVATE_DESTINATION,
            "--apply",
        ]
    elif stage in {"run", "summarize-backup"}:
        command = [
            "archive",
            "backup",
            "run",
            "--config",
            _PRIVATE_CONFIG,
            "--apply",
        ]
    else:
        command = ["archive", "backup", "preflight", "--config", _PRIVATE_CONFIG]

    result = CliRunner().invoke(main, _global_args(tmp_path) + command)

    public_surface = result.stdout + result.stderr + repr(result.exception)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: {expected_category}\n"
    for private in private_values:
        assert private not in public_surface
