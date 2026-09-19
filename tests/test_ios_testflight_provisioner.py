"""Contracts for the interactive Internal TestFlight provisioner."""

from __future__ import annotations

import base64
import json
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from ios import provision_testflight as provision


def test_tunnel_files_expose_only_the_isolated_staging_server(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    credentials = tmp_path / "cloudflare" / "tunnel.json"
    credentials.parent.mkdir()
    credentials.write_text("private-credential", encoding="utf-8")
    credentials.chmod(0o600)

    config, job = provision.write_tunnel_files(
        root=root,
        hostname="stage.example.test",
        tunnel_id="11111111-2222-3333-4444-555555555555",
        credentials_file=credentials,
        cloudflared=Path("/opt/homebrew/bin/cloudflared"),
    )

    assert config.stat().st_mode & 0o777 == 0o600
    assert job.stat().st_mode & 0o777 == 0o600
    assert config.read_text(encoding="utf-8") == (
        'tunnel: "11111111-2222-3333-4444-555555555555"\n'
        f'credentials-file: "{credentials}"\n'
        "ingress:\n"
        '  - hostname: "stage.example.test"\n'
        "    service: http://127.0.0.1:17080\n"
        "  - service: http_status:404\n"
    )
    assert "private-credential" not in config.read_text(encoding="utf-8")

    plist = plistlib.loads(job.read_bytes())
    assert plist["Label"] == "com.drover.testflight-tunnel"
    assert plist["ProgramArguments"] == [
        "/usr/bin/env",
        "-i",
        "HOME=" + str(root / "home"),
        "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "--config",
        str(config),
        "run",
    ]
    assert "17081" not in job.read_text(encoding="utf-8")

    repeated = provision.write_tunnel_files(
        root=root,
        hostname="stage.example.test",
        tunnel_id="11111111-2222-3333-4444-555555555555",
        credentials_file=credentials,
        cloudflared=Path("/opt/homebrew/bin/cloudflared"),
    )
    assert repeated == (config, job)


def test_tunnel_credentials_reject_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "actual.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "tunnel.json"
    link.symlink_to(target)

    with pytest.raises(provision.ProvisionError, match="regular file"):
        provision.write_tunnel_files(
            root=tmp_path / "staging",
            hostname="stage.example.test",
            tunnel_id="11111111-2222-3333-4444-555555555555",
            credentials_file=link,
            cloudflared=Path("/opt/homebrew/bin/cloudflared"),
        )


def _distribution_fixture() -> tuple[bytes, str, bytes, dict]:
    password = "private-p12-password"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                "Apple Distribution: Example Organization (TEAMID1234)",
            )
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    p12 = pkcs12.serialize_key_and_certificates(
        b"distribution",
        key,
        certificate,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )
    profile_bytes = b"signed-mobileprovision"
    profile = {
        "UUID": "11111111-2222-3333-4444-555555555555",
        "TeamIdentifier": ["TEAMID1234"],
        "ExpirationDate": now + timedelta(days=30),
        "Entitlements": {
            "application-identifier": "TEAMID1234.com.arnab.drover",
            "com.apple.developer.team-identifier": "TEAMID1234",
            "get-task-allow": False,
            "aps-environment": "production",
        },
        "DeveloperCertificates": [certificate.public_bytes(serialization.Encoding.DER)],
    }
    return p12, password, profile_bytes, profile


def test_distribution_values_are_derived_from_validated_files() -> None:
    p12, password, profile_bytes, profile = _distribution_fixture()

    values = provision.derive_distribution_values(
        p12_bytes=p12,
        p12_password=password,
        profile_bytes=profile_bytes,
        profile=profile,
    )

    assert values == {
        "DROVER_DISTRIBUTION_P12_BASE64": base64.b64encode(p12).decode(),
        "DROVER_DISTRIBUTION_P12_PASSWORD": password,
        "DROVER_DISTRIBUTION_PROFILE_BASE64": base64.b64encode(profile_bytes).decode(),
        "DROVER_DISTRIBUTION_TEAM_ID": "TEAMID1234",
        "DROVER_DISTRIBUTION_PROFILE_UUID": ("11111111-2222-3333-4444-555555555555"),
        "DROVER_DISTRIBUTION_IDENTITY_SHA1": (
            pkcs12.load_key_and_certificates(p12, password.encode())[1]
            .fingerprint(hashes.SHA1())
            .hex()
            .upper()
        ),
        "DROVER_DISTRIBUTION_IDENTITY_NAME": (
            "Apple Distribution: Example Organization (TEAMID1234)"
        ),
    }


