#!/usr/bin/env python3
"""Read-only HTTPS gate for the protected internal TestFlight workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ios"))
from verify_archive import normalize_staging_url  # noqa: E402

HOST_ID = "testflight-staging-mac-mini"
STRUCTURED_HARNESSES = {"claude-code", "codex", "agy", "deepseek"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class Rejected(ValueError):
    """Only fixed public categories may cross this boundary."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("stage_arguments_invalid")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, category):
    if not condition:
        raise Rejected(category)


def check_host(payload):
    hosts = payload.get("hosts")
    require(isinstance(hosts, list), "stage_host_unavailable")
    matches = [
        host
        for host in hosts
        if isinstance(host, dict) and host.get("host_id") == HOST_ID
    ]
    require(
        len(matches) == 1 and matches[0].get("status") == "online",
        "stage_host_unavailable",
    )
    capabilities = matches[0].get("capabilities")
    require(isinstance(capabilities, dict), "stage_harness_unavailable")
    harnesses = capabilities.get("harnesses")
    require(
        isinstance(harnesses, list)
        and any(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"] in STRUCTURED_HARNESSES
            and item.get("enabled") is True
            for item in harnesses
        ),
        "stage_harness_unavailable",
    )


def verify(url, token, expected_sha):
    try:
        origin = normalize_staging_url(url)
    except Exception:
        raise Rejected("stage_url_invalid") from None
    require(
        isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", expected_sha),
        "stage_arguments_invalid",
    )
    require(
        isinstance(token, str) and token and all(32 < ord(c) < 127 for c in token),
        "stage_credential_invalid",
    )
    # No ambient proxy, cookies, authentication retries, or redirect target may
    # change where this single-origin bearer credential is sent.
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(path):
        request = Request(
            origin + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=10) as response:
                require(response.status == 200, "stage_request_failed")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raise Rejected("stage_request_failed") from None
        try:
            require(len(body) <= MAX_RESPONSE_BYTES, "stage_response_invalid")
            result = json.loads(body)
            require(isinstance(result, dict), "stage_response_invalid")
            return result
        except Exception:
            raise Rejected("stage_response_invalid") from None

    identity = fetch("/release-identity")
    version = identity.get("package_version")
    require(
        identity.get("role") == "testflight-staging"
        and identity.get("source_sha") == expected_sha
        and isinstance(version, str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version),
        "stage_identity_mismatch",
    )
    require(fetch("/readyz").get("ready") is True, "stage_not_ready")
    check_host(fetch("/harness/hosts"))
    check_host(fetch("/harness"))
    probe = identity.get("staging_probe")
    require(isinstance(probe, dict), "stage_probe_invalid")
    require(
        probe.get("source_sha") == expected_sha
        and probe.get("host_id") == HOST_ID
        and isinstance(probe.get("session_id_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", probe["session_id_sha256"]),
        "stage_probe_invalid",
    )
    try:
        timestamp = datetime.fromisoformat(probe["completed_at"].replace("Z", "+00:00"))
        require(
            timestamp.tzinfo is not None and timestamp.utcoffset() is not None,
            "stage_probe_invalid",
        )
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    except Exception:
        raise Rejected("stage_probe_invalid") from None
    require(0 <= age <= 30 * 60, "stage_probe_stale")
    return {
        "source_sha": expected_sha,
        "package_version": version,
        "role": "testflight-staging",
        "probe_completed_at": timestamp.isoformat(),
        "host_id": HOST_ID,
        "staging_url_sha256": hashlib.sha256(origin.encode()).hexdigest(),
    }


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("DROVER_TESTFLIGHT_STAGING_URL")
    )
    parser.add_argument(
        "--token", default=os.environ.get("DROVER_TESTFLIGHT_PREFLIGHT_TOKEN")
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--record", type=Path, required=True)
    record_path = None
    status = 1
    try:
        args = parser.parse_args(argv)
        record_path = args.record
        record = verify(args.url, args.token, args.expected_sha)
        status = 0
    except Rejected as error:
        record = {"category": str(error)}
    except Exception:
        record = {"category": "stage_preflight_failed"}
    if record_path is not None:
        try:
            # Never overwrite an earlier candidate's gate record or follow a link.
            descriptor = os.open(
                record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                output.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
        except Exception:
            record = {"category": "stage_record_failed"}
            status = 1
    if status:
        print(record["category"], file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
