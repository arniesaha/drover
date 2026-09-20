"""Contracts for isolated, deterministic iOS distribution-signing setup."""

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "ios" / "setup_distribution_signing.sh"
CLEANUP_SCRIPT = ROOT / "scripts" / "ios" / "cleanup_distribution_signing.sh"
PROJECT_SPEC = ROOT / "apps" / "drover" / "project.yml"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_setup_creates_a_private_deterministic_signing_configuration(
    tmp_path: Path,
) -> None:
    if not SETUP_SCRIPT.exists():
        pytest.fail("scripts/ios/setup_distribution_signing.sh has not been created")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    workspace = tmp_path / "workspace with spaces"
    swift_args = tmp_path / "swift-args"
    security_args = tmp_path / "security-args"
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    home = tmp_path / "home"
    login_keychain = home / "Library" / "Keychains" / "login.keychain-db"
    keychain = (
        home
        / "Library"
        / "Keychains"
        / f"drover-distribution-{profile_uuid}.keychain-db"
    )
    login_keychain.parent.mkdir(parents=True)
    login_keychain.touch()
    identity_sha = "A" * 40
    identity_name = "Apple Distribution: Example Organization (TEAMID1234)"
    p12_secret = "base64-private-key-material"
    p12_password = "p12-password-not-for-argv"

    write_executable(
        fake_bin / "base64",
        "#!/usr/bin/env bash\n"
        "while IFS= read -r ignored; do :; done\n"
        "printf 'decoded-test-material'\n",
    )
    write_executable(
        fake_bin / "openssl",
        "#!/usr/bin/env bash\nprintf 'temporary-keychain-password'\n",
    )
    write_executable(
        fake_bin / "plutil",
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{profile_uuid}'\n",
    )
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  list-keychains) if [[ " $* " != *" -s "* ]]; then printf \'    "%s"\\n\' "$HOME/Library/Keychains/login.keychain-db"; fi ;;\n'
        '  default-keychain) if [[ " $* " != *" -s "* ]]; then printf \'    "%s"\\n\' "$HOME/Library/Keychains/login.keychain-db"; fi ;;\n'
        '  find-identity) printf \'1) %s \\"%s\\"\\n\' "$DROVER_DISTRIBUTION_IDENTITY_SHA1" "$DROVER_DISTRIBUTION_IDENTITY_NAME" ;;\n'
        '  set-key-partition-list) printf \'%s\\n\' "$@" > "$STUB_SECURITY_ARGS" ;;\n'
        "  delete-keychain) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    write_executable(
        fake_bin / "swift",
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$STUB_SWIFT_ARGS"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--keychain-path" ]]; then\n'
        '    : > "$2"\n'
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 1\n",
    )

    github_output = tmp_path / "github-output"
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STUB_SWIFT_ARGS": str(swift_args),
        "STUB_SECURITY_ARGS": str(security_args),
        "DROVER_DISTRIBUTION_P12_BASE64": p12_secret,
        "DROVER_DISTRIBUTION_P12_PASSWORD": p12_password,
        "DROVER_DISTRIBUTION_PROFILE_BASE64": "base64-profile-material",
        "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
        "DROVER_DISTRIBUTION_PROFILE_UUID": profile_uuid,
        "DROVER_DISTRIBUTION_IDENTITY_SHA1": identity_sha,
        "DROVER_DISTRIBUTION_IDENTITY_NAME": identity_name,
        "HOME": str(home),
    }

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(workspace),
            "--github-output",
            str(github_output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert p12_secret not in result.stdout + result.stderr
    assert p12_password not in result.stdout + result.stderr
    assert p12_secret not in swift_args.read_text(encoding="utf-8")
    assert p12_password not in swift_args.read_text(encoding="utf-8")
    assert security_args.read_text(encoding="utf-8").splitlines() == [
        "set-key-partition-list",
        "-S",
        "apple:",
        "-l",
        "Imported Private Key",
        "-k",
        "temporary-keychain-password",
        str(keychain),
    ]

    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
    )
    assert Path(outputs["keychain_path"]) == keychain
    signing_config = Path(outputs["signing_config"])
    assert signing_config.read_text(encoding="utf-8") == (
        "DROVER_CODE_SIGN_STYLE = Manual\n"
        "DROVER_DEVELOPMENT_TEAM = TEAMID1234\n"
        f"DROVER_CODE_SIGN_IDENTITY = {identity_name}\n"
        f"DROVER_PROVISIONING_PROFILE_SPECIFIER = {profile_uuid}\n"
        f'DROVER_OTHER_CODE_SIGN_FLAGS = --keychain "{keychain}"\n'
    )
    flags = next(
        line.removeprefix("DROVER_OTHER_CODE_SIGN_FLAGS = ")
        for line in signing_config.read_text(encoding="utf-8").splitlines()
        if line.startswith("DROVER_OTHER_CODE_SIGN_FLAGS = ")
    )
    assert shlex.split(flags) == ["--keychain", str(keychain)]
    assert Path(outputs["profile_path"]).name == f"{profile_uuid}.mobileprovision"
    assert Path(outputs["state_file"]).is_file()
    assert json.loads(
        (workspace / "keychain-state.json").read_text(encoding="utf-8")
    ) == {"default": str(login_keychain), "search": [str(login_keychain)]}


