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


def test_trusted_workflow_never_runs_untrusted_code() -> None:
    """The self-hosted runner must never execute code from an unmerged PR.

    That runner is the Mac mini hosting the live fleet, so a `pull_request`
    trigger would run arbitrary contributor code beside the hub, its DuckDB
    store and the API token. `pull_request_target` is worse still -- it runs
    with repository secrets -- so both are barred.

    The allowed triggers are asserted as a *subset* rather than an exact set:
    which of them are wired up is an operational choice (the workflow moved
    to manual-only so a full pytest and Xcode build would stop landing on the
    fleet host after every merge), whereas never running untrusted code is
    the invariant.
    """
    workflow = load_workflow("trusted-mac.yml")

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
        assert job["runs-on"] == TRUSTED_RUNNER
        assert "if" not in job

    job_names = [job["name"] for job in workflow["jobs"].values()]
    assert len(job_names) == len(set(job_names))
    assert set(job_names) == {"Python on trusted Mac", "iOS on trusted Mac"}


def test_trusted_python_uses_bounded_host_interpreter_venv() -> None:
    workflow = load_workflow("trusted-mac.yml")
    steps = workflow["jobs"]["python"]["steps"]
    setup = next(step for step in steps if step.get("name") == "Set up Python")

    assert "uses" not in setup
    assert setup["run"] == (
        '"$HOME/.local/bin/python3.11" -m venv "$RUNNER_TEMP/python-venv"\n'
        'echo "$RUNNER_TEMP/python-venv/bin" >> "$GITHUB_PATH"\n'
        '"$RUNNER_TEMP/python-venv/bin/python" --version\n'
    )


def test_trusted_python_runs_each_test_module_in_a_fresh_process() -> None:
    workflow = load_workflow("trusted-mac.yml")
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
    for name in ("ci.yml", "ios.yml", "trusted-mac.yml", "release.yml"):
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
