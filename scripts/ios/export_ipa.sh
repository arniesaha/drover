#!/usr/bin/env bash
# Export a stage-locked internal candidate; retain only the IPA and safe evidence.
set +x
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 - "$SCRIPT_DIR" "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, sys.argv.pop(1))
from verify_archive import (
    ArtifactVerificationError,
    VERSION_PATTERN,
    inspect_signing,
    normalize_staging_url,
    verify_app,
)


class Rejected(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("invalid export arguments; use --help")


def require(condition, message):
    if not condition:
        raise Rejected(message)


def private(path, directory=False):
    require(
        path.is_absolute() and not path.is_symlink(),
        "private export options are unavailable",
    )
    metadata = path.stat()
    require(
        metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077,
        "export options must be owner-only",
    )
    require(
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode),
        "private export options are unavailable",
    )


def invoke(command, log, message):
    with log.open("wb") as output:
        completed = subprocess.run(
            command, stdout=output, stderr=subprocess.STDOUT, check=False
        )
    require(completed.returncode == 0, message)


def verified(app, args, staging_url):
    try:
        return verify_app(
            app,
            expected_version=args.version,
            expected_build=args.build,
            sdk_floor="26.0",
            expected_staging_url=staging_url,
        )
    except ArtifactVerificationError as error:
        raise Rejected("signed artifact verification failed") from error


def export_options(path, archive, identity, scratch):
    private(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        private(path)
        options = plistlib.loads(path.read_bytes())
    else:
        app = next((archive / "Products" / "Applications").glob("*.app"))
        # Decode only the already-verified archive's public provisioning metadata.
        profile_log = scratch / "profile.plist"
        invoke(
            ["security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")],
            profile_log,
            "archive provisioning metadata is unavailable",
        )
        profile = plistlib.loads(profile_log.read_bytes())
        signing = inspect_signing(app)
        team = signing.entitlements.get("com.apple.developer.team-identifier", "")
        profile_id = profile.get("UUID", "")
        require(
            re.fullmatch(r"[A-Z0-9]{10}", team) is not None
            and profile.get("TeamIdentifier") == [team]
            and profile.get("Entitlements", {}).get("application-identifier")
            == f"{team}.{identity.bundle_identifier}"
            and re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", profile_id
            )
            is not None,
            "archive provisioning metadata does not match signed application",
        )
        authority = next(
            a for a in signing.authorities if a.startswith("Apple Distribution:")
        )
        options = {
            "method": "app-store-connect",
            "destination": "export",
            "signingStyle": "manual",
            "teamID": team,
            "signingCertificate": authority,
            "provisioningProfiles": {identity.bundle_identifier: profile_id},
            "manageAppVersionAndBuildNumber": False,
            "testFlightInternalTestingOnly": True,
        }
        with path.open("xb") as output:
            plistlib.dump(options, output)
    require(
        isinstance(options, dict)
        and options.get("method") == "app-store-connect"
        and options.get("destination") == "export"
        and options.get("signingStyle") == "manual"
        and options.get("testFlightInternalTestingOnly") is True
        and options.get("manageAppVersionAndBuildNumber") is False,
        "export options must select manual internal App Store Connect export with fixed version/build",
    )
    team = options.get("teamID")
    certificate = options.get("signingCertificate")
    profiles = options.get("provisioningProfiles")
    require(
        isinstance(team, str)
        and re.fullmatch(r"[A-Z0-9]{10}", team)
        and isinstance(certificate, str)
        and (
            re.fullmatch(r"[0-9a-fA-F]{40}", certificate)
            or (
                certificate.startswith("Apple Distribution: ")
                and certificate.endswith(f"({team})")
            )
        )
        and isinstance(profiles, dict)
        and isinstance(profiles.get(identity.bundle_identifier), str)
        and re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            profiles[identity.bundle_identifier],
        ),
        "export options require explicit team, certificate and provisioning profile references",
    )


def main():
    parser = Parser(
        description="Export and verify an internal TestFlight IPA. A missing export-options file is generated in its existing owner-only signing directory."
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-options", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--staging-url", required=True)
    args = parser.parse_args()
    require(
        VERSION_PATTERN.fullmatch(args.version)
        and VERSION_PATTERN.fullmatch(args.build),
        "version and build must be expanded numeric values",
    )
    staging_url = normalize_staging_url(args.staging_url)
    require(args.output.is_absolute(), "output must be an absolute path")
    require(
        not args.output.exists() and not args.output.is_symlink(),
        "output directory already exists",
    )
    require(args.output.parent.is_dir(), "output parent directory does not exist")
    require(
        args.archive.is_absolute()
        and args.archive.is_dir()
        and not args.archive.is_symlink()
        and args.archive.suffix == ".xcarchive",
        "archive is unavailable",
    )
    private(args.export_options.parent, directory=True)
    if args.export_options.exists() or args.export_options.is_symlink():
        private(args.export_options)
    # All raw Xcode output, export sidecars and unpacked signed data stay temporary.
    with tempfile.TemporaryDirectory(
        prefix="drover-ipa-", dir=args.export_options.parent
    ) as temporary:
        scratch = Path(temporary)
        identity = verified(args.archive, args, staging_url)
        export_options(args.export_options, args.archive, identity, scratch)
        exported = scratch / "export"
        exported.mkdir()
        invoke(
            [
                "xcodebuild",
                "-exportArchive",
                "-archivePath",
                str(args.archive),
                "-exportOptionsPlist",
                str(args.export_options),
                "-exportPath",
                str(exported),
            ],
            scratch / "export.log",
            "IPA export failed",
        )
        ipas = list(exported.rglob("*.ipa"))
        require(
            len(ipas) == 1 and ipas[0].is_file() and not ipas[0].is_symlink(),
            "export must contain exactly one IPA",
        )
        unpacked = scratch / "unpacked"
        unpacked.mkdir()
        invoke(
            ["ditto", "-x", "-k", str(ipas[0]), str(unpacked)],
            scratch / "unpack.log",
            "IPA unpacking failed",
        )
        apps = list((unpacked / "Payload").glob("*.app"))
        require(
            len(apps) == 1 and not apps[0].is_symlink(),
            "IPA must contain exactly one application",
        )
        identity = verified(apps[0], args, staging_url)
        with ipas[0].open("rb") as artifact:
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
        record = {
            "version": identity.version,
            "build": identity.build,
            "bundle_identifier": identity.bundle_identifier,
            "ipa_sha256": digest,
            "staging_url_sha256": hashlib.sha256(staging_url.encode()).hexdigest(),
        }
        args.output.mkdir(mode=0o700)
        shutil.copyfile(ipas[0], args.output / "Drover.ipa")
        with (args.output / "export-record.json").open("x") as output:
            output.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(json.dumps(record, sort_keys=True))


try:
    main()
except (Rejected, ArtifactVerificationError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
except Exception:
    # Never expose paths, provisioning material, tool output or exception payloads.
    print("IPA export failed; private diagnostics discarded", file=sys.stderr)
    raise SystemExit(1)
PY
