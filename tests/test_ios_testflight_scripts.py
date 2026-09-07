"""Exercise the artifact chain with synthetic bundles and fake Apple binaries."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
from test_ios_archive_verifier import (
    valid_info,
    write_bundle,
    write_executable,
    write_signing_config,
)

SCRIPTS = Path(__file__).parents[1] / "scripts" / "ios"
STAGE = "https://stage.example.test"
ISSUER = "11111111-2222-3333-4444-555555555555"
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
        "DROVER_TESTFLIGHT_STAGE_ONLY": "YES",
        "DROVER_TESTFLIGHT_STAGING_URL": STAGE,
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    }
    app = write_bundle(tmp_path / "fixture", info=info)
    (app / "embedded.mobileprovision").write_bytes(b"synthetic profile fixture")
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
                "provisioningProfiles": {"com.arnab.drover": ISSUER},
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
        print("Xcode 26.6\\nBuild version synthetic")
    elif "-exportArchive" in args:
        dest = pathlib.Path(args[args.index("-exportPath") + 1])
        for i in range(int(os.getenv("IPA_COUNT", "1"))):
            (dest / f"Drover{i}.ipa").write_bytes(b"synthetic IPA bytes")
        print("private Xcode output " + os.environ["LEAK_SENTINEL"])
    else:
        dest = pathlib.Path(args[args.index("-archivePath") + 1])
        shutil.copytree(root / "Drover.xcarchive", dest)
elif name == "xcrun":
    if "--show-sdk-version" in args:
        print("26.5")
    else:
        key = pathlib.Path(os.environ["API_PRIVATE_KEYS_DIR"]) / "AuthKey_EXAMPLE123.p8"
        print(json.dumps({"tool-version": "8.003", "tool-path": "/synthetic/altool", "success-message": "No errors uploading archive.", "product-errors": []} | json.loads(os.getenv("UPLOAD_RESPONSE", "{}")) | {"private": key.read_text() + os.environ["LEAK_SENTINEL"]}))
        sys.exit(int(os.getenv("UPLOAD_EXIT", "0")))
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
        sys.stdout.buffer.write(plistlib.dumps({"application-identifier": "TEAMID1234.com.arnab.drover", "com.apple.developer.team-identifier": "TEAMID1234", "aps-environment": "production", "get-task-allow": False}))
elif name == "security":
    sys.stdout.buffer.write(plistlib.dumps({"UUID": "11111111-2222-3333-4444-555555555555", "TeamIdentifier": ["TEAMID1234"], "Entitlements": {"application-identifier": "TEAMID1234.com.arnab.drover"}}))
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
        "LEAK_SENTINEL": STAGE + ISSUER + "RAW-UPLOAD-RESPONSE",
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
            "--staging-url",
            STAGE,
            *args,
            **extra,
        )

    def upload(**extra: str):
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
            **extra,
        )

    return tmp_path, run, export, upload, config


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://stage.example.test",
        STAGE + "/path",
        STAGE + "?query",
        STAGE + "#fragment",
        "https://user:pass@stage.example.test",
        STAGE + "?",
        STAGE + "#",
        "https://stage.example.test\n",
        "https://stage.example.test:invalid",
    ],
)
def test_archive_rejects_unsafe_or_missing_staging_url(chain, url):
    root, run, _, _, config = chain
    args = [] if url is None else ["--staging-url", url]
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
        "testflight-internal",
        *args,
    )
    assert result.returncode != 0
    assert "staging URL" in result.stderr
    assert not (root / "calls.jsonl").exists()


def test_archive_stage_locks_build_and_hashes_normalized_url(chain):
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
        "--channel",
        "testflight-internal",
        "--staging-url",
        "https://STAGE.example.test:443/",
    )
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    invocation = next(call["args"] for call in calls if "archive" in call["args"])
    assert "DROVER_TESTFLIGHT_STAGE_ONLY=YES" in invocation
    assert f"DROVER_TESTFLIGHT_STAGING_URL={STAGE}" in invocation
    assert "DROVER_ALLOW_ARBITRARY_LOADS=NO" in invocation
    record = json.loads((root / "output" / "archive-record.json").read_text())
    assert record["channel"] == "testflight-internal"
    assert record["staging_url_sha256"] == hashlib.sha256(STAGE.encode()).hexdigest()
    assert STAGE not in json.dumps(record) + result.stdout + result.stderr


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


@pytest.mark.parametrize("bad", ["stage", "signature"])
def test_export_verifies_unpacked_signed_app(chain, bad):
    root, _, export, _, _ = chain
    extra = {}
    if bad == "stage":
        path = root / "fixture" / "Drover.app" / "Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["DROVER_TESTFLIGHT_STAGE_ONLY"] = "NO"
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
        "staging_url_sha256": hashlib.sha256(STAGE.encode()).hexdigest(),
    }
    settings = plistlib.loads(options.read_bytes())
    assert settings["method"] == "app-store-connect"
    assert settings["destination"] == "export"
    assert settings["testFlightInternalTestingOnly"] is True
    assert settings["manageAppVersionAndBuildNumber"] is False
    assert not (options.stat().st_mode & 0o077)
    assert list((root / "scratch").iterdir()) == []
    assert not list((root / "private").glob("drover-ipa-*"))
    assert STAGE not in result.stdout + result.stderr


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
    assert STAGE not in result.stdout + result.stderr


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
    assert not (root / "upload-record.json").exists()
    assert "RAW-UPLOAD-RESPONSE" not in result.stdout + result.stderr
    assert list((root / "scratch").iterdir()) == []


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
    for secret in ("SYNTHETIC-NOT-A-PRIVATE-KEY", ISSUER, STAGE, "RAW-UPLOAD-RESPONSE"):
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
