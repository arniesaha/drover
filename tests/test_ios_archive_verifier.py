"""Contracts for verifying a signed iOS distribution artifact."""

import importlib.util
import os
import plistlib
import stat
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


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def write_signing_config(directory: Path, *, keychain: Path) -> Path:
    keychain.parent.mkdir(parents=True, exist_ok=True)
    keychain.touch()
    config = directory / "signing.xcconfig"
    config.write_text(
        "CODE_SIGN_STYLE = Manual\n"
        "DEVELOPMENT_TEAM = TEAMID1234\n"
        "CODE_SIGN_IDENTITY = Apple Distribution: Example Organization (TEAMID1234)\n"
        "PROVISIONING_PROFILE_SPECIFIER = 11111111-2222-3333-4444-555555555555\n"
        f'OTHER_CODE_SIGN_FLAGS = --keychain "{keychain}"\n',
        encoding="utf-8",
    )
    return config


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


def test_verify_app_parses_entitlements_from_stdout_without_stderr_diagnostics(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()
    evidence = plistlib.dumps(signed_entitlements()).decode()

    def stdout_entitlements_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:2] == ["--verify"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:2] == ["-dvv"]:
            return subprocess.CompletedProcess(
                command, 0, "", "Authority=Apple Distribution: Example (TEAMID)\n"
            )
        if command[1:3] == ["-d", "--entitlements"]:
            return subprocess.CompletedProcess(
                command, 0, evidence, "Executable=/tmp/Drover.app/Drover\n"
            )
        raise AssertionError(f"unexpected codesign command: {command}")

    identity = verifier.verify_app(
        write_bundle(tmp_path),
        expected_version="1.2.3",
        expected_build="42",
        sdk_floor="26.0",
        run=stdout_entitlements_runner,
    )

    assert identity.bundle_identifier == "com.arnab.drover"


def test_verify_app_accepts_legacy_stderr_entitlements_after_diagnostics(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()
    evidence = plistlib.dumps(signed_entitlements()).decode()

    def stderr_entitlements_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:2] == ["--verify"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:2] == ["-dvv"]:
            return subprocess.CompletedProcess(
                command, 0, "", "Authority=Apple Distribution: Example (TEAMID)\n"
            )
        if command[1:3] == ["-d", "--entitlements"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "Executable=/tmp/Drover.app/Drover\n" + evidence,
            )
        raise AssertionError(f"unexpected codesign command: {command}")

    identity = verifier.verify_app(
        write_bundle(tmp_path),
        expected_version="1.2.3",
        expected_build="42",
        sdk_floor="26.0",
        run=stderr_entitlements_runner,
    )

    assert identity.bundle_identifier == "com.arnab.drover"


