"""Contracts that keep public PR checks separate from trusted Mac work."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS_DIR = Path(__file__).parents[1] / ".github" / "workflows"
TRUSTED_RUNNER = ["self-hosted", "macOS", "ARM64", "drover-ci"]


def load_workflow(name: str) -> dict[str, Any]:
    """Load a workflow without YAML 1.1 coercing the ``on`` key."""
    path = WORKFLOWS_DIR / name
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_public_pr_workflows_stay_github_hosted() -> None:
    python_ci = load_workflow("ci.yml")
    ios_ci = load_workflow("ios.yml")

    assert python_ci["permissions"] == {"contents": "read"}
    assert ios_ci["permissions"] == {"contents": "read"}
    assert python_ci["jobs"]["build-and-test"]["runs-on"] == "ubuntu-latest"
    assert ios_ci["jobs"]["build-and-test"]["runs-on"] == "macos-15"
    assert "pull_request" in python_ci["on"]
    assert "pull_request" in ios_ci["on"]
    assert "workflow_dispatch" in python_ci["on"]


def test_trusted_workflow_has_no_pull_request_trigger() -> None:
    workflow = load_workflow("trusted-mac.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "${{ github.ref }}" in workflow["concurrency"]["group"]
    for job in workflow["jobs"].values():
        assert job["runs-on"] == TRUSTED_RUNNER
        assert "if" not in job

    job_names = [job["name"] for job in workflow["jobs"].values()]
    assert len(job_names) == len(set(job_names))
    assert set(job_names) == {"Python on trusted Mac", "iOS on trusted Mac"}
