"""Contract tests for the protected, validation-only App Store probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "ios" / "validate_testflight_contract.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ios-testflight-validation-only.yml"
KEY_ID = "EXAMPLE123"
ISSUER = "11111111-2222-3333-4444-555555555555"
SOURCE_SHA = "a" * 40
SECRET = "private-contract-sentinel"


def load_contract_module():
    spec = importlib.util.spec_from_file_location("validation_contract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(tmp_path: Path):
    ipa = tmp_path / "Drover.ipa"
    ipa.write_bytes(b"synthetic IPA")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    key = keys / f"AuthKey_{KEY_ID}.p8"
    key.write_text("synthetic key")
    key.chmod(0o600)
    private_output = tmp_path / "private-output"
    private_output.mkdir(mode=0o700)
    return [
        "validate_testflight_contract.py",
        "--ipa",
        str(ipa),
        "--api-key-id",
        KEY_ID,
        "--api-issuer",
        ISSUER,
        "--private-keys-dir",
        str(keys),
        "--record",
        str(tmp_path / "receipt.json"),
        "--private-output-dir",
        str(private_output),
        "--source-sha",
        SOURCE_SHA,
        "--version",
        "0.2.0",
        "--build",
        "25",
    ]


def test_validation_only_receipt_redacts_success_and_never_uploads(
    monkeypatch, tmp_path
):
    module = load_contract_module()
    calls = []

    def run(command, *, stdout, stderr, **kwargs):
        calls.append(command)
        if command[-1] == "--version":
            stdout.write(b"27.0.5 (2.1)\n")
            return subprocess.CompletedProcess(command, 0)
        assert "--validate-app" in command
        assert "--upload-app" not in command
        stdout.write(
            json.dumps(
                {
                    "success-message": f"No errors validating {SECRET}",
                    "product-errors": [],
                    "product-warnings": [{"message": SECRET}],
                }
            ).encode()
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", arguments(tmp_path))
    assert module.main() == 1
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt == {
        "action": "validation-only-contract-evidence",
        "build": "25",
        "exit_class": "zero",
        "no_upload": True,
        "product_errors_shape": "empty-list",
        "product_warnings_shape": "nonempty-list",
        "response_type": "object",
        "result_class": "positive-contract-unconfirmed",
        "source_sha": SOURCE_SHA,
        "success_message": {
            "class": "no-errors-validating-prefix",
            "presence": "present",
            "type": "string",
        },
        "tool_version": "27.0.5",
        "validation_confirmed": False,
        "version": "0.2.0",
    }
    evidence = json.dumps(receipt)
    assert SECRET not in evidence
    assert len(calls) == 2
    assert any("--validate-app" in call for call in calls)
    assert all("--upload-app" not in call for call in calls)


def test_validation_only_nonzero_and_malformed_output_stay_fail_closed(
    monkeypatch, tmp_path
):
    module = load_contract_module()

    def run(command, *, stdout, stderr, **kwargs):
        if command[-1] == "--version":
            stdout.write(b"version unavailable\n")
            return subprocess.CompletedProcess(command, 0)
        stdout.write(b"{not-json")
        stderr.write(SECRET.encode())
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", arguments(tmp_path))
    assert module.main() == 1
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["exit_class"] == "nonzero"
    assert receipt["response_type"] == "invalid-json"
    assert receipt["result_class"] == "nonzero-tool-exit"
    assert receipt["tool_version"] == "unknown"
    assert SECRET not in json.dumps(receipt)


def test_validation_only_product_errors_remain_a_hard_stop(monkeypatch, tmp_path):
    module = load_contract_module()

    def run(command, *, stdout, stderr, **kwargs):
        if command[-1] == "--version":
            stdout.write(b"27.0.5\n")
            return subprocess.CompletedProcess(command, 0)
        stdout.write(json.dumps({"product-errors": [{"message": SECRET}]}).encode())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", arguments(tmp_path))
    assert module.main() == 1
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["result_class"] == "product-errors-present"
    assert receipt["validation_confirmed"] is False
    assert SECRET not in json.dumps(receipt)


def test_validation_only_workflow_is_main_bound_and_cannot_upload_or_distribute():
    workflow = WORKFLOW.read_text()
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "environment: ios-testflight-upload" in workflow
    assert "python3 scripts/ios/require_ios_ci.py" in workflow
    assert "git rev-parse HEAD" in workflow
    assert "version:" in workflow and 'default: "0.2.0"' in workflow
    assert "build:" in workflow and 'default: "25"' in workflow
    assert "validate_testflight_contract.py" in workflow
    assert "upload_testflight.sh" not in workflow
    assert "--upload-app" not in workflow
    assert "distribute(" not in workflow
    assert "trap 'rm -rf \"$PRIVATE_OUTPUT\"' EXIT" in workflow