def test_distribution_signing_values_are_mapped_only_by_the_app_target() -> None:
    project = yaml.safe_load(PROJECT_SPEC.read_text(encoding="utf-8"))
    store_release = project["targets"]["Drover"]["settings"]["configs"]["StoreRelease"]
    assert store_release["CODE_SIGN_STYLE"] == "$(DROVER_CODE_SIGN_STYLE)"
    assert store_release["DEVELOPMENT_TEAM"] == "$(DROVER_DEVELOPMENT_TEAM)"
    assert store_release["CODE_SIGN_IDENTITY"] == "$(DROVER_CODE_SIGN_IDENTITY)"
    assert (
        store_release["PROVISIONING_PROFILE_SPECIFIER"]
        == "$(DROVER_PROVISIONING_PROFILE_SPECIFIER)"
    )
    assert store_release["OTHER_CODE_SIGN_FLAGS"] == "$(DROVER_OTHER_CODE_SIGN_FLAGS)"
    for name, target in project["targets"].items():
        if name == "Drover":
            continue
        settings = target.get("settings", {})
        assert "PROVISIONING_PROFILE_SPECIFIER" not in settings.get("base", {})
        assert "PROVISIONING_PROFILE_SPECIFIER" not in settings.get("configs", {}).get(
            "StoreRelease", {}
        )


def test_setup_rejects_an_xcconfig_expansion_in_the_workspace_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace $(untrusted)"

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(workspace),
            "--github-output",
            str(tmp_path / "github-output"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "workspace path contains unsupported characters" in result.stderr
    assert not workspace.exists()


def test_setup_refuses_to_replace_an_existing_provisioning_profile(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    workspace = tmp_path / "workspace"
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    existing_profile = (
        tmp_path
        / "home"
        / "Library"
        / "MobileDevice"
        / "Provisioning Profiles"
        / f"{profile_uuid}.mobileprovision"
    )
    existing_profile.parent.mkdir(parents=True)
    existing_profile.write_text("pre-existing-profile", encoding="utf-8")

    write_executable(
        fake_bin / "base64",
        "#!/usr/bin/env bash\nprintf 'decoded-test-material'\n",
    )
    write_executable(fake_bin / "openssl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        fake_bin / "plutil", f"#!/usr/bin/env bash\nprintf '%s\\n' '{profile_uuid}'\n"
    )
    write_executable(fake_bin / "swift", "#!/usr/bin/env bash\nexit 1\n")
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  cms|delete-keychain) exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(workspace),
            "--github-output",
            str(tmp_path / "github-output"),
        ],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DROVER_DISTRIBUTION_P12_BASE64": "test-p12",
            "DROVER_DISTRIBUTION_P12_PASSWORD": "test-password",
            "DROVER_DISTRIBUTION_PROFILE_BASE64": "test-profile",
            "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
            "DROVER_DISTRIBUTION_PROFILE_UUID": profile_uuid,
            "DROVER_DISTRIBUTION_IDENTITY_SHA1": "A" * 40,
            "DROVER_DISTRIBUTION_IDENTITY_NAME": "Apple Distribution: Example Organization (TEAMID1234)",
            "HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert existing_profile.read_text(encoding="utf-8") == "pre-existing-profile"


