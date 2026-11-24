#!/usr/bin/env python3
"""Capture fail-closed altool validation response shape without uploading."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

MAX_RAW_BYTES = 1024 * 1024
KEY_ID = re.compile(r"[A-Z0-9]{8,20}")
ISSUER = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")
TOOL_VERSION = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")


class Rejected(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Rejected(message)


def private_directory(path: Path) -> None:
    require(
        path.is_absolute() and path.is_dir() and not path.is_symlink(),
        "invalid private directory",
    )
    require(
        stat.S_IMODE(path.stat().st_mode) == 0o700, "private directory mode is unsafe"
    )


def private_key(path: Path) -> None:
    require(path.is_file() and not path.is_symlink(), "private key is unavailable")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "private key mode is unsafe")
    require(path.stat().st_size > 0, "private key is unavailable")


def output_type(value: object) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def collection_shape(response: object, field: str) -> str:
    if not isinstance(response, dict):
        return "unavailable"
    if field not in response:
        return "absent"
    value = response[field]
    if value is None:
        return "null"
    if not isinstance(value, list):
        return "non-list"
    return "empty-list" if not value else "nonempty-list"


def success_message_shape(response: object) -> dict[str, str]:
    if not isinstance(response, dict) or "success-message" not in response:
        return {"presence": "absent", "type": "absent", "class": "absent"}
    value = response["success-message"]
    if not isinstance(value, str):
        return {"presence": "present", "type": "non-string", "class": "non-string"}
    return {
        "presence": "present",
        "type": "string",
        "class": (
            "no-errors-validating-prefix"
            if re.match(r"^No errors validating\b", value.strip())
            else "other-string"
        ),
    }


def read_json(path: Path) -> tuple[object | None, str]:
    if path.stat().st_size > MAX_RAW_BYTES:
        return None, "oversize"
    try:
        return json.loads(path.read_bytes()), "parsed"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"


def tool_version(path: Path) -> str:
    if path.stat().st_size > 4096:
        return "unknown"
    match = TOOL_VERSION.search(path.read_text(errors="replace"))
    return match.group(1) if match else "unknown"


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(receipt, output, indent=2, sort_keys=True)
        output.write("\n")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture only fixed-shape validation evidence; never uploads."
    )
    parser.add_argument("--ipa", type=Path, required=True)
    parser.add_argument("--api-key-id", required=True)
    parser.add_argument("--api-issuer", required=True)
    parser.add_argument("--private-keys-dir", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--private-output-dir", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_arguments()
        require(
            KEY_ID.fullmatch(args.api_key_id) is not None, "invalid API key identifier"
        )
        require(ISSUER.fullmatch(args.api_issuer) is not None, "invalid API issuer")
        require(SHA.fullmatch(args.source_sha) is not None, "invalid source SHA")
        require(VERSION.fullmatch(args.version) is not None, "invalid version")
        require(VERSION.fullmatch(args.build) is not None, "invalid build")
        require(
            args.ipa.is_absolute()
            and args.ipa.is_file()
            and not args.ipa.is_symlink()
            and args.ipa.suffix == ".ipa",
            "IPA is unavailable",
        )
        require(
            args.record.is_absolute()
            and args.record.parent.is_dir()
            and not args.record.exists()
            and not args.record.is_symlink(),
            "receipt path is unavailable",
        )
        private_directory(args.private_keys_dir)
        private_directory(args.private_output_dir)
        key = args.private_keys_dir / f"AuthKey_{args.api_key_id}.p8"
        private_key(key)

        environment = os.environ | {"API_PRIVATE_KEYS_DIR": str(args.private_keys_dir)}
        version_output = args.private_output_dir / "altool-version.out"
        response_output = args.private_output_dir / "validation-response.json"
        error_output = args.private_output_dir / "validation-stderr.log"
        with version_output.open("xb") as stdout, error_output.open("xb") as stderr:
            subprocess.run(
                ["xcrun", "altool", "--version"],
                stdout=stdout,
                stderr=stderr,
                env=environment,
                check=False,
            )
        with response_output.open("xb") as stdout, error_output.open("ab") as stderr:
            result = subprocess.run(
                [
                    "xcrun",
                    "altool",
                    "--validate-app",
                    "-f",
                    str(args.ipa),
                    "--api-key",
                    args.api_key_id,
                    "--api-issuer",
                    args.api_issuer,
                    "--output-format",
                    "json",
                ],
                stdout=stdout,
                stderr=stderr,
                env=environment,
                cwd=args.private_output_dir,
                check=False,
            )
        response, parse_state = read_json(response_output)
        response_shape = (
            parse_state if parse_state != "parsed" else output_type(response)
        )
        errors_shape = collection_shape(response, "product-errors")
        warnings_shape = collection_shape(response, "product-warnings")
        confirmation = success_message_shape(response)
        if result.returncode != 0:
            result_class = "nonzero-tool-exit"
        elif parse_state != "parsed":
            result_class = "malformed-response"
        elif errors_shape == "nonempty-list":
            result_class = "product-errors-present"
        elif errors_shape == "non-list" or warnings_shape == "non-list":
            result_class = "malformed-collection"
        else:
            result_class = "positive-contract-unconfirmed"
        write_receipt(
            args.record,
            {
                "action": "validation-only-contract-evidence",
                "build": args.build,
                "exit_class": "zero" if result.returncode == 0 else "nonzero",
                "no_upload": True,
                "product_errors_shape": errors_shape,
                "product_warnings_shape": warnings_shape,
                "response_type": response_shape,
                "result_class": result_class,
                "source_sha": args.source_sha,
                "success_message": confirmation,
                "tool_version": tool_version(version_output),
                "validation_confirmed": False,
                "version": args.version,
            },
        )
    except Rejected as error:
        print(f"validation-only evidence rejected: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "validation-only evidence failed; private diagnostics discarded",
            file=sys.stderr,
        )
        return 1
    print(
        "validation-only evidence captured; validation remains unconfirmed",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
