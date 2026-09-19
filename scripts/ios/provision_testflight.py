#!/usr/bin/env python3
"""Interactively provision Drover's isolated Internal TestFlight lane."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    load_pem_private_key,
    pkcs12,
)
from cryptography.x509.oid import NameOID

SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TUNNEL_LABEL = "com.drover.testflight-tunnel"
TUNNEL_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class ProvisionError(ValueError):
    """Sanitized provisioning failure safe to display to an operator."""


class SubprocessRunner:
    """Run commands without surfacing private stdout/stderr on failures."""

    def run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        interactive: bool = False,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        if interactive and input_text is not None:
            raise ProvisionError("interactive commands cannot receive secret stdin")
        try:
            result = subprocess.run(
                arguments,
                input=input_text,
                text=True,
                capture_output=not interactive,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisionError("operator command could not run") from exc
        if check and result.returncode:
            raise ProvisionError("operator command failed; inspect it separately")
        return result


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise ProvisionError("refusing to replace an unsafe private file")
        if path.read_bytes() == data:
            path.chmod(0o600)
            return
    descriptor, temporary = tempfile.mkstemp(prefix=".provision-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_tunnel_files(
    *,
    root: Path,
    hostname: str,
    tunnel_id: str,
    credentials_file: Path,
    cloudflared: Path,
) -> tuple[Path, Path]:
    """Write private local-config and launchd files for the staging tunnel."""
    root = root.resolve()
    if credentials_file.is_symlink():
        raise ProvisionError("tunnel credential must be a regular file")
    credentials_file = credentials_file.resolve()
    cloudflared = Path(cloudflared)
    if not HOSTNAME.fullmatch(hostname) or not TUNNEL_ID.fullmatch(tunnel_id):
        raise ProvisionError("invalid tunnel hostname or identifier")
    if not credentials_file.is_file() or credentials_file.is_symlink():
        raise ProvisionError("tunnel credential must be a regular file")
    if credentials_file.stat().st_mode & 0o077:
        raise ProvisionError("tunnel credential must be owner-only")
    if not cloudflared.is_absolute():
        raise ProvisionError("cloudflared path must be absolute")

    config = root / "tunnel" / "cloudflared.yml"
    job = root / "launchd" / f"{TUNNEL_LABEL}.plist"
    config_text = (
        f"tunnel: {json.dumps(tunnel_id)}\n"
        f"credentials-file: {json.dumps(str(credentials_file))}\n"
        "ingress:\n"
        f"  - hostname: {json.dumps(hostname)}\n"
        "    service: http://127.0.0.1:17080\n"
        "  - service: http_status:404\n"
    )
    _private_write(config, config_text.encode())
    plist = {
        "Label": TUNNEL_LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            "-i",
            f"HOME={root / 'home'}",
            f"PATH={SAFE_PATH}",
            str(cloudflared),
            "tunnel",
            "--config",
            str(config),
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "Umask": 0o077,
        "StandardOutPath": str(root / "logs" / f"{TUNNEL_LABEL}.stdout.log"),
        "StandardErrorPath": str(root / "logs" / f"{TUNNEL_LABEL}.stderr.log"),
    }
    _private_write(job, plistlib.dumps(plist))
    return config, job


def derive_distribution_values(
    *,
    p12_bytes: bytes,
    p12_password: str,
    profile_bytes: bytes,
    profile: dict,
) -> dict[str, str]:
    """Validate signing inputs and derive the exact protected workflow values."""
    if not p12_password:
        raise ProvisionError("distribution PKCS#12 password is required")
    try:
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            p12_bytes, p12_password.encode()
        )
    except Exception as exc:
        raise ProvisionError("distribution PKCS#12 could not be opened") from exc
    if private_key is None or certificate is None:
        raise ProvisionError("distribution PKCS#12 lacks a key or certificate")

    names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    identity = names[0].value if len(names) == 1 else ""
    team_ids = profile.get("TeamIdentifier")
    entitlements = profile.get("Entitlements")
    team_id = team_ids[0] if isinstance(team_ids, list) and len(team_ids) == 1 else ""
    uuid = profile.get("UUID")
    expiration = profile.get("ExpirationDate")
    now = datetime.now(timezone.utc)
    if isinstance(expiration, datetime) and expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    valid_profile = (
        isinstance(entitlements, dict)
        and re.fullmatch(r"[A-Z0-9]{10}", team_id or "")
        and isinstance(uuid, str)
        and TUNNEL_ID.fullmatch(uuid.lower())
        and isinstance(expiration, datetime)
        and expiration > now
        and entitlements.get("application-identifier") == f"{team_id}.com.arnab.drover"
        and entitlements.get("com.apple.developer.team-identifier") == team_id
        and entitlements.get("get-task-allow") is False
        and entitlements.get("aps-environment") == "production"
        and not profile.get("ProvisionedDevices")
        and profile.get("ProvisionsAllDevices") is not True
    )
    if not valid_profile:
        raise ProvisionError("profile is not an App Store profile for Drover")
    if not_before > now or not_after <= now:
        raise ProvisionError("distribution certificate is not currently valid")
    if not identity.startswith("Apple Distribution: ") or not identity.endswith(
        f"({team_id})"
    ):
        raise ProvisionError(
            "certificate is not the matching Apple Distribution identity"
        )
    profile_certificates = profile.get("DeveloperCertificates")
    if (
        not isinstance(profile_certificates, list)
        or certificate.public_bytes(Encoding.DER) not in profile_certificates
    ):
        raise ProvisionError("profile does not include the distribution certificate")

    return {
        "DROVER_DISTRIBUTION_P12_BASE64": base64.b64encode(p12_bytes).decode(),
        "DROVER_DISTRIBUTION_P12_PASSWORD": p12_password,
        "DROVER_DISTRIBUTION_PROFILE_BASE64": base64.b64encode(profile_bytes).decode(),
        "DROVER_DISTRIBUTION_TEAM_ID": team_id,
        "DROVER_DISTRIBUTION_PROFILE_UUID": uuid,
        "DROVER_DISTRIBUTION_IDENTITY_SHA1": certificate.fingerprint(hashes.SHA1())
        .hex()
        .upper(),
        "DROVER_DISTRIBUTION_IDENTITY_NAME": identity,
    }


def _read_private_input(path: Path, description: str) -> bytes:
    path = path.expanduser()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProvisionError(f"{description} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProvisionError(f"{description} must be an owner-only regular file")
    material = path.read_bytes()
    if not material:
        raise ProvisionError(f"{description} is empty")
    return material


def load_distribution_values(
    *, runner, p12_path: Path, p12_password: str, profile_path: Path
) -> dict[str, str]:
    """Read private signing files and decode the CMS profile locally."""
    p12_path = p12_path.expanduser()
    profile_path = profile_path.expanduser()
    p12_bytes = _read_private_input(p12_path, "distribution PKCS#12")
    profile_bytes = _read_private_input(profile_path, "provisioning profile")
    result = runner.run(["security", "cms", "-D", "-i", str(profile_path.resolve())])
    try:
        profile = plistlib.loads(result.stdout.encode())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProvisionError("provisioning profile could not be decoded") from exc
    return derive_distribution_values(
        p12_bytes=p12_bytes,
        p12_password=p12_password,
        profile_bytes=profile_bytes,
        profile=profile,
    )


def derive_appstore_values(
    *, key_bytes: bytes, key_id: str, issuer_id: str
) -> dict[str, str]:
    """Validate an App Store Connect ES256 key and derive workflow values."""
    try:
        key = load_pem_private_key(key_bytes, password=None)
    except Exception as exc:
        raise ProvisionError("App Store Connect private key could not be read") from exc
    if (
        not isinstance(key, ec.EllipticCurvePrivateKey)
        or not isinstance(key.curve, ec.SECP256R1)
        or not re.fullmatch(r"[A-Z0-9]{8,20}", key_id)
        or not TUNNEL_ID.fullmatch(issuer_id.lower())
    ):
        raise ProvisionError("App Store Connect key metadata is invalid")
    return {
        "DROVER_APPSTORE_API_KEY_ID": key_id,
        "DROVER_APPSTORE_API_ISSUER_ID": issuer_id,
        "DROVER_APPSTORE_API_PRIVATE_KEY_BASE64": base64.b64encode(key_bytes).decode(),
    }


def load_appstore_values(
    *, key_path: Path, key_id: str, issuer_id: str
) -> dict[str, str]:
    return derive_appstore_values(
        key_bytes=_read_private_input(key_path, "App Store Connect private key"),
        key_id=key_id,
        issuer_id=issuer_id,
    )


def _validate_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in origin)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProvisionError("staging URL must be an HTTPS origin")
    return origin.removesuffix("/")


def configure_github(
    *,
    runner,
    repository: str,
    reviewer_id: int,
    public_origin: str,
    preflight_token: str,
    distribution_values: dict[str, str],
    appstore_values: dict[str, str],
) -> None:
    """Protect both environments and stream their values through stdin."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ProvisionError("GitHub repository must be OWNER/REPO")
    if not isinstance(reviewer_id, int) or reviewer_id <= 0:
        raise ProvisionError("GitHub reviewer identifier is invalid")
    origin = _validate_origin(public_origin)
    if not preflight_token:
        raise ProvisionError("preflight token is empty")

    environment_payload = json.dumps(
        {
            "wait_timer": 0,
            "prevent_self_review": False,
            "reviewers": [{"type": "User", "id": reviewer_id}],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        },
        separators=(",", ":"),
    )
    for environment in ("ios-testflight-staging", "ios-testflight-upload"):
        encoded = quote(environment, safe="")
        endpoint = f"repos/{repository}/environments/{encoded}"
        runner.run(
            ["gh", "api", "--method", "PUT", "--input", "-", endpoint],
            input_text=environment_payload,
        )
        policies_endpoint = endpoint + "/deployment-branch-policies"
        result = runner.run(["gh", "api", "--method", "GET", policies_endpoint])
        try:
            policies = json.loads(result.stdout).get("branch_policies", [])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProvisionError("GitHub branch policy response was invalid") from exc
        if not isinstance(policies, list) or not all(
            isinstance(policy, dict) for policy in policies
        ):
            raise ProvisionError("GitHub branch policy response was invalid")
        main_policy = next(
            (
                policy
                for policy in policies
                if policy.get("name") == "main" and policy.get("type") == "branch"
            ),
            None,
        )
        for policy in policies:
            if policy is main_policy:
                continue
            policy_id = policy.get("id")
            if not isinstance(policy_id, int) or policy_id <= 0:
                raise ProvisionError("GitHub branch policy identifier was invalid")
            runner.run(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"{policies_endpoint}/{policy_id}",
                ]
            )
        if main_policy is None:
            runner.run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    "--input",
                    "-",
                    policies_endpoint,
                ],
                input_text=json.dumps({"name": "main", "type": "branch"}),
            )

    runner.run(
        [
            "gh",
            "variable",
            "set",
            "DROVER_TESTFLIGHT_STAGING_URL",
            "--repo",
            repository,
        ],
        input_text=origin,
    )
    runner.run(
        [
            "gh",
            "secret",
            "set",
            "DROVER_TESTFLIGHT_PREFLIGHT_TOKEN",
            "--repo",
            repository,
            "--env",
            "ios-testflight-staging",
        ],
        input_text=preflight_token,
    )
    for name, value in {**distribution_values, **appstore_values}.items():
        runner.run(
            [
                "gh",
                "secret",
                "set",
                name,
                "--repo",
                repository,
                "--env",
                "ios-testflight-upload",
            ],
            input_text=value,
        )


