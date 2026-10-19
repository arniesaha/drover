"""Exercise synthetic artifact fixtures and real unsigned Xcode metadata builds."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_ios_archive_verifier import (
    valid_info,
    write_bundle,
    write_executable,
    write_signing_config,
)

SCRIPTS = Path(__file__).parents[1] / "scripts" / "ios"
HUB = "https://hub.example.test"
ISSUER = "11111111-2222-3333-4444-555555555555"
WIDGET_ISSUER = "66666666-7777-8888-9999-AAAAAAAAAAAA"
KEY_ID = "EXAMPLE123"


@pytest.fixture
def chain(tmp_path: Path):
    binary = tmp_path / "bin"
    binary.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    developer = tmp_path / "developer"
    developer.mkdir()
    logs = tmp_path / "scratch"
    logs.mkdir()
    info = valid_info() | {
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "CFBundleDisplayName": "Drover",
        "MinimumOSVersion": "18.0",
        "CFBundleIcons": {
            "CFBundlePrimaryIcon": {
                "CFBundleIconName": "AppIcon",
                "CFBundleIconFiles": ["AppIcon60x60"],
            }
        },
    }
    app = write_bundle(tmp_path / "fixture", info=info)
    (app / "embedded.mobileprovision").write_bytes(b"synthetic profile fixture")
    widget = app / "PlugIns" / "DroverWidget.appex"
    widget.mkdir(parents=True)
    (widget / "Info.plist").write_bytes(
        plistlib.dumps(info | {"CFBundleIdentifier": "com.arnab.drover.widget"})
    )
    (widget / "embedded.mobileprovision").write_bytes(
        b"synthetic widget profile fixture"
    )
    archive = tmp_path / "Drover.xcarchive"
    archived = archive / "Products" / "Applications"
    archived.mkdir(parents=True)
    shutil.copytree(app, archived / "Drover.app")
    options = private / "ExportOptions.plist"
    options.write_bytes(
        plistlib.dumps(
            {
                "method": "app-store-connect",
                "destination": "export",
                "signingStyle": "manual",
                "manageAppVersionAndBuildNumber": False,
                "testFlightInternalTestingOnly": True,
                "teamID": "TEAMID1234",
                "signingCertificate": "Apple Distribution: Example (TEAMID1234)",
                "provisioningProfiles": {
                    "com.arnab.drover": ISSUER,
                    "com.arnab.drover.widget": WIDGET_ISSUER,
                },
            }
        )
    )
    options.chmod(0o600)
    (private / f"AuthKey_{KEY_ID}.p8").write_text("SYNTHETIC-NOT-A-PRIVATE-KEY")
    (private / f"AuthKey_{KEY_ID}.p8").chmod(0o600)
    ipa = tmp_path / "fixture.ipa"
    ipa.write_bytes(b"synthetic IPA bytes")
    config = write_signing_config(private, keychain=private / "fake.keychain")
    fake = (
        "#!/usr/bin/env python3\n" + """import json, os, pathlib, plistlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"tool": name, "args": args, "keys": os.getenv("API_PRIVATE_KEYS_DIR"), "local_keys": pathlib.Path("private_keys").resolve() == pathlib.Path(os.getenv("API_PRIVATE_KEYS_DIR", "/missing"))}) + "\\n")
if name == "xcodebuild":
    if args == ["-version"]:
        print("Xcode 26.6")
        if os.getenv("XCODE_VERSION_STRESS") == "1":
            sys.stdout.write("Build version synthetic\\n" * 100000)
        else:
            print("Build version synthetic")
    elif "-exportArchive" in args:
        dest = pathlib.Path(args[args.index("-exportPath") + 1])
        for i in range(int(os.getenv("IPA_COUNT", "1"))):
            (dest / f"Drover{i}.ipa").write_bytes(b"synthetic IPA bytes")
        print("private Xcode output " + os.environ["LEAK_SENTINEL"])
    else:
        if os.getenv("ARCHIVE_SIGNING_ERROR") == "1":
            print('error: Provisioning profile "Drover Widget AppStore" (66666666-7777-8888-9999-AAAAAAAAAAAA) does not include the com.apple.security.application-groups entitlement. (in target DroverWidget from project Drover)', file=sys.stderr)
            print('Apple Distribution: Private Organization (TEAMID1234)', file=sys.stderr)
            sys.exit(1)
        dest = pathlib.Path(args[args.index("-archivePath") + 1])
        shutil.copytree(root / "Drover.xcarchive", dest)