def test_distribution_values_require_a_nonempty_p12_password() -> None:
    p12, _, profile_bytes, profile = _distribution_fixture()

    with pytest.raises(provision.ProvisionError, match="password is required"):
        provision.derive_distribution_values(
            p12_bytes=p12,
            p12_password="",
            profile_bytes=profile_bytes,
            profile=profile,
        )


def test_distribution_files_are_privately_read_and_cms_decoded(tmp_path: Path) -> None:
    p12, password, profile_bytes, profile = _distribution_fixture()
    p12_path = tmp_path / "distribution.p12"
    profile_path = tmp_path / "distribution.mobileprovision"
    p12_path.write_bytes(p12)
    profile_path.write_bytes(profile_bytes)
    p12_path.chmod(0o600)
    profile_path.chmod(0o600)
    runner = RecordingRunner()
    original_run = runner.run

    def run(arguments, **kwargs):
        if arguments[:3] == ["security", "cms", "-D"]:
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(
                arguments, 0, plistlib.dumps(profile).decode(), ""
            )
        return original_run(arguments, **kwargs)

    runner.run = run
    values = provision.load_distribution_values(
        runner=runner,
        p12_path=p12_path,
        p12_password=password,
        profile_path=profile_path,
    )

    assert values["DROVER_DISTRIBUTION_TEAM_ID"] == "TEAMID1234"
    assert ["security", "cms", "-D", "-i", str(profile_path)] in [
        call for call, _ in runner.calls
    ]


def test_private_material_rejects_a_symlink_even_to_an_owner_only_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "actual.p8"
    target.write_text("private", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "AuthKey_EXAMPLE123.p8"
    link.symlink_to(target)

    with pytest.raises(provision.ProvisionError, match="owner-only regular file"):
        provision._read_private_input(link, "App Store Connect private key")


def test_distribution_profile_must_include_the_supplied_certificate() -> None:
    p12, password, profile_bytes, profile = _distribution_fixture()
    profile["DeveloperCertificates"] = [b"a different certificate"]

    with pytest.raises(provision.ProvisionError, match="does not include"):
        provision.derive_distribution_values(
            p12_bytes=p12,
            p12_password=password,
            profile_bytes=profile_bytes,
            profile=profile,
        )


def test_appstore_values_require_and_encode_an_es256_private_key() -> None:
    key_bytes = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    values = provision.derive_appstore_values(
        key_bytes=key_bytes,
        key_id="EXAMPLE123",
        issuer_id="11111111-2222-3333-4444-555555555555",
    )

    assert values == {
        "DROVER_APPSTORE_API_KEY_ID": "EXAMPLE123",
        "DROVER_APPSTORE_API_ISSUER_ID": ("11111111-2222-3333-4444-555555555555"),
        "DROVER_APPSTORE_API_PRIVATE_KEY_BASE64": base64.b64encode(key_bytes).decode(),
    }


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        interactive: bool = False,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        self.calls.append((arguments, input_text))
        output = ""
        if arguments[-1].endswith("deployment-branch-policies"):
            output = '{"branch_policies": []}'
        return subprocess.CompletedProcess(arguments, 0, output, "")


def test_github_configuration_streams_secrets_and_protects_main() -> None:
    runner = RecordingRunner()
    distribution = {
        f"DROVER_DISTRIBUTION_{index}": f"signing-{index}" for index in range(7)
    }
    appstore = {
        f"DROVER_APPSTORE_API_{index}": f"appstore-{index}" for index in range(3)
    }

    provision.configure_github(
        runner=runner,
        repository="arniesaha/drover",
        reviewer_id=1234,
        public_origin="https://stage.example.test",
        preflight_token="preflight-private-value",
        distribution_values=distribution,
        appstore_values=appstore,
    )

    environment_payload = {
        "wait_timer": 0,
        "prevent_self_review": False,
        "reviewers": [{"type": "User", "id": 1234}],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }
    environment_calls = [
        call
        for call in runner.calls
        if "/environments/" in " ".join(call[0]) and call[0][2:4] == ["--method", "PUT"]
    ]
    assert len(environment_calls) == 2
    assert all(json.loads(body) == environment_payload for _, body in environment_calls)

    variable = next(
        call for call in runner.calls if call[0][1:3] == ["variable", "set"]
    )
    assert variable[1] == "https://stage.example.test"
    secret_calls = [call for call in runner.calls if call[0][1:3] == ["secret", "set"]]
    assert len(secret_calls) == 11
    assert {call[1] for call in secret_calls} == {
        "preflight-private-value",
        *distribution.values(),
        *appstore.values(),
    }
    flattened_arguments = " ".join(
        argument for call, _ in runner.calls for argument in call
    )
    for secret in {call[1] for call in secret_calls}:
        assert secret not in flattened_arguments

    branch_creates = [
        call
        for call in runner.calls
        if call[0][2:4] == ["--method", "POST"]
        and call[0][-1].endswith("deployment-branch-policies")
    ]
    assert len(branch_creates) == 2
    assert all(
        json.loads(body) == {"name": "main", "type": "branch"}
        for _, body in branch_creates
    )


def test_github_configuration_removes_non_main_deployment_policies() -> None:
    runner = RecordingRunner()
    original_run = runner.run

    def run(arguments, **kwargs):
        if arguments[-1].endswith("deployment-branch-policies"):
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "branch_policies": [
                            {"id": 7, "name": "release/*", "type": "branch"},
                            {"id": 8, "name": "main", "type": "branch"},
                        ]
                    }
                ),
                "",
            )
        return original_run(arguments, **kwargs)

    runner.run = run
    provision.configure_github(
        runner=runner,
        repository="arniesaha/drover",
        reviewer_id=1234,
        public_origin="https://stage.example.test",
        preflight_token="preflight-private-value",
        distribution_values={},
        appstore_values={},
    )

    delete_calls = [
        command for command, _ in runner.calls if command[2:4] == ["--method", "DELETE"]
    ]
    assert len(delete_calls) == 2
    assert all(command[-1].endswith("/7") for command in delete_calls)


