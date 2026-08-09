#!/usr/bin/env python3
"""Fail-closed validation for trusted self-hosted runner jobs."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import TextIO

EXPECTED_REPOSITORY = "arniesaha/drover"
EXPECTED_WORKFLOW_REF = (
    "arniesaha/drover/.github/workflows/trusted-mac.yml@refs/heads/main"
)
EXPECTED_OWNER = "arniesaha"


class GuardError(RuntimeError):
    pass


def validate_job(environ: Mapping[str, str], payload: Mapping[str, object]) -> None:
    required = (
        "GITHUB_REPOSITORY",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_ACTOR",
        "GITHUB_EVENT_PATH",
    )
    missing = [key for key in required if not environ.get(key)]
    if missing:
        raise GuardError("required GitHub job metadata is missing")
    if environ["GITHUB_REPOSITORY"] != EXPECTED_REPOSITORY:
        raise GuardError("repository is not allowed")
    if environ["GITHUB_WORKFLOW_REF"] != EXPECTED_WORKFLOW_REF:
        raise GuardError("workflow ref is not allowed")

    repository = payload.get("repository")
    sender = payload.get("sender")
    if (
        not isinstance(repository, Mapping)
        or repository.get("full_name") != EXPECTED_REPOSITORY
    ):
        raise GuardError("event repository does not match")
    if not isinstance(sender, Mapping):
        raise GuardError("event sender is missing")

    event = environ["GITHUB_EVENT_NAME"]
    if event == "push":
        if (
            environ["GITHUB_REF"] != "refs/heads/main"
            or payload.get("ref") != "refs/heads/main"
        ):
            raise GuardError("push ref is not allowed")
        return
    if event == "workflow_dispatch":
        if (
            environ["GITHUB_REF"] != "refs/heads/main"
            or payload.get("ref") != "refs/heads/main"
            or environ["GITHUB_ACTOR"] != EXPECTED_OWNER
            or sender.get("login") != EXPECTED_OWNER
        ):
            raise GuardError("dispatch metadata is not allowed")
        return
    raise GuardError("event is not allowed")


def main(environ: Mapping[str, str] | None = None, stderr: TextIO | None = None) -> int:
    environ = os.environ if environ is None else environ
    stderr = sys.stderr if stderr is None else stderr

    try:
        event_path = environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            raise GuardError("required GitHub job metadata is missing")
        with open(event_path, encoding="utf-8") as event_file:
            payload = json.load(event_file)
        if not isinstance(payload, Mapping):
            raise TypeError("event payload is not an object")
        validate_job(environ, payload)
    except GuardError as exc:
        print(f"pre-job guard rejected: {exc}", file=stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print(
            "pre-job guard rejected: event JSON is invalid or unreadable", file=stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
