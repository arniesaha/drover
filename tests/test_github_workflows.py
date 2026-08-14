"""Contracts that keep CI on GitHub-hosted runners and off this fleet."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS_DIR = Path(__file__).parents[1] / ".github" / "workflows"


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


def test_public_workflows_select_expensive_suites_from_changed_paths() -> None:
    python_ci = load_workflow("ci.yml")
    ios_ci = load_workflow("ios.yml")

    python_steps = python_ci["jobs"]["build-and-test"]["steps"]
    ios_steps = ios_ci["jobs"]["build-and-test"]["steps"]

    for steps in (python_steps, ios_steps):
        checkout = steps[0]
        assert checkout["uses"] == "actions/checkout@v4"
        assert checkout["with"]["fetch-depth"] == "0"
        selectors = [
            step
            for step in steps
            if "scripts/ci/select_test_scope.py" in step.get("run", "")
        ]
        assert len(selectors) == 1
        assert selectors[0]["id"] == "scope"
        assert selectors[0]["env"]["BASE_SHA"] == (
            "${{ github.event.pull_request.base.sha }}"
        )

    python_lightweight = next(
        step for step in python_steps if step.get("name") == "Run lightweight checks"
    )
    assert python_lightweight["env"]["BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha }}"
    )
    assert 'git diff --check "$BASE_SHA"...HEAD' in python_lightweight["run"]
    ios_lightweight = next(
        step for step in ios_steps if step.get("name") == "Run lightweight checks"
    )
    assert ios_lightweight["env"]["BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha }}"
    )
    assert ios_lightweight["run"] == 'git diff --check "$BASE_SHA"...HEAD'

    python_setup = next(
        step for step in python_steps if step.get("name") == "Set up Python"
    )
    assert python_setup["if"] == "steps.scope.outputs.python == 'true'"
    ios_xcode = next(
        step for step in ios_steps if step.get("name") == "Show Xcode version"
    )
    assert ios_xcode["if"] == "steps.scope.outputs.ios == 'true'"


def test_no_workflow_requests_a_self_hosted_runner() -> None:
    """A self-hosted runner on a public repo is a remote shell on the fleet.

    This repository had one: a non-ephemeral runner on the Mac mini that is
    the live hub, holding the DuckDB store, the API token, the APNs keys and
    the fleet config. Nothing exploited it only because the single workflow
    naming it happened to be dispatch-only, which is a configuration accident
    rather than a boundary.

    It is not enough to bar `pull_request` on the trusted workflow, which is
    what the earlier version of this test did. For `pull_request` events the
    workflow definition comes from the merge commit, not the base branch, so
    a fork PR can introduce a job requesting the runner even when no workflow
    on `main` does. The only durable invariant is that the label never
    appears at all.

    ci.yml carries the same check as a shell step so the run fails fast; this
    is the version that fails locally, before a push.
    """
    offenders = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = load_workflow(path.name)
        for job_id, job in workflow["jobs"].items():
            runs_on = job.get("runs-on")
            if isinstance(runs_on, dict):
                offenders.append(f"{path.name}:{job_id} selects a runner group")
                continue
            labels = [runs_on] if isinstance(runs_on, str) else list(runs_on or [])
            if any("self-hosted" in label for label in labels):
                offenders.append(f"{path.name}:{job_id} runs-on {labels}")
    assert not offenders, f"these jobs would run on our own hardware: {offenders}"


def test_macos_verification_never_runs_untrusted_code() -> None:
    """Extra macOS coverage, and the one workflow that is not a merge gate.

    Hosted, so a `pull_request` trigger would no longer reach the fleet, but
    it stays barred anyway: a hosted macOS minute costs ten times a Linux one
    and the required checks already cover PRs. `pull_request_target` is barred
    on its own merits, since it runs with repository secrets.
    """
    workflow = load_workflow("macos.yml")

    assert workflow["permissions"] == {"contents": "read"}
    triggers = set(workflow["on"])
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers
    assert triggers <= {"push", "workflow_dispatch"}
    assert triggers, "the workflow must remain runnable somehow"
    if "push" in triggers:
        assert workflow["on"]["push"]["branches"] == ["main"]
    assert "${{ github.ref }}" in workflow["concurrency"]["group"]
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "macos-15"
        assert "if" not in job

    job_names = [job["name"] for job in workflow["jobs"].values()]
    assert len(job_names) == len(set(job_names))
    assert set(job_names) == {"Python on macOS"}


def test_macos_python_uses_the_hosted_setup_action() -> None:
    """The old job built a venv from an interpreter at a fixed $HOME path.

    That only ever existed on the one machine. On a hosted runner the
    interpreter comes from the setup action, which is also what ci.yml uses.
    """
    workflow = load_workflow("macos.yml")
    steps = workflow["jobs"]["python"]["steps"]
    setup = next(step for step in steps if step.get("name") == "Set up Python")

    assert setup["uses"].startswith("actions/setup-python@")
    assert setup["with"]["python-version"] == "3.11"
    assert "run" not in setup


def test_macos_python_runs_each_test_module_in_a_fresh_process() -> None:
    workflow = load_workflow("macos.yml")
    steps = workflow["jobs"]["python"]["steps"]
    test_step = next(
        step for step in steps if step.get("name") == "Run tests with pytest"
    )

    assert test_step["run"] == (
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        'test_files = sorted(Path("tests").rglob("test_*.py"))\n'
        "if not test_files:\n"
        '    raise SystemExit("no Python test modules found")\n'
        "for test_file in test_files:\n"
        '    print(f"::group::{test_file}", flush=True)\n'
        "    try:\n"
        "        subprocess.run(\n"
        '            [sys.executable, "-m", "pytest", str(test_file)], check=True\n'
        "        )\n"
        "    finally:\n"
        '        print("::endgroup::", flush=True)\n'
        "PY\n"
    )


def test_release_workflow_is_tag_triggered_and_publishes_three_artifacts() -> None:
    workflow = load_workflow("release.yml")

    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    # Publishing a release needs write; nothing else in this repo does.
    assert workflow["permissions"] == {"contents": "write"}
    # A manual run must name the tag: on workflow_dispatch GITHUB_REF_NAME is
    # the branch, so without this the job would publish a release called
    # "main".
    assert "ref" in workflow["on"]["workflow_dispatch"]["inputs"]

    job = workflow["jobs"]["release"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = " ".join(step.get("run", "") for step in job["steps"])
    assert "uv build" in steps
    assert "uv export" in steps
    assert "sha256sum" in steps
    assert "SHA256SUMS.txt" in steps
    # An export that silently drops hashes would make --require-hashes a
    # no-op at install time, so the build must fail rather than ship one.
    assert "--hash=sha256:" in steps


def test_release_workflow_never_runs_on_pull_requests() -> None:
    """A PR that could cut a release would let a fork publish artifacts."""
    workflow = load_workflow("release.yml")
    assert "pull_request" not in workflow["on"]
    assert "pull_request_target" not in workflow["on"]


def test_ci_runs_the_shell_tests() -> None:
    """The installer is shell, so pytest alone would leave it unguarded."""
    steps = " ".join(
        step.get("run", "")
        for step in load_workflow("ci.yml")["jobs"]["build-and-test"]["steps"]
    )
    assert "tests/shell" in steps, "installer shell tests must run in CI"
    assert "bash -n install.sh" in steps, "install.sh must at least parse in CI"


def test_ci_stays_github_hosted_after_the_shell_step() -> None:
    """Adding shell tests must not quietly move CI onto the fleet host."""
    workflow = load_workflow("ci.yml")
    assert workflow["jobs"]["build-and-test"]["runs-on"] == "ubuntu-latest"
    assert workflow["permissions"] == {"contents": "read"}


def test_release_refuses_a_wheel_that_does_not_match_the_tag() -> None:
    """pyproject's version names the wheel; the tag names the release.

    When they drift every step still succeeds and the failure lands later, on
    someone else's machine, as a 404 mid-install.
    """
    steps = " ".join(
        step.get("run", "")
        for step in load_workflow("release.yml")["jobs"]["release"]["steps"]
    )
    assert "py3-none-any.whl" in steps
    assert "pyproject.toml to match the tag" in steps


def test_push_triggered_workflows_never_use_the_bare_inputs_context() -> None:
    """`inputs` exists only for workflow_dispatch and workflow_call.

    Referencing it in a workflow that also triggers on push fails the entire
    run at load time with "Unrecognized named-value: 'inputs'": a
    startup_failure with no job and no logs. That is how v0.1.1 came to be
    tagged with no release attached. `github.event.inputs` is always defined
    and is null on a push, so it is the portable spelling.
    """
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        name = path.name
        workflow = load_workflow(name)
        if "push" not in workflow["on"]:
            continue
        raw = (WORKFLOWS_DIR / name).read_text()
        for line in raw.splitlines():
            if "${{" not in line:
                continue
            assert (
                "inputs." not in line or "github.event.inputs." in line
            ), f"{name} references the bare inputs context: {line.strip()}"


def test_workflows_only_use_allowlisted_actions() -> None:
    """This repository restricts Actions to `actions/*`.

    Verified-creator actions are disallowed, so a third-party `uses:` does not
    fail a step: it fails the whole run at load time as a startup_failure with
    no job and no logs. v0.1.1 was tagged twice with no release before this
    was spotted. Confirm the live policy with:

        gh api repos/arniesaha/drover/actions/permissions/selected-actions
    """
    offenders = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
                continue
            ref = stripped.split("uses:", 1)[1].strip()
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            if not ref.startswith("actions/"):
                offenders.append(f"{path.name}: {ref}")
    assert not offenders, (
        "these actions are outside the repository allowlist and will cause a "
        f"startup_failure: {offenders}"
    )


def test_release_verifies_the_published_artifact_end_to_end() -> None:
    """Unit tests cannot catch an install that refuses itself.

    v0.1.1 passed every suite, published three correct artifacts, and still
    could not install: drover-server had no --version flag and install.sh
    smoke-tests exactly that. Only running the real script against the real
    release finds that class of bug.
    """
    job = load_workflow("release.yml")["jobs"]["verify-install"]
    assert job["needs"] == "release", "nothing to install before publishing"
    assert job["runs-on"] == "ubuntu-latest", "a clean machine, not the fleet"

    steps = " ".join(step.get("run", "") for step in job["steps"])
    assert "bash install.sh" in steps, "must run the real installer"
    assert "--version" in steps, "must assert the smoke gate v0.1.1 failed"
    assert "runtime/current" in steps
    assert "healthz" in steps, "must prove the installed build actually serves"
    assert "drover://" in steps, "must prove pairing works after install"
    # Every other assertion in that job runs the binary by absolute path,
    # which is how "installed but not a command" survived: the runtime lives
    # under ~/.drover, which is on nobody's PATH.
    assert (
        ".local/bin/drover-server" in steps
    ), "must prove the CLI is reachable as a command, not just on disk"
