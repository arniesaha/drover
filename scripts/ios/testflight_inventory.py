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

from testflight_api import (
    APPLE_ERROR_CATEGORIES,
    APPLE_ERROR_REASONS,
    Client,
    InventoryFailure,
    Rejected,
    inventory,
    prerelease_access_probe,
)

DIAGNOSTIC_STAGES = frozenset(
    {
        "arguments",
        "credential",
        "client",
        "inventory",
        "apps",
        "pre-release-versions",
        "builds",
        "internal-groups",
        "output",
    }
)
DIAGNOSTIC_CATEGORIES = frozenset(
    {"http", "malformed", "network", "rejected", "temporary", "unknown"}
)
DIAGNOSTIC_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504}
)
PROBE_CONTRACTS = frozenset({"global-filtered", "app-scoped"})
PROBE_RESULTS = frozenset({"ok", "rejected"})
KEY_IDENTITY_RESULTS = frozenset({"matches", "mismatch", "not-compared"})


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


def safe_diagnostic(error: Exception, fallback_stage: str) -> dict:
    """Return only fixed diagnostic fields, never exception text or API data."""
    stage = getattr(error, "inventory_stage", fallback_stage)
    category = getattr(error, "diagnostic_category", "unknown")
    status = getattr(error, "http_status", None)
    result = {
        "stage": stage if stage in DIAGNOSTIC_STAGES else "inventory",
        "error_category": (
            category if category in DIAGNOSTIC_CATEGORIES else "unknown"
        ),
    }
    if status in DIAGNOSTIC_HTTP_STATUSES:
        result["http_status"] = status
    apple = getattr(error, "apple_error_category", None)
    if apple in APPLE_ERROR_CATEGORIES:
        result["apple_error_category"] = apple
    reason = getattr(error, "apple_error_reason", None)
    if reason in APPLE_ERROR_REASONS:
        result["apple_error_reason"] = reason
    return result


def write_diagnostic(path: Path, diagnostic: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and not path.exists())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(diagnostic, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")


def allowlisted_probe(record: dict) -> dict:
    require(
        isinstance(record, dict)
        and set(record) == {"probe", "configured_key_identity", "contracts"}
    )
    require(record["probe"] == "pre-release-access")
    require(record["configured_key_identity"] in KEY_IDENTITY_RESULTS)
    require(isinstance(record["contracts"], list) and len(record["contracts"]) == 2)
    contracts = []
    for row in record["contracts"]:
        require(isinstance(row, dict) and row.get("contract") in PROBE_CONTRACTS)
        require(row.get("result") in PROBE_RESULTS)
        allowed = {"contract", "result"}
        if row["result"] == "rejected":
            allowed.add("error_category")
            require(row.get("error_category") in DIAGNOSTIC_CATEGORIES)
            if "http_status" in row:
                allowed.add("http_status")
                require(row["http_status"] in DIAGNOSTIC_HTTP_STATUSES)
            if "apple_error_category" in row:
                allowed.add("apple_error_category")
                require(row["apple_error_category"] in APPLE_ERROR_CATEGORIES)
            if "apple_error_reason" in row:
                allowed.add("apple_error_reason")
                require(row["apple_error_reason"] in APPLE_ERROR_REASONS)
        require(set(row) == allowed)
        contracts.append({key: row[key] for key in sorted(row)})
    require({row["contract"] for row in contracts} == PROBE_CONTRACTS)
    return {
        "probe": "pre-release-access",
        "configured_key_identity": record["configured_key_identity"],
        "contracts": contracts,
    }


def write_probe(path: Path, probe: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and not path.exists())
    probe = allowlisted_probe(probe)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(probe, output, sort_keys=True, separators=(",", ":"))
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
    parser.add_argument("--diagnostic-output", type=Path)
    parser.add_argument("--prerelease-access-probe", action="store_true")
    parser.add_argument("--expected-key-id")
    parser.add_argument("--expected-key-id-env", action="store_true")
    args = None
    stage = "arguments"
    try:
        args = parser.parse_args(argv)
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,190}", args.bundle_id))
        require(re.fullmatch(r"[A-Z0-9]{8,20}", args.api_key_id))
        require(
            re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                args.api_issuer,
            )
        )
        stage = "credential"
        private_file(args.private_key)
        stage = "client"
        client = Client(args.private_key, args.api_key_id, args.api_issuer)
        stage = "inventory"
        if args.prerelease_access_probe:
            require(args.diagnostic_output)
            require(not (args.expected_key_id and args.expected_key_id_env))
            expected_key_id = (
                os.environ.get("EXPECTED_KEY_ID")
                if args.expected_key_id_env
                else args.expected_key_id
            ) or None
            if expected_key_id is not None:
                require(re.fullmatch(r"[A-Z0-9]{8,20}", expected_key_id))
            probe = prerelease_access_probe(
                client,
                args.bundle_id,
                configured_key_id=args.api_key_id,
                expected_key_id=expected_key_id,
            )
            stage = "output"
            write_probe(args.diagnostic_output, probe)
            return
        record = inventory(client, args.bundle_id)
        stage = "output"
        write_record(args.output, record)
    except Exception as error:
        try:
            if args and args.output.exists() and not args.output.is_symlink():
                args.output.unlink()
        except Exception:
            pass
        diagnostic = safe_diagnostic(error, stage)
        try:
            if args and args.diagnostic_output:
                write_diagnostic(args.diagnostic_output, diagnostic)
        except Exception:
            pass
        details = " ".join(f"{key}={value}" for key, value in diagnostic.items())
        raise SystemExit(
            f"App Store Connect inventory failed ({details}); private diagnostics discarded"
        )


if __name__ == "__main__":
    main()
