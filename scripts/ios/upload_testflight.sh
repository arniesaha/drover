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


def diagnostic_codes(value):
    """Return only bounded, non-message identifiers from altool JSON."""
    found = set()
    named = {
        "AUTHENTICATION_ERROR",
        "FORBIDDEN",
        "INVALID_REQUEST",
        "NOT_AUTHORIZED",
        "UNAUTHORIZED",
    }

    def visit(item, depth=0):
        if depth > 12 or len(found) >= 20:
            return
        if isinstance(item, dict):
            for key, child in list(item.items())[:100]:
                normalized = key.lower().replace("_", "").replace("-", "")
                if normalized in {"code", "status", "statuscode"}:
                    candidate = str(child) if isinstance(child, (str, int)) else ""
                    if (
                        re.fullmatch(r"-?\d{1,10}", candidate)
                        or re.fullmatch(r"[A-Z]{2,12}-\d{3,8}", candidate)
                        or re.fullmatch(
                            r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\.\d{3,8}",
                            candidate,
                        )
                        or candidate in named
                    ):
                        found.add(candidate)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:100]:
                visit(child, depth + 1)

    visit(value)
    return sorted(found)


def stderr_diagnostic_codes(value):
    """Extract only explicitly structured identifiers, never free-form text."""
    found = set()
    for pattern in (
        r"\bCode=(-?\d{1,10})\b",
        r"\bstatusCode\s*=\s*(\d{3})\b",
        r'"code"\s*:\s*"([A-Z]{2,12}-\d{3,8})"',
    ):
        found.update(re.findall(pattern, value))
    return sorted(found)[:20]


def write_receipt(path, receipt):
    with path.open("x") as output:
        output.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


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
        errors_path = Path(temporary) / "stderr.log"
        with raw.open("wb") as output, errors_path.open("wb") as errors:
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
        try:
            response = json.loads(raw.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = None
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
        product_errors = response.get("product-errors") if isinstance(response, dict) else None
        if result.returncode != 0 or not confirmed or product_errors:
            codes = set(diagnostic_codes(response))
            codes.update(
                stderr_diagnostic_codes(
                    errors_path.read_text(errors="ignore")[:1024 * 1024]
                )
            )
            write_receipt(
                args.record,
                {
                    "diagnostic_codes": sorted(codes)[:20],
                    "ipa_sha256": digest,
                    "tool_exit_code": result.returncode,
                    "upload_confirmed": False,
                },
            )
            raise Rejected("upload rejected; sanitized failure receipt written")
    with args.ipa.open("rb") as artifact:
        require(
            hashlib.file_digest(artifact, "sha256").hexdigest() == digest,
            "IPA changed during upload; receipt withheld",
        )
    receipt = {"upload_confirmed": True, "ipa_sha256": digest}
    write_receipt(args.record, receipt)
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