def test_existing_tunnel_is_validated_routed_and_loaded(tmp_path: Path) -> None:
    runner = RecordingRunner()
    tunnel_id = "11111111-2222-3333-4444-555555555555"
    cloudflare_home = tmp_path / "cloudflare"
    cloudflare_home.mkdir()
    credentials = cloudflare_home / f"{tunnel_id}.json"
    credentials.write_text("private-credential", encoding="utf-8")
    credentials.chmod(0o600)

    original_run = runner.run

    def run(arguments, **kwargs):
        if arguments == [
            "/opt/homebrew/bin/cloudflared",
            "tunnel",
            "list",
            "--output",
            "json",
        ]:
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps([{"id": tunnel_id, "name": "drover-testflight"}]),
                "",
            )
        return original_run(arguments, **kwargs)

    runner.run = run
    config, job = provision.configure_tunnel(
        runner=runner,
        root=tmp_path / "stage",
        hostname="stage.example.test",
        cloudflared=Path("/opt/homebrew/bin/cloudflared"),
        cloudflare_home=cloudflare_home,
    )

    assert config.is_file() and job.is_file()
    commands = [call for call, _ in runner.calls]
    assert [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "route",
        "dns",
        "--overwrite-dns",
        "drover-testflight",
        "stage.example.test",
    ] in commands
    assert [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "ingress",
        "validate",
        "--config",
        str(config),
    ] in commands
    assert any(
        command[:3] == ["launchctl", "bootstrap", f"gui/{provision.os.getuid()}"]
        for command in commands
    )


