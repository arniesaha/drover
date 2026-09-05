"""Contracts for verifying a signed iOS distribution artifact."""

import importlib.util
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "ios" / "verify_archive.py"
ARCHIVE_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "ios" / "archive.sh"


def load_verifier():
    if not SCRIPT_PATH.exists():
        pytest.fail("scripts/ios/verify_archive.py has not been created")
    spec = importlib.util.spec_from_file_location("verify_archive", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_info() -> dict[str, str]:
    return {
        "CFBundleIdentifier": "com.arnab.drover",
        "CFBundleShortVersionString": "1.2.3",
        "CFBundleVersion": "42",
        "DTPlatformName": "iphoneos",
        "DTPlatformVersion": "26.5",
        "DTSDKName": "iphoneos26.5",
    }


def valid_manifest() -> dict[str, object]:
    return {
        "NSPrivacyTracking": False,
        "NSPrivacyCollectedDataTypes": [],
        "NSPrivacyAccessedAPITypes": [],
    }


def write_bundle(tmp_path: Path, *, info: dict[str, object] | None = None) -> Path:
    app = tmp_path / "Drover.app"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as handle:
        plistlib.dump(info or valid_info(), handle)
    with (app / "PrivacyInfo.xcprivacy").open("wb") as handle:
        plistlib.dump(valid_manifest(), handle)
    return app


def signed_entitlements(*, aps_environment: str = "production") -> dict[str, object]:
    return {
        "application-identifier": "TEAMID.com.arnab.drover",
        "aps-environment": aps_environment,
        "get-task-allow": False,
    }


def stub_codesign(
    *,
    entitlements: dict[str, object] | None = None,
    authority: str = "Apple Distribution: Example (TEAMID)",
):
    evidence = entitlements or signed_entitlements()

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[1:2] == ["--verify"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:2] == ["-dvv"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                f"Authority={authority}\nTeamIdentifier=TEAMID\n",
            )
        if command[1:3] == ["-d", "--entitlements"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                plistlib.dumps(evidence).decode(),
            )
        raise AssertionError(f"unexpected codesign command: {command}")

    return run


def test_verify_app_accepts_a_signed_iphoneos_candidate(tmp_path: Path) -> None:
    verifier = load_verifier()
    app = write_bundle(tmp_path)

    identity = verifier.verify_app(
        app,
        expected_version="1.2.3",
        expected_build="42",
        sdk_floor="26.0",
        run=stub_codesign(),
    )

    assert identity.bundle_identifier == "com.arnab.drover"
    assert identity.sdk_version == "26.5"


def test_verify_app_invokes_codesign_for_signed_evidence(tmp_path: Path) -> None:
    verifier = load_verifier()
    calls: list[list[str]] = []
    delegate = stub_codesign()

    def recording_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return delegate(command, **kwargs)

    verifier.verify_app(
        write_bundle(tmp_path),
        expected_version="1.2.3",
        expected_build="42",
        sdk_floor="26.0",
        run=recording_runner,
    )

    assert calls == [
        [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(tmp_path / "Drover.app"),
        ],
        ["codesign", "-dvv", str(tmp_path / "Drover.app")],
        ["codesign", "-d", "--entitlements", ":-", str(tmp_path / "Drover.app")],
    ]


def test_verify_app_rejects_development_apns_from_signed_evidence(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()

    with pytest.raises(verifier.ArtifactVerificationError, match="APNs"):
        verifier.verify_app(
            write_bundle(tmp_path),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(
                entitlements=signed_entitlements(aps_environment="development")
            ),
        )


def test_verify_app_rejects_debugger_entitlement_from_signed_evidence(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()
    entitlements = signed_entitlements()
    entitlements["get-task-allow"] = True

    with pytest.raises(verifier.ArtifactVerificationError, match="debugger"):
        verifier.verify_app(
            write_bundle(tmp_path),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(entitlements=entitlements),
        )


def test_verify_app_rejects_a_development_signing_authority(tmp_path: Path) -> None:
    verifier = load_verifier()

    with pytest.raises(verifier.ArtifactVerificationError, match="Distribution"):
        verifier.verify_app(
            write_bundle(tmp_path),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(authority="Apple Development: Example (TEAMID)"),
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("CFBundleIdentifier", "com.example.other", "bundle identifier"),
        ("CFBundleShortVersionString", "1.2.4", "version"),
        ("CFBundleVersion", "43", "build"),
    ],
)
def test_verify_app_rejects_candidate_identity_mismatch(
    tmp_path: Path, field: str, value: str, expected_error: str
) -> None:
    verifier = load_verifier()
    info = valid_info()
    info[field] = value

    with pytest.raises(verifier.ArtifactVerificationError, match=expected_error):
        verifier.verify_app(
            write_bundle(tmp_path, info=info),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_simulator_product(tmp_path: Path) -> None:
    verifier = load_verifier()
    info = valid_info()
    info["DTPlatformName"] = "iphonesimulator"
    info["DTPlatformVersion"] = "26.5"
    info["DTSDKName"] = "iphonesimulator26.5"

    with pytest.raises(verifier.ArtifactVerificationError, match="iPhoneOS"):
        verifier.verify_app(
            write_bundle(tmp_path, info=info),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_missing_privacy_manifest(tmp_path: Path) -> None:
    verifier = load_verifier()
    app = write_bundle(tmp_path)
    (app / "PrivacyInfo.xcprivacy").unlink()

    with pytest.raises(verifier.ArtifactVerificationError, match="privacy manifest"):
        verifier.verify_app(
            app,
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_a_malformed_privacy_manifest(tmp_path: Path) -> None:
    verifier = load_verifier()
    app = write_bundle(tmp_path)
    (app / "PrivacyInfo.xcprivacy").write_text("<?xml malformed", encoding="utf-8")

    with pytest.raises(verifier.ArtifactVerificationError, match="privacy manifest"):
        verifier.verify_app(
            app,
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_an_unsupported_sdk(tmp_path: Path) -> None:
    verifier = load_verifier()
    info = valid_info()
    info["DTPlatformVersion"] = "25.4"
    info["DTSDKName"] = "iphoneos25.4"

    with pytest.raises(verifier.ArtifactVerificationError, match="SDK is below"):
        verifier.verify_app(
            write_bundle(tmp_path, info=info),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_malformed_codesign_entitlements(tmp_path: Path) -> None:
    verifier = load_verifier()
    delegate = stub_codesign()

    def malformed_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["-d", "--entitlements"]:
            return subprocess.CompletedProcess(command, 0, "", "<?xml malformed")
        return delegate(command, **kwargs)

    with pytest.raises(verifier.ArtifactVerificationError, match="malformed"):
        verifier.verify_app(
            write_bundle(tmp_path),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=malformed_runner,
        )


@pytest.mark.parametrize(
    "field",
    ["CFBundleShortVersionString", "CFBundleVersion"],
)
def test_verify_app_rejects_unexpanded_build_settings(
    tmp_path: Path, field: str
) -> None:
    verifier = load_verifier()
    info = valid_info()
    info[field] = "$(MARKETING_VERSION)"

    with pytest.raises(verifier.ArtifactVerificationError, match="unexpanded"):
        verifier.verify_app(
            write_bundle(tmp_path, info=info),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_verify_app_rejects_a_mismatched_export_after_a_valid_archive(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()
    archive = tmp_path / "Drover.xcarchive"
    archived_app = archive / "Products" / "Applications" / "Drover.app"
    archived_app.parent.mkdir(parents=True)
    write_bundle(archived_app.parent)

    verifier.verify_app(
        archive,
        expected_version="1.2.3",
        expected_build="42",
        sdk_floor="26.0",
        run=stub_codesign(),
    )

    exported_info = valid_info()
    exported_info["CFBundleVersion"] = "43"
    exported_app = write_bundle(tmp_path / "Payload", info=exported_info)
    with pytest.raises(verifier.ArtifactVerificationError, match="build"):
        verifier.verify_app(
            exported_app,
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=stub_codesign(),
        )


def test_archive_wrapper_refuses_an_existing_output_before_archiving() -> None:
    if not ARCHIVE_SCRIPT_PATH.exists():
        pytest.fail("scripts/ios/archive.sh has not been created")
    with tempfile.TemporaryDirectory(dir="/Volumes/M2 1") as workspace:
        output = Path(workspace) / "candidate"
        output.mkdir()

        result = subprocess.run(
            [
                "bash",
                str(ARCHIVE_SCRIPT_PATH),
                "--version",
                "1.2.3",
                "--build",
                "42",
                "--output",
                str(output),
            ],
            cwd=ARCHIVE_SCRIPT_PATH.parents[2],
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_archive_wrapper_rejects_unexpanded_values_before_archiving() -> None:
    with tempfile.TemporaryDirectory(dir="/Volumes/M2 1") as workspace:
        output = Path(workspace) / "candidate"

        result = subprocess.run(
            [
                "bash",
                str(ARCHIVE_SCRIPT_PATH),
                "--version",
                "$(MARKETING_VERSION)",
                "--build",
                "42",
                "--output",
                str(output),
            ],
            cwd=ARCHIVE_SCRIPT_PATH.parents[2],
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "expanded numeric" in result.stderr
    assert not output.exists()


def test_archive_wrapper_does_not_select_a_signing_identity() -> None:
    script = ARCHIVE_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "CODE_SIGN_IDENTITY" not in script
    assert "-allowProvisioningUpdates" not in script


def test_distribution_docs_describe_signed_artifact_validation() -> None:
    documentation = (
        Path(__file__).parents[1] / "apps" / "drover" / "docs" / "distribution.md"
    ).read_text(encoding="utf-8")

    assert "scripts/ios/archive.sh --version" in documentation
    assert "scripts/ios/verify_archive.py --app" in documentation
    assert "signed entitlements" in documentation
    assert "get-task-allow" in documentation
    assert "ios-distribution" in documentation