elif name == "xcrun":
    if "--show-sdk-version" in args:
        print("26.5")
    else:
        key = pathlib.Path(os.environ["API_PRIVATE_KEYS_DIR"]) / "AuthKey_EXAMPLE123.p8"
        if os.getenv("UPLOAD_RAW"):
            print(os.environ["UPLOAD_RAW"])
            print(os.getenv("UPLOAD_STDERR", ""), file=sys.stderr)
            sys.exit(1)
        validating = "--validate-app" in args
        print(json.dumps({"tool-version": "8.003", "tool-path": "/synthetic/altool", "success-message": ("No errors validating archive." if validating else "No errors uploading archive."), "product-errors": []} | json.loads(os.getenv("VALIDATE_RESPONSE" if validating else "UPLOAD_RESPONSE", "{}")) | {"private": key.read_text() + os.environ["LEAK_SENTINEL"]}))
        if os.getenv("UPLOAD_STDERR"):
            print(os.environ["UPLOAD_STDERR"], file=sys.stderr)
        sys.exit(int(os.getenv("VALIDATE_EXIT" if validating else "UPLOAD_EXIT", "0")))
elif name == "ditto":
    if "-x" in args:
        shutil.copytree(root / "fixture", pathlib.Path(args[-1]) / "Payload")
    else:
        pathlib.Path(args[-1]).write_bytes(b"synthetic archive zip")
elif name == "codesign":
    if "--verify" in args:
        sys.exit(int(os.getenv("CODESIGN_EXIT", "0")))
    elif "-dvv" in args:
        print("Authority=Apple Distribution: Example (TEAMID1234)", file=sys.stderr)
    else:
        if str(args[-1]).endswith("DroverWidget.appex"):
            entitlements = {"application-identifier": "TEAMID1234.com.arnab.drover.widget", "com.apple.developer.team-identifier": "TEAMID1234", "com.apple.security.application-groups": ["group.com.arnab.drover"], "get-task-allow": False}
        else:
            entitlements = {"application-identifier": "TEAMID1234.com.arnab.drover", "com.apple.developer.team-identifier": "TEAMID1234", "aps-environment": "production", "get-task-allow": False}
        sys.stdout.buffer.write(plistlib.dumps(entitlements))
elif name == "security":
    if str(args[-1]).endswith("DroverWidget.appex/embedded.mobileprovision"):
        profile = {"UUID": "66666666-7777-8888-9999-AAAAAAAAAAAA", "TeamIdentifier": ["TEAMID1234"], "Entitlements": {"application-identifier": "TEAMID1234.com.arnab.drover.widget"}}
    else:
        profile = {"UUID": "11111111-2222-3333-4444-555555555555", "TeamIdentifier": ["TEAMID1234"], "Entitlements": {"application-identifier": "TEAMID1234.com.arnab.drover"}}
    sys.stdout.buffer.write(plistlib.dumps(profile))
elif name == "git":
    if "rev-parse" in args:
        print("a" * 40)
elif name != "xcodegen":
    raise SystemExit("unexpected fake tool")