def test_missing_tunnel_is_created_before_local_configuration(tmp_path: Path) -> None:
    runner = RecordingRunner()
    tunnel_id = "11111111-2222-3333-4444-555555555555"
    cloudflare_home = tmp_path / "cloudflare"
    cloudflare_home.mkdir()
    listings = 0
    original_run = runner.run

    def run(arguments, **kwargs):
        nonlocal listings
        if arguments[-3:] == ["tunnel", "list", "--output"]:
            raise AssertionError("malformed list command")
        if arguments[-4:] == ["tunnel", "list", "--output", "json"]:
            listings += 1
            runner.calls.append((arguments, kwargs.get("input_text")))
            records = []
            if listings == 2:
                records = [{"id": tunnel_id, "name": "drover-testflight"}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(records), "")
        if "create" in arguments and arguments[-1] == "drover-testflight":
            (cloudflare_home / "drover-testflight.json").write_text(
                "private-credential", encoding="utf-8"
            )
            (cloudflare_home / "drover-testflight.json").chmod(0o600)
        return original_run(arguments, **kwargs)

    runner.run = run
    provision.configure_tunnel(
        runner=runner,
        root=tmp_path / "stage",
        hostname="stage.example.test",
        cloudflared=Path("/opt/homebrew/bin/cloudflared"),
        cloudflare_home=cloudflare_home,
    )

    assert listings == 2
    assert [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "create",
        "--credentials-file",
        str(cloudflare_home / "drover-testflight.json"),
        "drover-testflight",
    ] in [call for call, _ in runner.calls]


def test_staging_flow_uses_isolated_provider_and_keeps_token_off_argv(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    repository = tmp_path / "repo"
    repository.mkdir()
    root = tmp_path / "stage"
    provider_key = tmp_path / "anthropic_api_key"
    provider_key.write_text("dedicated-provider-secret", encoding="utf-8")
    provider_key.chmod(0o600)
    original_run = runner.run

    def run(arguments, **kwargs):
        if "prepare" in arguments:
            (root / "home" / ".drover").mkdir(parents=True)
        if "credentials" in arguments and "issue-preflight" in arguments:
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(
                arguments, 0, "preflight-private-value\n", ""
            )
        return original_run(arguments, **kwargs)

    runner.run = run
    token = provision.provision_staging(
        runner=runner,
        repository=repository,
        root=root,
        sha="a" * 40,
        public_origin="https://stage.example.test",
        harness="claude-code",
        provider_credential=provider_key,
    )

    assert token == "preflight-private-value"
    staged_key = root / "home" / ".drover" / "anthropic_api_key"
    assert staged_key.read_text(encoding="utf-8") == "dedicated-provider-secret"
    assert staged_key.stat().st_mode & 0o777 == 0o600
    commands = [call for call, _ in runner.calls]
    stage_actions = [
        argument
        for command in commands
        for argument in command
        if argument in {"prepare", "activate", "probe"}
    ]
    assert stage_actions == ["prepare", "activate", "probe"]
    assert all(
        "preflight-private-value" not in " ".join(command) for command in commands
    )


def test_codex_staging_login_uses_only_the_isolated_codex_home(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    repository = tmp_path / "repo"
    repository.mkdir()
    root = tmp_path / "stage"
    original_run = runner.run
    environments: list[dict[str, str] | None] = []

    def run(arguments, **kwargs):
        if "prepare" in arguments:
            (root / "home" / ".drover").mkdir(parents=True)
        if arguments and Path(arguments[0]).name == "codex":
            environments.append(kwargs.get("env"))
        if "credentials" in arguments and "issue-preflight" in arguments:
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(arguments, 0, "preflight-token\n", "")
        return original_run(arguments, **kwargs)

    runner.run = run
    provision.provision_staging(
        runner=runner,
        repository=repository,
        root=root,
        sha="a" * 40,
        public_origin="https://stage.example.test",
        harness="codex",
        provider_credential=None,
        codex=Path("/usr/local/bin/codex"),
    )

    login = next(
        command
        for command, _ in runner.calls
        if command and Path(command[0]).name == "codex"
    )
    assert login[1:] == [
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
    ]
    assert environments == [
        {
            "HOME": str(root / "home"),
            "CODEX_HOME": str(root / "home/.codex"),
            "PATH": provision.SAFE_PATH,
        }
    ]


def test_audit_reports_missing_prerequisites_without_mutating(tmp_path: Path) -> None:
    runner = RecordingRunner()
    original_run = runner.run

    def run(arguments, **kwargs):
        runner.calls.append((arguments, kwargs.get("input_text")))
        if arguments[1:3] in (["variable", "list"], ["secret", "list"]):
            return subprocess.CompletedProcess(arguments, 0, "[]", "")
        if arguments[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "protection_rules": [],
                        "deployment_branch_policy": None,
                    }
                ),
                "",
            )
        if arguments[0] in {"launchctl", "curl"}:
            return subprocess.CompletedProcess(arguments, 1, "", "unavailable")
        if arguments[:2] == ["security", "find-identity"]:
            return subprocess.CompletedProcess(
                arguments, 0, "0 valid identities found", ""
            )
        if arguments[:3] == ["xcrun", "devicectl", "list"]:
            return subprocess.CompletedProcess(
                arguments, 0, "iPhone available (paired)", ""
            )
        return original_run(arguments, **kwargs)

    runner.run = run
    report = provision.audit_prerequisites(
        runner=runner,
        repository="arniesaha/drover",
        root=tmp_path / "stage",
    )

    assert report["repository_variable"] is False
    assert report["staging_secret"] is False
    assert report["upload_secrets"] is False
    assert report["environment_protection"] is False
    assert report["staging_services"] is False
    assert report["distribution_identity"] is False
    assert report["device"] is True
    commands = [call for call, _ in runner.calls]
    assert not any(" set " in f" {' '.join(command)} " for command in commands)
    assert not any(
        "bootstrap" in command or "POST" in command or "PUT" in command
        for command in commands
    )


