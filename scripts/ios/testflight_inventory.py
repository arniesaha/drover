#!/usr/bin/env python3
"""Write a bounded, sanitized GET-only App Store Connect inventory."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from testflight_api import Client, Rejected, inventory


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("invalid inventory arguments")


def require(condition):
    if not condition:
        raise Rejected("inventory prerequisites are unavailable")


def private_file(path: Path) -> None:
    require(path.is_absolute() and path.is_file() and not path.is_symlink())
    metadata = path.stat()
    require(
        metadata.st_uid == os.getuid()
        and stat.S_ISREG(metadata.st_mode)
        and not metadata.st_mode & 0o077
        and metadata.st_size > 0
    )


def safe_text(value) -> str:
    require(
        isinstance(value, str)
        and value.strip()
        and len(value) <= 191
        and not any(ord(character) < 32 for character in value)
    )
    return value


def allowlisted_record(record: dict) -> dict:
    require(isinstance(record, dict))
    require(set(record) == {"versions", "builds", "internal_groups"})
    require(all(isinstance(record[field], list) for field in record))
    versions = []
    for row in record["versions"]:
        require(isinstance(row, dict) and set(row) == {"version"})
        versions.append({"version": safe_text(row["version"])})
    builds = []
    for row in record["builds"]:
        require(
            isinstance(row, dict)
            and set(row) == {"version", "build", "processing_state"}
        )
        builds.append(
            {
                "version": safe_text(row["version"]),
                "build": safe_text(row["build"]),
                "processing_state": safe_text(row["processing_state"]),
            }
        )
    groups = []
    for row in record["internal_groups"]:
        require(isinstance(row, dict) and set(row) == {"name"})
        groups.append({"name": safe_text(row["name"])})
    return {"versions": versions, "builds": builds, "internal_groups": groups}


def write_record(path: Path, record: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and not path.exists())
    record = allowlisted_record(record)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(record, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")


def main(argv=None):
    parser = Parser(
        description="Read existing iOS TestFlight inventory without mutation."
    )
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--api-key-id", required=True)
    parser.add_argument("--api-issuer", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,190}", args.bundle_id))
        require(re.fullmatch(r"[A-Z0-9]{8,20}", args.api_key_id))
        require(
            re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                args.api_issuer,
            )
        )
        private_file(args.private_key)
        record = inventory(
            Client(args.private_key, args.api_key_id, args.api_issuer), args.bundle_id
        )
        write_record(args.output, record)
    except Exception:
        try:
            if args.output.exists() and not args.output.is_symlink():
                args.output.unlink()
        except Exception:
            pass
        raise SystemExit(
            "App Store Connect inventory unavailable; private diagnostics discarded"
        )


if __name__ == "__main__":
    main()
