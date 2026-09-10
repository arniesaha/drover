#!/usr/bin/env bash
# Confirm upload and retain a fixed-schema receipt, never altool's raw response.
set +x
set -euo pipefail
umask 077
exec python3 - "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


class Rejected(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("invalid upload arguments; use --help")


def require(condition, message):
    if not condition:
        raise Rejected(message)


def check_private(path, directory=False):
    require(
        path.is_absolute() and not path.is_symlink(),
        "private key material is unavailable",
    )
    metadata = path.stat()
    require(
        metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077,
        "private key directory and file must be owner-only",
    )
    require(
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode),
        "private key material is unavailable",
    )


def main():
    parser = Parser(
        description="Upload a verified IPA; confirmation does not imply Apple processing or device acceptance."
    )
    parser.add_argument("--ipa", type=Path, required=True)
    parser.add_argument("--api-key-id", required=True)
    parser.add_argument("--api-issuer", required=True)
    parser.add_argument("--private-keys-dir", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    require(
        re.fullmatch(r"[A-Z0-9]{8,20}", args.api_key_id), "invalid API key identifier"
    )
    require(
        re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", args.api_issuer
        ),
        "invalid API issuer identifier",
    )
    require(
        args.ipa.is_absolute()
        and args.ipa.is_file()
        and not args.ipa.is_symlink()
        and args.ipa.suffix == ".ipa",
        "IPA is unavailable",
    )
    require(
        args.record.is_absolute() and args.record.parent.is_dir(),
        "receipt parent is unavailable",
    )
    require(
        not args.record.exists() and not args.record.is_symlink(),
        "receipt already exists",
    )
    check_private(args.private_keys_dir, directory=True)
    key = args.private_keys_dir / f"AuthKey_{args.api_key_id}.p8"
    check_private(key)
    require(key.stat().st_size > 0, "private key material is unavailable")
    with args.ipa.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    environment = os.environ | {"API_PRIVATE_KEYS_DIR": str(args.private_keys_dir)}
    with tempfile.TemporaryDirectory(prefix="drover-upload-") as temporary:
        # altool searches ./private_keys before API_PRIVATE_KEYS_DIR and home.
        # Pin that first lookup without reading or modifying ambient credentials.
        (Path(temporary) / "private_keys").symlink_to(
            args.private_keys_dir, target_is_directory=True
        )
        # Separate stderr so it cannot corrupt the JSON; both files are private
        # and removed on success, malformed output, or a nonzero tool exit.
        raw = Path(temporary) / "response.json"
        with raw.open("wb") as output, (Path(temporary) / "stderr.log").open(
            "wb"
        ) as errors:
            result = subprocess.run(
                [
                    "xcrun",
                    "altool",
                    "--upload-app",
                    "-f",
                    str(args.ipa),
                    "--api-key",
                    args.api_key_id,
                    "--api-issuer",
                    args.api_issuer,
                    "--output-format",
                    "json",
                ],
                stdout=output,
                stderr=errors,
                env=environment,
                cwd=temporary,
                check=False,
            )
        require(result.returncode == 0, "upload failed; private diagnostics discarded")
        response = json.loads(raw.read_text())
        # altool has punctuated this line differently across Xcode releases --
        # "archive", the full path, the basename, with and without a trailing
        # period. An exact-match set fails the run *after* the bytes are at
        # Apple: no receipt is written, and the operator's natural retry
        # becomes a second build. Match the invariant part, and keep the
        # product-errors check as the real gate.
        message = response.get("success-message") if isinstance(response, dict) else None
        confirmed = isinstance(message, str) and re.match(
            r"^No errors uploading\b", message.strip()
        )
        require(
            bool(confirmed) and not response.get("product-errors"),
            "upload confirmation was not received",
        )
    with args.ipa.open("rb") as artifact:
        require(
            hashlib.file_digest(artifact, "sha256").hexdigest() == digest,
            "IPA changed during upload; receipt withheld",
        )
    receipt = {"upload_confirmed": True, "ipa_sha256": digest}
    with args.record.open("x") as output:
        output.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(
        "upload confirmed; Apple processing and physical-device acceptance remain pending"
    )


try:
    main()
except Rejected as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
except Exception:
    print("upload failed; private diagnostics discarded", file=sys.stderr)
    raise SystemExit(1)
PY
