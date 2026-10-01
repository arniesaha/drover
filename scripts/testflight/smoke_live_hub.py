#!/usr/bin/env python3
"""Read-only, bounded smoke check of the operator's live hub before TestFlight."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PATHS = ("/healthz", "/readyz", "/harness/hosts", "/harness/sessions")


class Rejected(ValueError):
    """Fixed diagnostics only; never include URLs, credentials or responses."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("arguments_invalid")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def smoke(url, token, budget=20.0):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or any(c in url for c in "?#\\")
            or any(c.isspace() or ord(c) < 32 for c in url)
            or parts.netloc.endswith(":")
            or (parts.port is not None and not 1 <= parts.port <= 65535)
        ):
            raise ValueError
        origin = url.rstrip("/")
    except Exception:
        raise Rejected("url_invalid") from None
    if not token or not all(32 < ord(c) < 127 for c in token):
        raise Rejected("credential_invalid")
    if not math.isfinite(budget) or budget <= 0:
        raise Rejected("budget_invalid")
    deadline = time.monotonic() + budget
    # No proxy or redirect can send this bearer token to another origin.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    analytical = "unreported"
    for path in PATHS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Rejected("budget_exceeded")
        headers = {"Accept": "application/json"}
        if path != "/healthz":
            headers["Authorization"] = f"Bearer {token}"
        request = Request(origin + path, headers=headers, method="GET")
        try:
            with opener.open(request, timeout=remaining) as response:
                if response.status != 200:
                    raise Rejected("request_failed")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if path == "/healthz":
                    # Only allow known values into output, even if a server
                    # echoes a credential into this header.
                    value = response.headers.get("X-Drover-Analytical")
                    analytical = (
                        value
                        if value in {"ok", "failed-retrying", "recovering"}
                        else "unreported"
                    )
        except Rejected:
            raise
        except Exception:
            if time.monotonic() >= deadline:
                raise Rejected("budget_exceeded") from None
            raise Rejected("request_failed") from None
        if time.monotonic() >= deadline:
            raise Rejected("budget_exceeded")
        if len(body) > MAX_RESPONSE_BYTES:
            raise Rejected("response_too_large")
        if path == "/healthz":
            if body not in {b"ok", b"ok\n"}:
                raise Rejected("health_invalid")
        elif path == "/readyz":
            try:
                ready = json.loads(body)
            except Rejected:
                raise
            except Exception:
                raise Rejected("readiness_invalid") from None
            if not isinstance(ready, dict) or ready.get("ready") is not True:
                raise Rejected("not_ready")
    return {"ok": True, "checks": len(PATHS), "analytical": analytical}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument(
            "--url", default=os.environ.get("DROVER_TESTFLIGHT_HUB_URL")
        )
        parser.add_argument("--budget-seconds", type=float, default=20.0)
        args = parser.parse_args(argv)
        if not math.isfinite(args.budget_seconds) or args.budget_seconds <= 0:
            raise Rejected("budget_invalid")

        # A socket timeout alone is an idle timeout: trickling headers/body or
        # slow DNS could exceed it. The CLI also enforces a total wall deadline
        # on macOS/Linux, including DNS, TLS, headers and response bodies.
        def expired(signum, frame):
            raise Rejected("budget_exceeded")

        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.budget_seconds)
        try:
            result = smoke(
                args.url,
                os.environ.get("DROVER_TESTFLIGHT_HUB_TOKEN"),
                args.budget_seconds,
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    except Rejected as error:
        print(f"live hub smoke failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("live hub smoke failed: request_failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