"""
    )
    for name in (
        "xcodebuild",
        "xcrun",
        "ditto",
        "codesign",
        "security",
        "xcodegen",
        "git",
    ):
        write_executable(binary / name, fake)
    env = os.environ | {
        "PATH": f"{binary}:{os.environ['PATH']}",
        "FIXTURE_ROOT": str(tmp_path),
        "DEVELOPER_DIR": str(developer),
        "TMPDIR": str(logs),
        "LEAK_SENTINEL": HUB + ISSUER + "RAW-UPLOAD-RESPONSE",
    }

    def run(script: str, *args: str, **extra: str):
        assert (SCRIPTS / script).exists(), f"missing {script}"
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *map(str, args)],
            capture_output=True,
            text=True,
            env=env | extra,
        )

    def export(*args: str, **extra: str):
        return run(
            "export_ipa.sh",
            "--archive",
            archive,
            "--output",
            tmp_path / "export",
            "--export-options",
            options,
            "--version",
            "1.2.3",
            "--build",
            "42",
            *args,
            **extra,
        )

    def upload(*args: str, **extra: str):
        return run(
            "upload_testflight.sh",
            "--ipa",
            ipa,
            "--api-key-id",
            KEY_ID,
            "--api-issuer",
            ISSUER,
            "--private-keys-dir",
            private,
            "--record",
            tmp_path / "upload-record.json",
            *args,
            **extra,
        )

    return tmp_path, run, export, upload, config


def test_archive_production_channel_requires_local_networking_ats(chain):
    root, run, _, _, config = chain
    archived_info_path = (
        root / "Drover.xcarchive/Products/Applications/Drover.app/Info.plist"
    )
    info = plistlib.loads(archived_info_path.read_bytes())
    info |= {
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    archived_info_path.write_bytes(plistlib.dumps(info))

    result = run(
        "archive.sh",
        "--version",
        "1.2.3",
        "--build",
        "42",
        "--output",
        root / "output",
        "--signing-config",
        config,
        "--channel",
        "testflight-production",
    )

    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    invocation = next(call["args"] for call in calls if "archive" in call["args"])
    assert not any(
        "DROVER_ALLOW_ARBITRARY_LOADS" in argument for argument in invocation
    )
    record = json.loads((root / "output" / "archive-record.json").read_text())
    assert record["channel"] == "testflight-production"


def test_archive_consumes_complete_xcode_version_output(chain):
    root, run, _, _, config = chain
    result = run(
        "archive.sh",
        "--version",
        "1.2.3",
        "--build",
        "42",
        "--output",
        root / "output",
        "--signing-config",
        config,
        XCODE_VERSION_STRESS="1",
    )
    assert result.returncode == 0, result.stderr


def test_archive_prints_a_redacted_widget_signing_failure_summary(chain):
    root, run, _, _, config = chain

    result = run(
        "archive.sh",
        "--version",
        "1.2.3",
        "--build",
        "42",
        "--output",
        root / "output",
        "--signing-config",
        config,
        ARCHIVE_SIGNING_ERROR="1",
    )

    assert result.returncode != 0
    assert "archive failed; sanitized Xcode summary:" in result.stderr
    assert "Drover Widget AppStore" in result.stderr
    assert "DroverWidget" in result.stderr
    assert "com.apple.security.application-groups" in result.stderr
    assert WIDGET_ISSUER not in result.stderr
    assert "Private Organization" not in result.stderr


@pytest.mark.parametrize("count", ["0", "2"])
def test_export_rejects_nonunique_ipa(chain, count):
    root, _, export, _, _ = chain
    result = export(IPA_COUNT=count)
    assert result.returncode != 0
    assert "exactly one IPA" in result.stderr
    assert not (root / "export" / "export-record.json").exists()


def test_export_rejects_existing_output(chain):
    root, _, export, _, _ = chain
    (root / "export").mkdir()
    result = export()
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("bad", ["ats", "signature"])
def test_export_verifies_unpacked_signed_app(chain, bad):
    root, _, export, _, _ = chain
    extra = {}
    if bad == "ats":
        path = root / "fixture" / "Drover.app" / "Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["NSAppTransportSecurity"] = {"NSAllowsArbitraryLoads": True}
        path.write_bytes(plistlib.dumps(info))
    else:
        extra["CODESIGN_EXIT"] = "1"
    result = export(**extra)
    assert result.returncode != 0
    assert "verification failed" in result.stderr
    assert not (root / "export" / "export-record.json").exists()


@pytest.mark.parametrize("generate", [False, True])
def test_export_emits_only_sanitized_ipa_evidence(chain, generate):
    root, _, export, _, _ = chain
    options = root / "private" / "ExportOptions.plist"
    if generate:
        options.unlink()
    result = export()
    assert result.returncode == 0, result.stderr
    record = json.loads((root / "export" / "export-record.json").read_text())
    assert record == {
        "version": "1.2.3",
        "build": "42",
        "bundle_identifier": "com.arnab.drover",
        "ipa_sha256": hashlib.sha256(b"synthetic IPA bytes").hexdigest(),
    }
    settings = plistlib.loads(options.read_bytes())
    assert settings["method"] == "app-store-connect"
    assert settings["destination"] == "export"
    assert settings["testFlightInternalTestingOnly"] is True
    assert settings["manageAppVersionAndBuildNumber"] is False
    assert settings["provisioningProfiles"]["com.arnab.drover.widget"] == WIDGET_ISSUER
    assert not (options.stat().st_mode & 0o077)
    assert list((root / "scratch").iterdir()) == []
    assert not list((root / "private").glob("drover-ipa-*"))
    assert HUB not in result.stdout + result.stderr


def test_export_verifies_local_networking_production_ipa(chain):
    root, run, _, _, _ = chain
    for info_path in (
        root / "fixture/Drover.app/Info.plist",
        root / "Drover.xcarchive/Products/Applications/Drover.app/Info.plist",
    ):
        info = plistlib.loads(info_path.read_bytes())
        info |= {
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        }
        info_path.write_bytes(plistlib.dumps(info))

    result = run(
        "export_ipa.sh",
        "--archive",
        root / "Drover.xcarchive",
        "--output",
        root / "export",
        "--export-options",
        root / "private/ExportOptions.plist",
        "--version",
        "1.2.3",
        "--build",
        "42",
    )

    assert result.returncode == 0, result.stderr
    record = json.loads((root / "export/export-record.json").read_text())
    assert record["bundle_identifier"] == "com.arnab.drover"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("destination", "upload"),
        ("signingStyle", "automatic"),
        ("method", "debugging"),
        ("testFlightInternalTestingOnly", False),
        ("manageAppVersionAndBuildNumber", True),
    ],
)
def test_export_rejects_options_that_change_distribution_scope(chain, setting, value):
    root, _, export, _, _ = chain
    options = root / "private" / "ExportOptions.plist"
    content = plistlib.loads(options.read_bytes())
    content[setting] = value
    options.write_bytes(plistlib.dumps(content))
    result = export()
    assert result.returncode != 0
    calls = (root / "calls.jsonl").read_text()
    assert "-exportArchive" not in calls


@pytest.mark.parametrize("target", ["directory", "file", "symlink"])
def test_export_rejects_nonprivate_options(chain, target):
    root, _, export, _, _ = chain
    options = root / "private" / "ExportOptions.plist"
    if target == "directory":
        options.parent.chmod(0o755)
    elif target == "file":
        options.chmod(0o644)
    else:
        moved = options.with_name("real.plist")
        options.rename(moved)
        options.symlink_to(moved)
    result = export()
    assert result.returncode != 0
    assert not (root / "calls.jsonl").exists()
    assert HUB not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "field", ["teamID", "signingCertificate", "provisioningProfiles"]
)
def test_export_requires_explicit_signing_references_in_reviewed_options(chain, field):
    root, _, export, _, _ = chain
    options = root / "private" / "ExportOptions.plist"
    content = plistlib.loads(options.read_bytes())
    content.pop(field)
    options.write_bytes(plistlib.dumps(content))
    result = export()
    assert result.returncode != 0
    assert "-exportArchive" not in (root / "calls.jsonl").read_text()


@pytest.mark.parametrize(
    "bad",
    ["missing-key", "directory-mode", "key-mode", "key-symlink", "existing-record"],
)
def test_upload_rejects_unsafe_private_material_or_receipt(chain, bad):
    root, _, _, upload, _ = chain
    key = root / "private" / f"AuthKey_{KEY_ID}.p8"
    if bad == "missing-key":
        key.unlink()
    elif bad == "directory-mode":
        key.parent.chmod(0o755)
    elif bad == "key-mode":
        key.chmod(0o644)
    elif bad == "key-symlink":
        key.unlink()
        key.symlink_to(root / "fixture.ipa")
    else:
        (root / "upload-record.json").write_text("preserve")
    result = upload()
    assert result.returncode != 0
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"UPLOAD_EXIT": "1"},
        {"UPLOAD_RESPONSE": '{"product-errors":[{"message":"failed"}]}'},
        {"UPLOAD_RESPONSE": '{"success-message":null}'},
    ],
)
def test_upload_requires_positive_confirmation_and_discards_raw_errors(chain, extra):
    root, _, _, upload, _ = chain
    result = upload(**extra)
    assert result.returncode != 0
    record = json.loads((root / "upload-record.json").read_text())
    assert record["upload_confirmed"] is False
    assert record["ipa_sha256"] == hashlib.sha256(b"synthetic IPA bytes").hexdigest()
    assert set(record) == {
        "diagnostic_codes",
        "diagnostic_messages",
        "stage",
        "ipa_sha256",
        "tool_exit_code",
        "upload_confirmed",
    }
    assert "RAW-UPLOAD-RESPONSE" not in result.stdout + result.stderr
    assert "RAW-UPLOAD-RESPONSE" not in json.dumps(record)
    assert list((root / "scratch").iterdir()) == []


def test_upload_failure_receipt_preserves_nested_apple_messages(chain):
    root, _, _, upload, _ = chain
    result = upload(
        UPLOAD_EXIT="1",
        UPLOAD_RESPONSE=json.dumps(
            {
                "product-errors": [
                    {
                        "code": -19237,
                        "message": f"Apple rejected key {KEY_ID} issuer {ISSUER}",
                        "userInfo": {
                            "original_server_error": {
                                "code": "STATE_ERROR.VALIDATION_ERROR.90161",
                                "status": "409",
                                "detail": f"Apple rejected key {KEY_ID} issuer {ISSUER}",
                            }
                        },
                    }
                ]
            }
        ),
    )
    assert result.returncode != 0
    record_text = (root / "upload-record.json").read_text()
    assert json.loads(record_text) == {
        "diagnostic_codes": [
            "-19237",
            "409",
            "STATE_ERROR.VALIDATION_ERROR.90161",
        ],
        "diagnostic_messages": ["Apple rejected key [REDACTED] issuer [REDACTED]"],
        "stage": "upload",
        "ipa_sha256": hashlib.sha256(b"synthetic IPA bytes").hexdigest(),
        "tool_exit_code": 1,
        "upload_confirmed": False,
    }
    assert KEY_ID not in record_text + result.stdout + result.stderr
    assert "RAW-UPLOAD-RESPONSE" not in record_text + result.stdout + result.stderr


def test_upload_failure_receipt_extracts_only_structured_stderr_codes(chain):
    root, _, _, upload, _ = chain
    result = upload(
        UPLOAD_EXIT="1",
        UPLOAD_STDERR=(
            "Error Domain=ITunesConnectionOperationErrorDomain Code=-19237 "
            f'key {KEY_ID} issuer {ISSUER} statusCode = 401 "code":"ITMS-90161"'
        ),
    )
    assert result.returncode != 0
    record_text = (root / "upload-record.json").read_text()
    record = json.loads(record_text)
    assert record["diagnostic_codes"] == ["-19237", "401", "ITMS-90161"]
    assert KEY_ID not in record_text + result.stdout + result.stderr
    assert "RAW-UPLOAD-RESPONSE" not in record_text + result.stdout + result.stderr


def test_upload_uses_key_directory_and_writes_fixed_sanitized_receipt(chain):
    root, _, _, upload, _ = chain
    result = upload()
    assert result.returncode == 0, result.stderr
    record_text = (root / "upload-record.json").read_text()
    record = json.loads(record_text)
    assert record == {
        "upload_confirmed": True,
        "ipa_sha256": hashlib.sha256(b"synthetic IPA bytes").hexdigest(),
    }
    for secret in ("SYNTHETIC-NOT-A-PRIVATE-KEY", ISSUER, HUB, "RAW-UPLOAD-RESPONSE"):
        assert secret not in record_text + result.stdout + result.stderr
    call = json.loads((root / "calls.jsonl").read_text().splitlines()[-1])
    assert call["keys"] == str(root / "private")
    assert call["args"] == [
        "altool",
        "--upload-app",
        "-f",
        str(root / "fixture.ipa"),
        "--api-key",
        KEY_ID,
        "--api-issuer",
        ISSUER,
        "--output-format",
        "json",
    ]
    assert list((root / "scratch").iterdir()) == []


def test_upload_pins_first_key_search_location_before_home_fallbacks(chain):
    root, _, _, upload, _ = chain
    result = upload()
    assert result.returncode == 0, result.stderr
    call = json.loads((root / "calls.jsonl").read_text().splitlines()[-1])
    assert call["local_keys"] is True


@pytest.mark.parametrize(
    "message",
    [
        "No errors uploading '{ipa}'.",
        "No errors uploading archive",
        "No errors uploading '{name}'.",
    ],
)
def test_upload_accepts_every_altool_confirmation_wording(chain, message):
    """altool has punctuated this line differently across Xcode releases.

    Rejecting an unrecognised wording fails the run *after* the bytes are at
    Apple, writes no receipt, and invites a retry that becomes a second build.
    """
    root, _, _, upload, _ = chain
    result = upload(
        UPLOAD_RESPONSE=json.dumps(
            {
                "success-message": message.format(
                    ipa=root / "fixture.ipa", name="fixture.ipa"
                )
            }
        )
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((root / "upload-record.json").read_text())["upload_confirmed"]
        is True
    )


def test_upload_still_rejects_an_unrelated_success_message(chain):
    root, _, _, upload, _ = chain
    result = upload(UPLOAD_RESPONSE=json.dumps({"success-message": "Uploaded 1 file."}))
    assert result.returncode != 0
    record = json.loads((root / "upload-record.json").read_text())
    assert record["upload_confirmed"] is False
    assert record["diagnostic_codes"] == []


def test_upload_accepts_xcode_26_6_named_file_confirmation(chain):
    root, _, _, upload, _ = chain
    result = upload(
        UPLOAD_RESPONSE=json.dumps(
            {"success-message": f"No errors uploading '{root / 'fixture.ipa'}'"}
        )
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((root / "upload-record.json").read_text())["upload_confirmed"]
        is True
    )


@pytest.mark.parametrize("source", ["json", "stderr", "malformed-json"])
def test_rejection_keeps_itms_message_key_and_bundle_without_secrets(chain, source):
    root, _, _, upload, _ = chain
    message = (
        "ERROR ITMS-90360: Missing Info.plist value. CFBundleDisplayName is missing "
        "in bundle com.arnab.drover.widget (Drover.app/PlugIns/DroverWidget.appex). "
        f"key={KEY_ID} issuer={ISSUER} token=short-secret "
        f"Bearer short-bearer {Path.home()}/private/AuthKey.p8 "
        "/Users/runner/private/AuthKey.p8 "
        "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJzZWNyZXQifQ.signature "
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    )
    extra = {"UPLOAD_EXIT": "1"}
    if source == "json":
        extra["UPLOAD_RESPONSE"] = json.dumps(
            {"product-errors": [{"code": 90360, "message": message}]}
        )
    else:
        # Keep PEM on one line here: stderr is collected line by line.
        extra["UPLOAD_STDERR"] = message.replace("\n", " ")
        if source == "malformed-json":
            # The fake tool echoes this as a value, so use its raw-output mode.
            extra["UPLOAD_RAW"] = message
            extra.pop("UPLOAD_STDERR")
    result = upload(**extra)
    assert result.returncode != 0
    receipt = json.loads((root / "upload-record.json").read_text())
    evidence = json.dumps(receipt) + result.stderr + result.stdout
    for expected in (
        "ITMS-90360",
        "Missing Info.plist value",
        "CFBundleDisplayName",
        "com.arnab.drover.widget",
    ):
        assert expected in evidence
    for secret in (
        KEY_ID,
        ISSUER,
        "short-secret",
        "short-bearer",
        "private-material",
        "AuthKey.p8",
        str(Path.home()),
        "/Users/runner",
        "eyJhbG",
    ):
        assert secret not in evidence
    assert list((root / "scratch").iterdir()) == []


def test_validation_failure_prevents_upload_and_retains_diagnostics(chain):
    root, _, _, upload, _ = chain
    result = upload(
        "--validate",
        VALIDATE_EXIT="1",
        VALIDATE_RESPONSE=json.dumps(
            {
                "product-errors": [
                    {
                        "code": 90360,
                        "message": "ITMS-90360: Missing Info.plist value CFBundleDisplayName in com.arnab.drover.widget",
                    }
                ]
            }
        ),
    )
    assert result.returncode != 0
    record = json.loads((root / "upload-record.json").read_text())
    assert record["stage"] == "validation"
    assert record["upload_confirmed"] is False
    assert "CFBundleDisplayName" in record["diagnostic_messages"][0]
    calls = [
        json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 1
    assert "--validate-app" in calls[0]["args"]
    assert "--upload-app" not in calls[0]["args"]


def test_validation_runs_before_upload(chain):
    root, _, _, upload, _ = chain
    result = upload("--validate")
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2
    assert "--validate-app" in calls[0]["args"]
    assert "--upload-app" in calls[1]["args"]


@pytest.mark.parametrize(
    "key",
    [
        "CFBundleDisplayName",
        "CFBundleShortVersionString",
        "CFBundleVersion",
        "MinimumOSVersion",
    ],
)
@pytest.mark.parametrize("value", [None, "", "$(UNRESOLVED)", "mismatch"])
def test_widget_ipa_rejects_missing_or_mismatched_metadata(chain, key, value):
    root, run, _, _, _ = chain
    path = root / "fixture/Drover.app/PlugIns/DroverWidget.appex/Info.plist"
    info = plistlib.loads(path.read_bytes())
    if value is None:
        info.pop(key)
    else:
        info[key] = value
    path.write_bytes(plistlib.dumps(info))
    result = run(
        "verify_widget_ipa.sh",
        "--ipa",
        root / "fixture.ipa",
        "--expected-profile-uuid",
        WIDGET_ISSUER,
    )
    assert result.returncode != 0
    assert key in result.stderr


def test_widget_ipa_accepts_matching_metadata(chain):
    root, run, _, _, _ = chain
    result = run(
        "verify_widget_ipa.sh",
        "--ipa",
        root / "fixture.ipa",
        "--expected-profile-uuid",
        WIDGET_ISSUER,
    )
    assert result.returncode == 0, result.stderr
    assert "metadata verified" in result.stdout


def test_widget_ipa_rejects_missing_app_icon(chain):
    root, run, _, _, _ = chain
    path = root / "fixture/Drover.app/Info.plist"
    info = plistlib.loads(path.read_bytes())
    info.pop("CFBundleIcons")
    path.write_bytes(plistlib.dumps(info))
    result = run(
        "verify_widget_ipa.sh",
        "--ipa",
        root / "fixture.ipa",
        "--expected-profile-uuid",
        WIDGET_ISSUER,
    )
    assert result.returncode != 0
    assert "AppIcon" in result.stderr


def test_apple_messages_are_bounded_and_redact_environment_credentials(monkeypatch):
    from ios.apple_diagnostics import diagnostic_messages

    monkeypatch.setenv("DROVER_TESTFLIGHT_HUB_TOKEN", "private-short-token")
    messages = diagnostic_messages(
        {
            "product-errors": [
                {"message": f"ITMS-90360: error {i} " + "x " * 3000} for i in range(30)
            ]
        },
        "",
    )
    assert len(messages) == 20
    assert all(len(message) <= 4096 for message in messages)
    messages = diagnostic_messages(
        {
            "message": f'ITMS-90360: CFBundleDisplayName com.arnab.drover.widget private-short-token "{Path.home()}/Private Folder/key.p8"'
        },
        "",
    )
    assert messages == [
        "ITMS-90360: CFBundleDisplayName com.arnab.drover.widget [REDACTED] [REDACTED_PATH]"
    ]


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("xcodegen") is None,
    reason="requires macOS Xcode and XcodeGen for real plist processing",
)
@pytest.mark.parametrize(
    "configuration,ats_mode,expected_key",
    [
        ("StoreRelease", "local", "NSAllowsLocalNetworking"),
        ("Release", "local", "NSAllowsLocalNetworking"),
        ("Debug", "arbitrary", "NSAllowsArbitraryLoads"),
    ],
)
def test_generated_project_processes_typed_ats_metadata(
    tmp_path: Path,
    configuration: str,
    ats_mode: str,
    expected_key: str,
):
    """Process production metadata in a tiny unsigned app with the real Xcode tools."""
    project = SCRIPTS.parents[1] / "apps" / "drover" / "project.yml"
    spec = yaml.safe_load(project.read_text())
    target = spec["targets"]["Drover"]
    # Preserve production settings, metadata and build phases. Package and UI
    # compilation are irrelevant to ProcessInfoPlistFile, so use a tiny C app.
    target["dependencies"] = []
    spec["targets"] = {"Drover": target}
    spec["packages"] = {}
    spec["schemes"] = {"DroverAppStore": spec["schemes"]["DroverAppStore"]}
    source = tmp_path / "Drover"
    source.mkdir()
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    (tmp_path / "project.yml").write_text(yaml.safe_dump(spec))
    generated = subprocess.run(
        ["xcodegen", "generate", "--spec", str(tmp_path / "project.yml")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    result = subprocess.run(
        [
            "xcodebuild",
            "-project",
            str(tmp_path / "Drover.xcodeproj"),
            "-scheme",
            "DroverAppStore",
            "-configuration",
            configuration,
            "-sdk",
            "iphonesimulator",
            "-destination",
            "generic/platform=iOS Simulator",
            "-derivedDataPath",
            str(tmp_path / "derived"),
            "CODE_SIGNING_ALLOWED=NO",
            f"DROVER_ATS_MODE={ats_mode}",
            "build",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    processed = (
        tmp_path
        / "derived"
        / "Build"
        / "Products"
        / f"{configuration}-iphonesimulator"
        / "Drover.app"
        / "Info.plist"
    )
    info = plistlib.loads(processed.read_bytes())
    assert info["CFBundleDisplayName"] == "Drover"
    assert set(info["UISupportedInterfaceOrientations"]) == {
        "UIInterfaceOrientationPortrait",
        "UIInterfaceOrientationPortraitUpsideDown",
        "UIInterfaceOrientationLandscapeLeft",
        "UIInterfaceOrientationLandscapeRight",
    }
    assert info["NSAppTransportSecurity"] == {expected_key: True}
    template = plistlib.loads((source / "Info.plist").read_bytes())
    assert template["NSAppTransportSecurity"]["NSAllowsArbitraryLoads"] is True