def test_verify_app_rejects_a_malformed_selected_entitlement_stream(
    tmp_path: Path,
) -> None:
    verifier = load_verifier()
    fallback_evidence = plistlib.dumps(signed_entitlements()).decode()

    def malformed_stdout_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:2] == ["--verify"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:2] == ["-dvv"]:
            return subprocess.CompletedProcess(
                command, 0, "", "Authority=Apple Distribution: Example (TEAMID)\n"
            )
        if command[1:3] == ["-d", "--entitlements"]:
            return subprocess.CompletedProcess(
                command, 0, "<?xml malformed", fallback_evidence
            )
        raise AssertionError(f"unexpected codesign command: {command}")

    with pytest.raises(verifier.ArtifactVerificationError, match="malformed"):
        verifier.verify_app(
            write_bundle(tmp_path),
            expected_version="1.2.3",
            expected_build="42",
            sdk_floor="26.0",
            run=malformed_stdout_runner,
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS codesign")
def test_inspect_signing_parses_a_preexisting_system_app() -> None:
    verifier = load_verifier()
    calculator = Path("/System/Applications/Calculator.app")
    if not calculator.is_dir():
        pytest.skip("Calculator.app is not installed")

    evidence = verifier.inspect_signing(calculator)

    assert isinstance(evidence.entitlements, dict)
    assert evidence.entitlements


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


def test_archive_wrapper_requires_a_private_explicit_signing_configuration() -> None:
    script = ARCHIVE_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "--signing-config" in script
    assert "CODE_SIGN_STYLE = Manual" in script
    assert '"CODE_SIGN_IDENTITY = "' in script
    assert "Apple Distribution: " in script
    assert '-xcconfig "$SIGNING_CONFIG"' in script
    assert "CODE_SIGN_IDENTITY=" not in script
    assert "-allowProvisioningUpdates" not in script


def test_archive_wrapper_rejects_an_unquoted_keychain_option_path(
    tmp_path: Path,
) -> None:
    keychain = tmp_path / "temporary keychain" / "distribution.keychain-db"
    signing_config = write_signing_config(tmp_path, keychain=keychain)
    signing_config.write_text(
        signing_config.read_text(encoding="utf-8").replace(
            f'--keychain "{keychain}"', f"--keychain {keychain}"
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(ARCHIVE_SCRIPT_PATH),
            "--version",
            "1.2.3",
            "--build",
            "42",
            "--output",
            str(tmp_path / "candidate"),
            "--signing-config",
            str(signing_config),
        ],
        cwd=ARCHIVE_SCRIPT_PATH.parents[2],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "signing configuration is invalid" in result.stderr


def test_archive_wrapper_uses_environment_selected_xcode_with_portable_output(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    xcodebuild_log = tmp_path / "xcodebuild-log"
    xcode_select_log = tmp_path / "xcode-select-log"
    record_args = tmp_path / "record-args"
    effective_developer_dir = tmp_path / "selected-xcode"
    effective_developer_dir.mkdir()
    signing_config = write_signing_config(
        tmp_path,
        keychain=tmp_path / "temporary keychain" / "distribution.keychain-db",
    )
    output = tmp_path / "portable-output"

    write_executable(
        fake_bin / "xcodebuild",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-version" ]]; then\n'
        "  printf 'Xcode 26.6\\nBuild version test\\n'\n"
        "  exit 0\n"
        "fi\n"
        'printf \'%s\\n\' "$DEVELOPER_DIR" >> "$STUB_XCODEBUILD_LOG"\n'
        'archive_path=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "-archivePath" ]]; then archive_path="$2"; fi\n'
        "  shift\n"
        "done\n"
        'mkdir -p "$archive_path"\n',
    )
    write_executable(fake_bin / "xcrun", "#!/usr/bin/env bash\nprintf '26.5\\n'\n")
    write_executable(fake_bin / "xcodegen", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        fake_bin / "xcode-select",
        "#!/usr/bin/env bash\nprintf 'called\\n' >> \"$STUB_XCODE_SELECT_LOG\"\nexit 99\n",
    )
    write_executable(
        fake_bin / "git",
        "#!/usr/bin/env bash\n"
        'if [[ "$3" == "rev-parse" ]]; then printf \'%040d\\n\' 0; fi\n',
    )
    write_executable(
        fake_bin / "ditto",
        "#!/usr/bin/env bash\n"
        'last=""\nfor argument in "$@"; do last="$argument"; done\n: > "$last"\n',
    )
    write_executable(
        fake_bin / "shasum",
        "#!/usr/bin/env bash\nprintf '%064d  candidate\\n' 0\n",
    )
    write_executable(
        fake_bin / "python3",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-" ]]; then\n'
        '  printf \'%s\\n\' "$@" > "$STUB_RECORD_ARGS"\n'
        "fi\n",
    )

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
            "--signing-config",
            str(signing_config),
        ],
        cwd=ARCHIVE_SCRIPT_PATH.parents[2],
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEVELOPER_DIR": str(effective_developer_dir),
            "STUB_XCODEBUILD_LOG": str(xcodebuild_log),
            "STUB_XCODE_SELECT_LOG": str(xcode_select_log),
            "STUB_RECORD_ARGS": str(record_args),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert xcodebuild_log.read_text(encoding="utf-8").splitlines() == [
        str(effective_developer_dir)
    ]
    assert not xcode_select_log.exists()
    assert str(effective_developer_dir) in record_args.read_text(encoding="utf-8")


def test_distribution_docs_describe_signed_artifact_validation() -> None:
    documentation = (
        Path(__file__).parents[1] / "apps" / "drover" / "docs" / "distribution.md"
    ).read_text(encoding="utf-8")

    assert "scripts/ios/archive.sh --version" in documentation
    assert "scripts/ios/verify_archive.py --app" in documentation
    assert "signed entitlements" in documentation
    assert "get-task-allow" in documentation
    assert "ios-distribution" in documentation
    assert "--signing-config" in documentation
    assert "sanitized metadata" in documentation
    assert "not uploaded" in documentation