def configure_tunnel(
    *,
    runner,
    root: Path,
    hostname: str,
    cloudflared: Path,
    cloudflare_home: Path,
    tunnel_name: str = "drover-testflight",
) -> tuple[Path, Path]:
    """Reuse and load one locally-managed tunnel for the staging listener."""
    if tunnel_name != "drover-testflight":
        raise ProvisionError("unexpected TestFlight tunnel name")
    if not HOSTNAME.fullmatch(hostname):
        raise ProvisionError("invalid staging hostname")
    cloudflare_home = cloudflare_home.expanduser().resolve()
    cloudflare_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    cloudflare_home.chmod(0o700)
    cloudflare_env = {
        "HOME": str(Path.home()),
        "PATH": SAFE_PATH,
        "TUNNEL_ORIGIN_CERT": str(cloudflare_home / "cert.pem"),
    }

    def matching_tunnels():
        result = runner.run(
            [str(cloudflared), "tunnel", "list", "--output", "json"],
            env=cloudflare_env,
        )
        try:
            records = json.loads(result.stdout)
            return [
                record
                for record in records
                if record.get("name") == tunnel_name and not record.get("deleted_at")
            ]
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProvisionError("Cloudflare tunnel inventory was invalid") from exc

    matches = matching_tunnels()
    created_credentials = cloudflare_home / f"{tunnel_name}.json"
    created = False
    if not matches:
        runner.run(
            [
                str(cloudflared),
                "tunnel",
                "create",
                "--credentials-file",
                str(created_credentials),
                tunnel_name,
            ],
            env=cloudflare_env,
        )
        created = True
        matches = matching_tunnels()
    if len(matches) != 1:
        raise ProvisionError("expected exactly one active Drover TestFlight tunnel")
    tunnel_id = str(matches[0].get("id", "")).lower()
    if not TUNNEL_ID.fullmatch(tunnel_id):
        raise ProvisionError("Cloudflare tunnel identifier was invalid")
    credentials = (
        created_credentials if created else cloudflare_home / f"{tunnel_id}.json"
    )
    for relative in ("home", "logs"):
        directory = root.resolve() / relative
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    config, job = write_tunnel_files(
        root=root,
        hostname=hostname,
        tunnel_id=tunnel_id,
        credentials_file=credentials,
        cloudflared=cloudflared,
    )
    runner.run(
        [
            str(cloudflared),
            "tunnel",
            "ingress",
            "validate",
            "--config",
            str(config),
        ]
    )
    runner.run(
        [
            str(cloudflared),
            "tunnel",
            "route",
            "dns",
            "--overwrite-dns",
            tunnel_name,
            hostname,
        ],
        env=cloudflare_env,
    )
    domain = f"gui/{os.getuid()}"
    result = runner.run(
        ["launchctl", "bootout", f"{domain}/{TUNNEL_LABEL}"], check=False
    )
    if result.returncode not in (0, 3):
        raise ProvisionError("existing TestFlight tunnel job could not be unloaded")
    runner.run(["launchctl", "bootstrap", domain, str(job)])
    return config, job