def test_setup_sanitizes_profile_decoding_errors(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    diagnostic = "private-profile-diagnostic"
    write_executable(
        fake_bin / "base64",
        "#!/usr/bin/env bash\nprintf 'decoded-test-material'\n",
    )
    write_executable(fake_bin / "openssl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(fake_bin / "plutil", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(fake_bin / "swift", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "cms" ]]; then\n'
        f"  printf '%s\\n' '{diagnostic}' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    profile_uuid = "11111111-2222-3333-4444-555555555555"

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(tmp_path / "workspace"),
            "--github-output",
            str(tmp_path / "github-output"),
        ],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DROVER_DISTRIBUTION_P12_BASE64": "test-p12",
            "DROVER_DISTRIBUTION_P12_PASSWORD": "test-password",
            "DROVER_DISTRIBUTION_PROFILE_BASE64": "test-profile",
            "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
            "DROVER_DISTRIBUTION_PROFILE_UUID": profile_uuid,
            "DROVER_DISTRIBUTION_IDENTITY_SHA1": "A" * 40,
            "DROVER_DISTRIBUTION_IDENTITY_NAME": "Apple Distribution: Example Organization (TEAMID1234)",
            "HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert diagnostic not in result.stdout + result.stderr
    assert "distribution profile could not be decoded" in result.stderr


def test_setup_rejects_an_ambient_distribution_identity(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    unexpected_identity = "Apple Distribution: Unapproved Organization (TEAMID1234)"
    write_executable(
        fake_bin / "base64",
        "#!/usr/bin/env bash\nprintf 'decoded-test-material'\n",
    )
    write_executable(
        fake_bin / "openssl",
        "#!/usr/bin/env bash\nprintf 'temporary-keychain-password'\n",
    )
    write_executable(
        fake_bin / "plutil", f"#!/usr/bin/env bash\nprintf '%s\\n' '{profile_uuid}'\n"
    )
    write_executable(
        fake_bin / "swift",
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--keychain-path" ]]; then : > "$2"; fi\n'
        "  shift\n"
        "done\n",
    )
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  list-keychains) exit 0 ;;\n"
        '  default-keychain) if [[ " $* " != *" -s "* ]]; then printf \'    "%s"\\n\' "$HOME/Library/Keychains/login.keychain-db"; fi ;;\n'
        "  find-identity) printf '1) %040d \"%s\"\\n' 0 'Apple Distribution: Unapproved Organization (TEAMID1234)' ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(tmp_path / "workspace"),
            "--github-output",
            str(tmp_path / "github-output"),
        ],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DROVER_DISTRIBUTION_P12_BASE64": "test-p12",
            "DROVER_DISTRIBUTION_P12_PASSWORD": "test-password",
            "DROVER_DISTRIBUTION_PROFILE_BASE64": "test-profile",
            "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
            "DROVER_DISTRIBUTION_PROFILE_UUID": profile_uuid,
            "DROVER_DISTRIBUTION_IDENTITY_SHA1": "A" * 40,
            "DROVER_DISTRIBUTION_IDENTITY_NAME": "Apple Distribution: Approved Organization (TEAMID1234)",
            "HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "approved distribution identity is unavailable" in result.stderr
    assert unexpected_identity not in result.stdout + result.stderr


def test_setup_failure_removes_keychain_file_when_security_delete_fails(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    home = tmp_path / "home"
    keychain = (
        home
        / "Library"
        / "Keychains"
        / f"drover-distribution-{profile_uuid}.keychain-db"
    )
    profile = (
        home
        / "Library"
        / "MobileDevice"
        / "Provisioning Profiles"
        / f"{profile_uuid}.mobileprovision"
    )
    workspace = tmp_path / "workspace"
    write_executable(
        fake_bin / "base64",
        "#!/usr/bin/env bash\nprintf 'decoded-test-material'\n",
    )
    write_executable(
        fake_bin / "openssl",
        "#!/usr/bin/env bash\nprintf 'temporary-keychain-password'\n",
    )
    write_executable(
        fake_bin / "plutil", f"#!/usr/bin/env bash\nprintf '%s\\n' '{profile_uuid}'\n"
    )
    write_executable(
        fake_bin / "swift",
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--keychain-path" ]]; then mkdir -p "$(dirname "$2")"; : > "$2"; fi\n'
        "  shift\n"
        "done\n",
    )
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  list-keychains) exit 0 ;;\n"
        '  default-keychain) if [[ " $* " != *" -s "* ]]; then printf \'    "%s"\\n\' "$HOME/Library/Keychains/login.keychain-db"; fi ;;\n'
        "  find-identity) printf '1) %040d \"Unapproved\"\\n' 0 ;;\n"
        "  delete-keychain) exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    result = subprocess.run(
        [
            "bash",
            str(SETUP_SCRIPT),
            "--workspace",
            str(workspace),
            "--github-output",
            str(tmp_path / "github-output"),
        ],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DROVER_DISTRIBUTION_P12_BASE64": "test-p12",
            "DROVER_DISTRIBUTION_P12_PASSWORD": "test-password",
            "DROVER_DISTRIBUTION_PROFILE_BASE64": "test-profile",
            "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
            "DROVER_DISTRIBUTION_PROFILE_UUID": profile_uuid,
            "DROVER_DISTRIBUTION_IDENTITY_SHA1": "A" * 40,
            "DROVER_DISTRIBUTION_IDENTITY_NAME": "Apple Distribution: Approved Organization (TEAMID1234)",
            "HOME": str(home),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not keychain.exists()
    assert not profile.exists()
    assert not workspace.exists()


