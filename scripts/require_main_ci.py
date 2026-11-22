#!/usr/bin/env python3
"""Refuse to publish a release whose exact commit is not green on main CI.

``make check-release-ready`` checks the repository, not the build, and v0.6.57
was cut while main's ci.yml was red. The release workflow runs this before it
builds anything: the tagged commit must have a ci.yml push run on main that
completed successfully. A run still in progress is waited for, bounded by
``--wait-minutes``, so tagging right after a merge does not fail spuriously.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Callable

from ios.require_ios_ci import GitHub, Rejected, items

WORKFLOW = ".github/workflows/ci.yml"
POLL_SECONDS = 30


class Pending(Rejected):
    """CI for the commit has not finished yet; retrying may still pass."""


def main_ci_runs(get: Callable[[str], dict], repository: str, sha: str) -> list:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise Rejected("invalid GitHub repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Rejected("a full release commit SHA is required")
    runs = items(
        get,
        f"/repos/{repository}/actions/workflows/ci.yml/runs",
        "workflow_runs",
        head_sha=sha,
        event="push",
        branch="main",
    )
    # Re-check identity even though the endpoint filters: a PR run, another
    # workflow, or a manual dispatch cannot stand in for main's push CI.
    return [
        run
        for run in runs
        if run.get("head_sha") == sha
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("path", "").split("@", 1)[0] == WORKFLOW
    ]


def require_main_ci(get: Callable[[str], dict], repository: str, sha: str) -> list:
    """Return the passing run ids, or raise Pending/Rejected."""
    runs = main_ci_runs(get, repository, sha)
    if not runs:
        raise Pending(
            f"no ci.yml push run on main for {sha}; release only commits that "
            "have landed on main"
        )
    # The newest attempt decides: a re-run that went green supersedes the
    # original failure, and a newer red run revokes an older green one.
    latest = max(runs, key=lambda run: (run.get("run_number", 0), run.get("id", 0)))
    status, conclusion = latest.get("status"), latest.get("conclusion")
    if status != "completed":
        raise Pending(f"ci.yml run {latest.get('id')} is {status}")
    if conclusion != "success":
        raise Rejected(
            f"ci.yml run {latest.get('id')} for {sha} concluded {conclusion}; "
            "fix main and tag a green commit"
        )
    return [latest["id"]]


def wait_for_main_ci(
    get: Callable[[str], dict],
    repository: str,
    sha: str,
    *,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list:
    deadline = clock() + wait_seconds
    while True:
        try:
            return require_main_ci(get, repository, sha)
        except Pending as pending:
            if clock() >= deadline:
                raise Rejected(f"timed out waiting for CI: {pending}") from None
            print(f"waiting for CI: {pending}", file=sys.stderr)
            sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=os.environ.get("RELEASE_SHA", ""))
    parser.add_argument("--wait-minutes", type=float, default=45.0)
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise Rejected("GITHUB_TOKEN with actions read access is required")
        run_ids = wait_for_main_ci(
            GitHub(token).get,
            os.environ.get("GITHUB_REPOSITORY", ""),
            args.sha,
            wait_seconds=args.wait_minutes * 60,
        )
    except Rejected as error:
        print(f"main CI gate failed: {error}", file=sys.stderr)
        return 1
    print(f"main CI gate passed for {args.sha}; ci.yml runs: {run_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