def provision_staging(
    *,
    runner,
    repository: Path,
    root: Path,
    sha: str,
    public_origin: str,
    harness: str,
    provider_credential: Path | None,
    codex: Path | None = None,
) -> str:
    """Prepare, activate, probe, and mint the isolated staging credential."""
    repository = repository.resolve()
    root = root.resolve()
    origin = _validate_origin(public_origin)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ProvisionError("candidate SHA must be full lowercase 40-hex")
    if harness not in ("claude-code", "codex"):
        raise ProvisionError("unsupported staging harness")
    stage_script = repository / "scripts" / "testflight" / "stage.py"
    runner.run(
        [
            sys.executable,
            str(stage_script),
            "prepare",
            "--repository",
            str(repository),
            "--root",
            str(root),
            "--sha",
            sha,
            "--public-url",
            origin,
        ]
    )
    if harness == "claude-code":
        if provider_credential is None:
            raise ProvisionError("dedicated Claude credential file is required")
        material = _read_private_input(
            provider_credential, "provider credential"
        ).strip()
        if not material:
            raise ProvisionError("provider credential is empty")
        _private_write(root / "home/.drover/anthropic_api_key", material)
    else:
        codex_path = codex or (
            Path(candidate)
            if (candidate := shutil.which("codex", path=SAFE_PATH))
            else None
        )
        if codex_path is None or not codex_path.is_absolute():
            raise ProvisionError("Codex CLI is unavailable for isolated login")
        codex_home = root / "home/.codex"
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home.chmod(0o700)
        runner.run(
            [
                str(codex_path),
                "-c",
                'cli_auth_credentials_store="file"',
                "login",
            ],
            interactive=True,
            env={
                "HOME": str(root / "home"),
                "CODEX_HOME": str(codex_home),
                "PATH": SAFE_PATH,
            },
        )

    runner.run(
        [
            sys.executable,
            str(stage_script),
            "activate",
            "--root",
            str(root),
            "--sha",
            sha,
        ]
    )
    server = root / "worktrees" / sha / ".venv/bin/drover-server"
    result = runner.run(
        [
            str(server),
            "--config",
            str(root / "home/.drover/config.toml"),
            "credentials",
            "issue-preflight",
            "--label",
            "internal-testflight",
        ],
        env={"HOME": str(root / "home"), "PATH": "/usr/bin:/bin"},
    )
    token = result.stdout.strip()
    if not token or not all(32 < ord(character) < 127 for character in token):
        raise ProvisionError("staging preflight credential was invalid")
    runner.run(
        [
            sys.executable,
            str(stage_script),
            "probe",
            "--root",
            str(root),
            "--sha",
            sha,
            "--harness",
            harness,
        ]
    )
    return token