def test_audit_cli_returns_nonzero_and_machine_readable_blockers(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        provision,
        "audit_prerequisites",
        lambda **_: {"device": True, "staging_services": False},
    )

    status = provision.main(
        [
            "audit",
            "--repository",
            "arniesaha/drover",
            "--root",
            str(tmp_path / "stage"),
            "--json",
        ]
    )

    assert status == 1
    assert json.loads(capsys.readouterr().out) == {
        "device": True,
        "staging_services": False,
    }


def test_apply_requires_typed_confirmation_before_any_mutation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "no")
    called = False

    def unexpected(**_):
        nonlocal called
        called = True

    monkeypatch.setattr(provision, "apply_provisioning", unexpected, raising=False)

    status = provision.main(
        [
            "apply",
            "--source",
            str(tmp_path / "repo"),
            "--root",
            str(tmp_path / "stage"),
        ]
    )

    assert status == 1
    assert called is False
    assert "cancelled" in capsys.readouterr().err


def test_apply_refuses_external_mutation_until_asc_record_is_confirmed(
    tmp_path: Path, monkeypatch
) -> None:
    mutated: list[str] = []
    monkeypatch.setattr(provision, "load_distribution_values", lambda **_: {"d": "v"})
    monkeypatch.setattr(
        provision, "load_appstore_values", lambda **_: {"a": "v"}, raising=False
    )
    monkeypatch.setattr(
        provision,
        "configure_tunnel",
        lambda **_: mutated.append("tunnel"),
    )
    monkeypatch.setattr(
        provision,
        "provision_staging",
        lambda **_: mutated.append("staging"),
    )
    monkeypatch.setattr(
        provision,
        "configure_github",
        lambda **_: mutated.append("github"),
    )

    with pytest.raises(provision.ProvisionError, match="App Store Connect app record"):
        provision.apply_provisioning(
            runner=RecordingRunner(),
            source=tmp_path / "repo",
            root=tmp_path / "stage",
            repository="arniesaha/drover",
            public_origin="https://stage.example.test",
            sha="a" * 40,
            harness="claude-code",
            provider_credential=tmp_path / "provider",
            p12_path=tmp_path / "distribution.p12",
            p12_password="secret",
            profile_path=tmp_path / "profile.mobileprovision",
            asc_key_path=tmp_path / "AuthKey_EXAMPLE123.p8",
            asc_key_id="EXAMPLE123",
            asc_issuer_id="11111111-2222-3333-4444-555555555555",
            reviewer_id=1234,
            cloudflared=Path("/opt/homebrew/bin/cloudflared"),
            cloudflare_home=tmp_path / "cloudflare",
            confirm=lambda _: False,
        )

    assert mutated == []


