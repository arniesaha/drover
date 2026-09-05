#!/usr/bin/env python3
"""Verify metadata and signing evidence from an iOS distribution artifact."""

from __future__ import annotations

import argparse
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from xml.parsers.expat import ExpatError

DEFAULT_BUNDLE_IDENTIFIER = "com.arnab.drover"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")


class ArtifactVerificationError(ValueError):
    """Raised when an artifact is not a distribution-ready iOS application."""


@dataclass(frozen=True)
class SigningEvidence:
    authorities: tuple[str, ...]
    entitlements: dict[str, Any]


@dataclass(frozen=True)
class ArtifactIdentity:
    bundle_identifier: str
    version: str
    build: str
    sdk_version: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _contains_build_setting(value: str) -> bool:
    return "$(" in value or "${" in value


def _version_parts(value: str, *, field: str) -> tuple[int, ...]:
    if _contains_build_setting(value) or not VERSION_PATTERN.fullmatch(value):
        raise ArtifactVerificationError(f"{field} is not an expanded numeric version")
    return tuple(int(part) for part in value.split("."))


def _version_at_least(actual: str, required: str) -> bool:
    actual_parts = _version_parts(actual, field="artifact SDK")
    required_parts = _version_parts(required, field="SDK floor")
    width = max(len(actual_parts), len(required_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= required_parts + (
        0,
    ) * (width - len(required_parts))


def _required_string(plist: dict[str, Any], key: str, *, source: str) -> str:
    value = plist.get(key)
    if not isinstance(value, str) or not value:
        raise ArtifactVerificationError(f"{source} is missing {key}")
    if _contains_build_setting(value):
        raise ArtifactVerificationError(
            f"{source} contains an unexpanded build setting"
        )
    return value


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("NSPrivacyTracking") is not False:
        raise ArtifactVerificationError(
            "privacy manifest does not declare tracking disabled"
        )
    for key in ("NSPrivacyCollectedDataTypes", "NSPrivacyAccessedAPITypes"):
        if not isinstance(manifest.get(key), list):
            raise ArtifactVerificationError(f"privacy manifest is missing {key}")


def validate_distribution_metadata(
    info: dict[str, Any],
    manifest: dict[str, Any],
    signing: SigningEvidence,
    *,
    expected_version: str,
    expected_build: str,
    sdk_floor: str,
    expected_bundle_identifier: str = DEFAULT_BUNDLE_IDENTIFIER,
) -> ArtifactIdentity:
    """Validate plist data and signed entitlements without reading source settings."""
    bundle_identifier = _required_string(
        info, "CFBundleIdentifier", source="Info.plist"
    )
    if bundle_identifier != expected_bundle_identifier:
        raise ArtifactVerificationError(
            "bundle identifier does not match the candidate"
        )

    version = _required_string(info, "CFBundleShortVersionString", source="Info.plist")
    build = _required_string(info, "CFBundleVersion", source="Info.plist")
    _version_parts(version, field="artifact version")
    _version_parts(build, field="artifact build")
    _version_parts(expected_version, field="expected version")
    _version_parts(expected_build, field="expected build")
    if version != expected_version:
        raise ArtifactVerificationError("artifact version does not match the candidate")
    if build != expected_build:
        raise ArtifactVerificationError("artifact build does not match the candidate")

    platform = _required_string(info, "DTPlatformName", source="Info.plist")
    if platform != "iphoneos":
        raise ArtifactVerificationError("artifact is not built for iPhoneOS")
    sdk_name = _required_string(info, "DTSDKName", source="Info.plist")
    sdk_match = re.fullmatch(r"iphoneos([0-9]+(?:\.[0-9]+){0,2})", sdk_name)
    if sdk_match is None:
        raise ArtifactVerificationError("artifact does not report an iPhoneOS SDK")
    sdk_version = sdk_match.group(1)
    platform_version = _required_string(info, "DTPlatformVersion", source="Info.plist")
    if platform_version != sdk_version:
        raise ArtifactVerificationError("artifact SDK metadata is inconsistent")
    if not _version_at_least(sdk_version, sdk_floor):
        raise ArtifactVerificationError("artifact SDK is below the required floor")

    _validate_manifest(manifest)

    if not any(
        authority.startswith("Apple Distribution:") for authority in signing.authorities
    ):
        raise ArtifactVerificationError(
            "artifact is not signed with an Apple Distribution certificate"
        )
    entitlements = signing.entitlements
    if entitlements.get("aps-environment") != "production":
        raise ArtifactVerificationError("signed APNs entitlement is not production")
    if entitlements.get("get-task-allow") is not False:
        raise ArtifactVerificationError(
            "signed debugger entitlement is enabled or unavailable"
        )
    application_identifier = entitlements.get("application-identifier")
    if not isinstance(
        application_identifier, str
    ) or not application_identifier.endswith(f".{bundle_identifier}"):
        raise ArtifactVerificationError(
            "signed application identifier does not match the bundle"
        )

    return ArtifactIdentity(
        bundle_identifier=bundle_identifier,
        version=version,
        build=build,
        sdk_version=sdk_version,
    )


def _run_codesign(command: Sequence[str], run: Runner) -> str:
    try:
        completed = run(list(command), capture_output=True, text=True, check=False)
    except OSError as error:
        raise ArtifactVerificationError("codesign is unavailable") from error
    if completed.returncode != 0:
        raise ArtifactVerificationError("codesign could not verify the signed artifact")
    return f"{completed.stdout}\n{completed.stderr}"


def _load_codesign_entitlements(output: str) -> dict[str, Any]:
    start = output.find("<?xml")
    if start < 0:
        start = output.find("<plist")
    if start < 0:
        raise ArtifactVerificationError("codesign returned no signed entitlements")
    try:
        entitlements = plistlib.loads(output[start:].encode())
    except (ExpatError, plistlib.InvalidFileException, ValueError) as error:
        raise ArtifactVerificationError(
            "codesign returned malformed signed entitlements"
        ) from error
    if not isinstance(entitlements, dict):
        raise ArtifactVerificationError("codesign entitlements are not a dictionary")
    return entitlements


def inspect_signing(app: Path, *, run: Runner = subprocess.run) -> SigningEvidence:
    """Call codesign for verification and obtain the bundle's signed evidence."""
    app_text = str(app)
    _run_codesign(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", app_text], run
    )
    details = _run_codesign(["codesign", "-dvv", app_text], run)
    authorities = tuple(
        line.partition("=")[2].strip()
        for line in details.splitlines()
        if line.startswith("Authority=")
    )
    entitlements_output = _run_codesign(
        ["codesign", "-d", "--entitlements", ":-", app_text], run
    )
    return SigningEvidence(
        authorities=authorities,
        entitlements=_load_codesign_entitlements(entitlements_output),
    )


def _load_plist(path: Path, *, description: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
    except (ExpatError, OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ArtifactVerificationError(
            f"{description} is missing or malformed"
        ) from error
    if not isinstance(value, dict):
        raise ArtifactVerificationError(f"{description} is not a dictionary")
    return value


def verify_app(
    app: Path,
    *,
    expected_version: str,
    expected_build: str,
    sdk_floor: str,
    expected_bundle_identifier: str = DEFAULT_BUNDLE_IDENTIFIER,
    run: Runner = subprocess.run,
) -> ArtifactIdentity:
    """Verify an unpacked app bundle or the app stored in an Xcode archive."""
    if app.suffix == ".xcarchive" and app.is_dir():
        candidates = sorted((app / "Products" / "Applications").glob("*.app"))
        if len(candidates) != 1:
            raise ArtifactVerificationError(
                "archive does not contain exactly one application"
            )
        application = candidates[0]
    elif app.is_dir() and app.suffix == ".app":
        application = app
    else:
        raise ArtifactVerificationError(
            "--app must name an unpacked .app bundle or .xcarchive"
        )
    info = _load_plist(application / "Info.plist", description="Info.plist")
    manifest = _load_plist(
        application / "PrivacyInfo.xcprivacy", description="privacy manifest"
    )
    signing = inspect_signing(application, run=run)
    return validate_distribution_metadata(
        info,
        manifest,
        signing,
        expected_version=expected_version,
        expected_build=expected_build,
        sdk_floor=sdk_floor,
        expected_bundle_identifier=expected_bundle_identifier,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-build", required=True)
    parser.add_argument("--minimum-ios-sdk", default="26.0")
    parser.add_argument("--expected-bundle-id", default=DEFAULT_BUNDLE_IDENTIFIER)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        identity = verify_app(
            args.app,
            expected_version=args.expected_version,
            expected_build=args.expected_build,
            sdk_floor=args.minimum_ios_sdk,
            expected_bundle_identifier=args.expected_bundle_id,
        )
    except ArtifactVerificationError as error:
        print(f"distribution artifact rejected: {error}")
        return 1
    print(
        "distribution artifact verified: "
        f"bundle={identity.bundle_identifier} version={identity.version} "
        f"build={identity.build} sdk={identity.sdk_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