def test_cleanup_removes_only_the_recorded_temporary_signing_material(
    tmp_path: Path,
) -> None:
    if not CLEANUP_SCRIPT.exists():
        pytest.fail("scripts/ios/cleanup_distribution_signing.sh has not been created")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    home = tmp_path / "home"
    keychain = (
        home
        / "Library"
        / "Keychains"
        / f"drover-distribution-{profile_uuid}.keychain-db"
    )
    keychain.parent.mkdir(parents=True)
    signing_config = workspace / "signing.xcconfig"
    state_file = workspace / "signing-state"
    keychain_state = workspace / "keychain-state.json"
    original_keychain = home / "Library" / "Keychains" / "login.keychain-db"
    keychain.touch()
    signing_config.touch()
    profile = (
        tmp_path
        / "home"
        / "Library"
        / "MobileDevice"
        / "Provisioning Profiles"
        / f"{profile_uuid}.mobileprovision"
    )
    profile.parent.mkdir(parents=True)
    profile.touch()
    keychain_state.write_text(
        json.dumps(
            {"default": str(original_keychain), "search": [str(original_keychain)]}
        ),
        encoding="utf-8",
    )
    state_file.write_text(
        "\n".join(
            [
                f"workspace={workspace}",
                f"keychain={keychain}",
                f"profile={profile}",
                f"signing_config={signing_config}",
                f"keychain_state={keychain_state}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    security_log = tmp_path / "security-log"
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >> "$STUB_SECURITY_LOG"\n'
        'if [[ "$1" == "delete-keychain" ]]; then rm -f "$2"; fi\n',
    )

    result = subprocess.run(
        ["bash", str(CLEANUP_SCRIPT), "--state", str(state_file)],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "STUB_SECURITY_LOG": str(security_log),
            "HOME": str(home),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not workspace.exists()
    assert not profile.exists()
    assert security_log.read_text(encoding="utf-8").splitlines() == [
        "delete-keychain",
        str(keychain),
        "default-keychain",
        "-d",
        "user",
        "-s",
        str(original_keychain),
        "list-keychains",
        "-d",
        "user",
        "-s",
        str(original_keychain),
    ]


def _write_cleanup_failure_fixture(tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_uuid = "11111111-2222-3333-4444-555555555555"
    home = tmp_path / "home"
    keychain = (
        home
        / "Library"
        / "Keychains"
        / f"drover-distribution-{profile_uuid}.keychain-db"
    )
    keychain.parent.mkdir(parents=True)
    keychain.touch()
    profile = (
        home
        / "Library"
        / "MobileDevice"
        / "Provisioning Profiles"
        / f"{profile_uuid}.mobileprovision"
    )
    profile.parent.mkdir(parents=True)
    profile.touch()
    signing_config = workspace / "signing.xcconfig"
    signing_config.touch()
    original_keychain = home / "Library" / "Keychains" / "login.keychain-db"
    keychain_state = workspace / "keychain-state.json"
    keychain_state.write_text(
        json.dumps(
            {"default": str(original_keychain), "search": [str(original_keychain)]}
        ),
        encoding="utf-8",
    )
    state_file = workspace / "signing-state"
    state_file.write_text(
        "\n".join(
            [
                f"workspace={workspace}",
                f"keychain={keychain}",
                f"profile={profile}",
                f"signing_config={signing_config}",
                f"keychain_state={keychain_state}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "workspace": workspace,
        "home": home,
        "keychain": keychain,
        "profile": profile,
        "state_file": state_file,
        "keychain_state": keychain_state,
    }


def test_cleanup_removes_keychain_file_when_security_delete_fails(
    tmp_path: Path,
) -> None:
    fixture = _write_cleanup_failure_fixture(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "delete-keychain" ]]; then exit 1; fi\n'
        "exit 0\n",
    )

    result = subprocess.run(
        ["bash", str(CLEANUP_SCRIPT), "--state", str(fixture["state_file"])],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(fixture["home"]),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not fixture["keychain"].exists()
    assert not fixture["profile"].exists()
    assert not fixture["workspace"].exists()


def test_cleanup_removes_credentials_but_retains_recovery_state_when_restore_fails(
    tmp_path: Path,
) -> None:
    fixture = _write_cleanup_failure_fixture(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "security",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "delete-keychain" ]]; then rm -f "$2"; exit 0; fi\n'
        'if [[ "$1" == "default-keychain" && " $* " == *" -s "* ]]; then exit 1; fi\n'
        "exit 0\n",
    )

    result = subprocess.run(
        ["bash", str(CLEANUP_SCRIPT), "--state", str(fixture["state_file"])],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(fixture["home"]),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "could not be restored" in result.stderr
    assert not fixture["keychain"].exists()
    assert not fixture["profile"].exists()
    assert fixture["workspace"].is_dir()
    assert fixture["state_file"].is_file()
    assert fixture["keychain_state"].is_file()
