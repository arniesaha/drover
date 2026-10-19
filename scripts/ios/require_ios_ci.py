#!/usr/bin/env python3
"""Require successful ios.yml main-push checks for the exact dispatch SHA."""

import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WORKFLOW = ".github/workflows/ios.yml"
REQUIRED_CHECKS = {"Build and test iOS app"}


class Rejected(ValueError):
    pass


class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        request = Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            raise Rejected(
                f"GitHub API returned HTTP {error.code}; actions/checks read access is required"
            ) from None
        except Exception:
            raise Rejected(
                "GitHub API request failed; iOS CI cannot be verified"
            ) from None


def items(get, path, field, **query):
    """Read every page; partial API results must never grant a release."""
    result = []
    for page in range(1, 11):
        response = get(path + "?" + urlencode(query | {"per_page": 100, "page": page}))
        batch = response.get(field)
        total = response.get("total_count")
        if not isinstance(batch, list) or not isinstance(total, int):
            raise Rejected("GitHub API returned invalid check evidence")
        result.extend(batch)
        if len(result) >= total:
            return result
        if not batch:
            break
    raise Rejected("GitHub API results are incomplete; iOS CI cannot be verified")


def require_ios_ci(get, repository, sha):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise Rejected("invalid GitHub repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Rejected("a full dispatch commit SHA is required")
    prefix = f"/repos/{repository}"
    runs = items(
        get,
        prefix + "/actions/workflows/ios.yml/runs",
        "workflow_runs",
        head_sha=sha,
        event="push",
        branch="main",
    )
    # Check identity again even though the endpoint has filters. A PR merge
    # ref, equal Git tree, other workflow or manual run cannot stand in for it.
    runs = [
        run
        for run in runs
        if (
            run.get("head_sha") == sha
            and run.get("event") == "push"
            and run.get("head_branch") == "main"
            and run.get("path", "").split("@", 1)[0] == WORKFLOW
        )
    ]
    if not runs:
        raise Rejected(
            f"missing ios.yml push/main run for exact SHA {sha}; missing checks: "
            + ", ".join(sorted(REQUIRED_CHECKS))
        )
    problems = []
    for run in runs:
        run_id = run.get("id")
        suite = run.get("check_suite_id")
        if not isinstance(run_id, int) or not isinstance(suite, int):
            raise Rejected("ios.yml run is missing its check-suite identity")
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            problems.append(
                f"ios.yml run {run_id}: {run.get('status')}/{run.get('conclusion')}"
            )
        checks = items(
            get,
            prefix + f"/check-suites/{suite}/check-runs",
            "check_runs",
            filter="latest",
        )
        missing = REQUIRED_CHECKS - {check.get("name") for check in checks}
        problems.extend(
            f"missing check: {name} (run {run_id})" for name in sorted(missing)
        )
        for check in checks:
            name = json.dumps(str(check.get("name", "unnamed check")))
            if (
                check.get("head_sha") != sha
                or check.get("check_suite", {}).get("id") != suite
                or check.get("app", {}).get("slug") != "github-actions"
            ):
                problems.append(
                    f"{name}: wrong SHA, suite or check owner (run {run_id})"
                )
            elif (
                check.get("status") != "completed"
                or check.get("conclusion") != "success"
            ):
                problems.append(
                    f"{name}: {check.get('status')}/{check.get('conclusion')} (run {run_id})"
                )
    if problems:
        raise Rejected(
            f"iOS CI is not successful for exact SHA {sha}: " + "; ".join(problems)
        )
    return [run["id"] for run in runs]


def main():
    try:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise Rejected("GITHUB_TOKEN is required with actions/checks read access")
        sha = os.environ.get("GITHUB_SHA", "")
        run_ids = require_ios_ci(
            GitHub(token).get,
            os.environ.get("GITHUB_REPOSITORY", ""),
            sha,
        )
        print(
            f"iOS CI gate passed for exact SHA {sha}; ios.yml main-push runs: {run_ids}"
        )
        return 0
    except Rejected as error:
        print(f"iOS CI gate failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("iOS CI gate failed: invalid GitHub check evidence", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