def test_apply_runs_confirmed_phases_and_writes_only_nonsecret_state(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    distribution = {
        "DROVER_DISTRIBUTION_PROFILE_UUID": ("11111111-2222-3333-4444-555555555555"),
        "DROVER_DISTRIBUTION_IDENTITY_SHA1": "A" * 40,
        "private-signing": "signing-secret",
    }
    appstore = {
        "DROVER_APPSTORE_API_KEY_ID": "EXAMPLE123",
        "private-appstore": "appstore-secret",
    }
    monkeypatch.setattr(
        provision,
        "load_distribution_values",
        lambda **_: events.append("validate-signing") or distribution,
    )
    monkeypatch.setattr(
        provision,
        "load_appstore_values",
        lambda **_: events.append("validate-appstore") or appstore,
    )
    monkeypatch.setattr(
        provision,
        "configure_tunnel",
        lambda **_: events.append("tunnel") or (tmp_path / "config", tmp_path / "job"),
    )
    monkeypatch.setattr(
        provision,
        "provision_staging",
        lambda **_: events.append("staging") or "preflight-secret",
    )

    def github(**kwargs):
        events.append("github")
        assert kwargs["preflight_token"] == "preflight-secret"
        assert kwargs["distribution_values"] is distribution
        assert kwargs["appstore_values"] is appstore

    monkeypatch.setattr(provision, "configure_github", github)
    monkeypatch.setattr(
        provision,
        "audit_prerequisites",
        lambda **_: events.append("audit") or {"ready": True},
    )
    confirmations: list[str] = []

    report = provision.apply_provisioning(
        runner=RecordingRunner(),
        source=tmp_path / "repo",
        root=tmp_path / "stage",
        repository="arniesaha/drover",
        public_origin="https://stage.example.test",
        sha="a" * 40,
        harness="claude-code",
        provider_credential=tmp_path / "provider",
        p12_path=tmp_path / "distribution.p12",
        p12_password="secret",
        profile_path=tmp_path / "profile.mobileprovision",
        asc_key_path=tmp_path / "AuthKey_EXAMPLE123.p8",
        asc_key_id="EXAMPLE123",
        asc_issuer_id="11111111-2222-3333-4444-555555555555",
        reviewer_id=1234,
        cloudflared=Path("/opt/homebrew/bin/cloudflared"),
        cloudflare_home=tmp_path / "cloudflare",
        confirm=lambda message: confirmations.append(message) or True,
    )

    assert report == {"ready": True}
    assert events == [
        "validate-signing",
        "validate-appstore",
        "tunnel",
        "staging",
        "github",
        "audit",
    ]
    assert len(confirmations) == 4
    record = (tmp_path / "stage/provisioning-record.json").read_text()
    assert "signing-secret" not in record
    assert "appstore-secret" not in record
    assert "preflight-secret" not in record
    assert json.loads(record)["source_sha"] == "a" * 40


def test_apply_cli_reads_p12_password_outside_process_arguments(
    tmp_path: Path, monkeypatch
) -> None:
    answers = iter(["APPLY"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(provision.getpass, "getpass", lambda _: "p12-private")
    captured = {}
    runner = RecordingRunner()
    original_run = runner.run

    def run(arguments, **kwargs):
        if arguments[-2:] == ["rev-parse", "origin/main"]:
            runner.calls.append((arguments, kwargs.get("input_text")))
            return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")
        return original_run(arguments, **kwargs)

    runner.run = run
    monkeypatch.setattr(provision, "SubprocessRunner", lambda: runner)

    def apply(**kwargs):
        captured.update(kwargs)
        return {"ready": True}

    monkeypatch.setattr(provision, "apply_provisioning", apply)
    status = provision.main(
        [
            "apply",
            "--source",
            str(tmp_path / "repo"),
            "--root",
            str(tmp_path / "stage"),
            "--repository",
            "arniesaha/drover",
            "--public-origin",
            "https://stage.example.test",
            "--harness",
            "claude-code",
            "--provider-credential",
            str(tmp_path / "anthropic_api_key"),
            "--p12",
            str(tmp_path / "distribution.p12"),
            "--profile",
            str(tmp_path / "profile.mobileprovision"),
            "--asc-key",
            str(tmp_path / "AuthKey_EXAMPLE123.p8"),
            "--asc-key-id",
            "EXAMPLE123",
            "--asc-issuer-id",
            "11111111-2222-3333-4444-555555555555",
            "--reviewer-id",
            "1234",
            "--cloudflared",
            "/opt/homebrew/bin/cloudflared",
            "--cloudflare-home",
            str(tmp_path / "cloudflare"),
        ]
    )

    assert status == 0
    assert captured["p12_password"] == "p12-private"
    assert captured["sha"] == "a" * 40
    assert any("fetch" in command for command, _ in runner.calls)
    assert "p12-private" not in " ".join(captured.get("arguments", []))