UPLOAD_SECRET_NAMES = {
    "DROVER_DISTRIBUTION_P12_BASE64",
    "DROVER_DISTRIBUTION_P12_PASSWORD",
    "DROVER_DISTRIBUTION_PROFILE_BASE64",
    "DROVER_DISTRIBUTION_TEAM_ID",
    "DROVER_DISTRIBUTION_PROFILE_UUID",
    "DROVER_DISTRIBUTION_IDENTITY_SHA1",
    "DROVER_DISTRIBUTION_IDENTITY_NAME",
    "DROVER_APPSTORE_API_KEY_ID",
    "DROVER_APPSTORE_API_ISSUER_ID",
    "DROVER_APPSTORE_API_PRIVATE_KEY_BASE64",
}


def _listed_names(result) -> set[str]:
    try:
        return {
            item["name"]
            for item in json.loads(result.stdout)
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProvisionError("GitHub inventory response was invalid") from exc


def audit_prerequisites(*, runner, repository: str, root: Path) -> dict[str, bool]:
    """Return a read-only readiness report without exposing secret values."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ProvisionError("GitHub repository must be OWNER/REPO")
    variable_names = _listed_names(
        runner.run(
            ["gh", "variable", "list", "--repo", repository, "--json", "name"],
            check=False,
        )
    )
    staging_names = _listed_names(
        runner.run(
            [
                "gh",
                "secret",
                "list",
                "--repo",
                repository,
                "--env",
                "ios-testflight-staging",
                "--json",
                "name",
            ],
            check=False,
        )
    )
    upload_names = _listed_names(
        runner.run(
            [
                "gh",
                "secret",
                "list",
                "--repo",
                repository,
                "--env",
                "ios-testflight-upload",
                "--json",
                "name",
            ],
            check=False,
        )
    )
    protected = True
    for environment in ("ios-testflight-staging", "ios-testflight-upload"):
        result = runner.run(
            ["gh", "api", f"repos/{repository}/environments/{environment}"],
            check=False,
        )
        try:
            payload = json.loads(result.stdout) if result.returncode == 0 else {}
        except (TypeError, ValueError):
            payload = {}
        rules = payload.get("protection_rules", [])
        policy = payload.get("deployment_branch_policy")
        policies_result = runner.run(
            [
                "gh",
                "api",
                f"repos/{repository}/environments/{environment}/"
                "deployment-branch-policies",
            ],
            check=False,
        )
        try:
            branch_policies = (
                json.loads(policies_result.stdout).get("branch_policies", [])
                if policies_result.returncode == 0
                else []
            )
        except (AttributeError, TypeError, ValueError):
            branch_policies = []
        main_only = (
            isinstance(branch_policies, list)
            and len(branch_policies) == 1
            and isinstance(branch_policies[0], dict)
            and branch_policies[0].get("name") == "main"
            and branch_policies[0].get("type") == "branch"
        )
        protected = (
            protected
            and any(rule.get("type") == "required_reviewers" for rule in rules)
            and policy
            == {
                "protected_branches": False,
                "custom_branch_policies": True,
            }
            and main_only
        )

    domain = f"gui/{os.getuid()}"
    services = all(
        runner.run(["launchctl", "print", f"{domain}/{label}"], check=False).returncode
        == 0
        for label in ("com.drover.testflight-server", "com.drover.testflight-harnessd")
    )
    identities = runner.run(
        ["security", "find-identity", "-v", "-p", "codesigning"], check=False
    )
    devices = runner.run(
        ["xcrun", "devicectl", "list", "devices", "--timeout", "10"],
        check=False,
    )
    return {
        "repository_variable": "DROVER_TESTFLIGHT_STAGING_URL" in variable_names,
        "staging_secret": "DROVER_TESTFLIGHT_PREFLIGHT_TOKEN" in staging_names,
        "upload_secrets": UPLOAD_SECRET_NAMES <= upload_names,
        "environment_protection": protected,
        "staging_services": services,
        "staging_root": (root / ".stage-root").is_file(),
        "tunnel": (root / "tunnel/cloudflared.yml").is_file()
        and runner.run(
            ["launchctl", "print", f"{domain}/{TUNNEL_LABEL}"], check=False
        ).returncode
        == 0,
        "distribution_identity": "Apple Distribution:" in identities.stdout,
        "device": "available (paired)" in devices.stdout,
    }


def apply_provisioning(
    *,
    runner,
    source: Path,
    root: Path,
    repository: str,
    public_origin: str,
    sha: str,
    harness: str,
    provider_credential: Path | None,
    p12_path: Path,
    p12_password: str,
    profile_path: Path,
    asc_key_path: Path,
    asc_key_id: str,
    asc_issuer_id: str,
    reviewer_id: int,
    cloudflared: Path,
    cloudflare_home: Path,
    confirm,
    dispatch: bool = False,
    version: str | None = None,
    build: str | None = None,
) -> dict[str, bool]:
    """Execute the approved external provisioning phases in order."""
    distribution = load_distribution_values(
        runner=runner,
        p12_path=p12_path,
        p12_password=p12_password,
        profile_path=profile_path,
    )
    appstore = load_appstore_values(
        key_path=asc_key_path,
        key_id=asc_key_id,
        issuer_id=asc_issuer_id,
    )
    if not confirm(
        "Confirm com.arnab.drover exists in App Store Connect and this API key "
        "can upload builds"
    ):
        raise ProvisionError("App Store Connect app record was not confirmed")
    if not confirm("Create or update the dedicated Cloudflare tunnel and DNS route"):
        raise ProvisionError("Cloudflare tunnel provisioning was not confirmed")
    cloudflare_home = cloudflare_home.expanduser().resolve()
    if not (cloudflare_home / "cert.pem").is_file():
        cloudflare_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        runner.run(
            [str(cloudflared), "tunnel", "login"],
            interactive=True,
            env={
                "HOME": str(Path.home()),
                "PATH": SAFE_PATH,
                "TUNNEL_ORIGIN_CERT": str(cloudflare_home / "cert.pem"),
            },
        )
    configure_tunnel(
        runner=runner,
        root=root,
        hostname=urlsplit(_validate_origin(public_origin)).hostname,
        cloudflared=cloudflared,
        cloudflare_home=cloudflare_home,
    )

    if not confirm("Prepare, activate, and probe the isolated staging services"):
        raise ProvisionError("staging activation was not confirmed")
    preflight_token = provision_staging(
        runner=runner,
        repository=source,
        root=root,
        sha=sha,
        public_origin=public_origin,
        harness=harness,
        provider_credential=provider_credential,
    )

    if not confirm("Write the protected GitHub environments, variable, and secrets"):
        raise ProvisionError("GitHub configuration was not confirmed")
    configure_github(
        runner=runner,
        repository=repository,
        reviewer_id=reviewer_id,
        public_origin=public_origin,
        preflight_token=preflight_token,
        distribution_values=distribution,
        appstore_values=appstore,
    )
    record = {
        "source_sha": sha,
        "github_repository": repository,
        "staging_url_sha256": hashlib.sha256(
            _validate_origin(public_origin).encode()
        ).hexdigest(),
        "distribution_profile_uuid": distribution.get(
            "DROVER_DISTRIBUTION_PROFILE_UUID"
        ),
        "distribution_identity_sha1": distribution.get(
            "DROVER_DISTRIBUTION_IDENTITY_SHA1"
        ),
        "appstore_key_id": appstore.get("DROVER_APPSTORE_API_KEY_ID"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _private_write(
        root.resolve() / "provisioning-record.json",
        (json.dumps(record, sort_keys=True, indent=2) + "\n").encode(),
    )

    if dispatch:
        if (
            not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)
            or not isinstance(build, str)
            or not re.fullmatch(r"[1-9][0-9]*", build)
        ):
            raise ProvisionError("dispatch version or build is invalid")
        if not confirm("Upload this candidate to Internal TestFlight"):
            raise ProvisionError("TestFlight dispatch was not confirmed")
        runner.run(
            [
                "gh",
                "workflow",
                "run",
                "ios-testflight-internal.yml",
                "--repo",
                repository,
                "--ref",
                "main",
                "-f",
                f"version={version}",
                "-f",
                f"build={build}",
            ]
        )
    return audit_prerequisites(runner=runner, repository=repository, root=root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    audit = commands.add_parser("audit", help="read-only prerequisite inventory")
    audit.add_argument("--repository", default="arniesaha/drover")
    audit.add_argument("--root", type=Path, default=Path.home() / ".drover-testflight")
    audit.add_argument("--json", action="store_true", dest="as_json")
    apply = commands.add_parser("apply", help="interactively provision prerequisites")
    apply.add_argument("--source", type=Path, default=Path.cwd())
    apply.add_argument("--root", type=Path, default=Path.home() / ".drover-testflight")
    apply.add_argument("--repository", default="arniesaha/drover")
    apply.add_argument("--public-origin")
    apply.add_argument("--sha")
    apply.add_argument("--harness", choices=("claude-code", "codex"), default="codex")
    apply.add_argument("--provider-credential", type=Path)
    apply.add_argument("--p12", type=Path)
    apply.add_argument("--profile", type=Path)
    apply.add_argument("--asc-key", type=Path)
    apply.add_argument("--asc-key-id")
    apply.add_argument("--asc-issuer-id")
    apply.add_argument("--reviewer-id", type=int)
    apply.add_argument("--cloudflared", type=Path)
    apply.add_argument(
        "--cloudflare-home", type=Path, default=Path.home() / ".cloudflared"
    )
    apply.add_argument("--dispatch", action="store_true")
    apply.add_argument("--version")
    apply.add_argument("--build")
    args = parser.parse_args(argv)
    try:
        if args.action == "audit":
            report = audit_prerequisites(
                runner=SubprocessRunner(),
                repository=args.repository,
                root=args.root.expanduser().resolve(),
            )
            if args.as_json:
                print(json.dumps(report, sort_keys=True, indent=2))
            else:
                for name, ready in report.items():
                    print(f"{'OK' if ready else 'BLOCKED':7} {name}")
            return 0 if all(report.values()) else 1
        if args.action == "apply":
            confirmation = input(
                "This will change Cloudflare, GitHub, and local launchd state. "
                "Type APPLY to continue: "
            )
            if confirmation != "APPLY":
                print("provisioning cancelled", file=sys.stderr)
                return 1
            runner = SubprocessRunner()

            def required(value, prompt_text, *, path=False):
                supplied = value if value is not None else input(prompt_text).strip()
                if supplied in (None, ""):
                    raise ProvisionError("a required interactive input was empty")
                return Path(supplied) if path else supplied

            source = args.source.expanduser().resolve()
            origin = required(args.public_origin, "Staging HTTPS origin: ")
            if args.sha:
                sha = args.sha
            else:
                runner.run(
                    [
                        "git",
                        "-C",
                        str(source),
                        "fetch",
                        "--prune",
                        "origin",
                        "+refs/heads/main:refs/remotes/origin/main",
                    ]
                )
                sha = runner.run(
                    ["git", "-C", str(source), "rev-parse", "origin/main"]
                ).stdout.strip()
            p12_path = required(args.p12, "Apple Distribution .p12 path: ", path=True)
            profile_path = required(
                args.profile, "App Store .mobileprovision path: ", path=True
            )
            asc_key_path = required(
                args.asc_key, "App Store Connect AuthKey .p8 path: ", path=True
            )
            key_match = re.fullmatch(r"AuthKey_([A-Z0-9]{8,20})\.p8", asc_key_path.name)
            key_default = key_match.group(1) if key_match else None
            asc_key_id = required(
                args.asc_key_id or key_default, "App Store Connect key ID: "
            )
            asc_issuer_id = required(
                args.asc_issuer_id, "App Store Connect issuer UUID: "
            )
            provider = args.provider_credential
            if args.harness == "claude-code":
                provider = required(
                    provider, "Dedicated Anthropic API key file: ", path=True
                )
            reviewer_id = args.reviewer_id
            if reviewer_id is None:
                reviewer_id = int(
                    runner.run(["gh", "api", "user", "--jq", ".id"]).stdout.strip()
                )
            cloudflared = args.cloudflared
            if cloudflared is not None:
                cloudflared = cloudflared.expanduser().resolve()
            if cloudflared is None:
                executable = shutil.which("cloudflared", path=SAFE_PATH)
                if executable is None:
                    if (
                        input("cloudflared is missing. Type INSTALL to use Homebrew: ")
                        != "INSTALL"
                    ):
                        raise ProvisionError(
                            "cloudflared installation was not approved"
                        )
                    runner.run(["brew", "install", "cloudflared"], interactive=True)
                    executable = shutil.which("cloudflared", path=SAFE_PATH)
                if executable is None:
                    raise ProvisionError(
                        "cloudflared is unavailable after installation"
                    )
                cloudflared = Path(executable)

            def phase_confirmation(message: str) -> bool:
                return input(f"{message}. Type YES to continue: ") == "YES"

            report = apply_provisioning(
                runner=runner,
                source=source,
                root=args.root.expanduser().resolve(),
                repository=args.repository,
                public_origin=origin,
                sha=sha,
                harness=args.harness,
                provider_credential=provider,
                p12_path=p12_path,
                p12_password=getpass.getpass("PKCS#12 password: "),
                profile_path=profile_path,
                asc_key_path=asc_key_path,
                asc_key_id=asc_key_id,
                asc_issuer_id=asc_issuer_id,
                reviewer_id=reviewer_id,
                cloudflared=cloudflared,
                cloudflare_home=args.cloudflare_home.expanduser(),
                confirm=phase_confirmation,
                dispatch=args.dispatch,
                version=args.version,
                build=args.build,
            )
            for name, ready in report.items():
                print(f"{'OK' if ready else 'BLOCKED':7} {name}")
            return 0
    except ProvisionError as error:
        print(f"provisioning failed: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
