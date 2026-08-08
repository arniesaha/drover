import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PRE_JOB_PATH = Path(__file__).parents[1] / "scripts/github_runner/pre_job_guard.py"
SPEC = importlib.util.spec_from_file_location("pre_job_guard", PRE_JOB_PATH)
assert SPEC is not None and SPEC.loader is not None
PRE_JOB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PRE_JOB
SPEC.loader.exec_module(PRE_JOB)

POST_JOB_PATH = Path(__file__).parents[1] / "scripts/github_runner/post_job_cleanup.py"
POST_JOB_SPEC = importlib.util.spec_from_file_location(
    "post_job_cleanup", POST_JOB_PATH
)
assert POST_JOB_SPEC is not None and POST_JOB_SPEC.loader is not None
POST_JOB = importlib.util.module_from_spec(POST_JOB_SPEC)
sys.modules[POST_JOB_SPEC.name] = POST_JOB
POST_JOB_SPEC.loader.exec_module(POST_JOB)


def push_payload() -> dict[str, object]:
    return {
        "ref": "refs/heads/main",
        "repository": {"full_name": "arniesaha/drover"},
        "sender": {"login": "arniesaha"},
    }


def base_env(tmp_path: Path, payload: dict[str, object]) -> dict[str, str]:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    return {
        "GITHUB_REPOSITORY": "arniesaha/drover",
        "GITHUB_WORKFLOW_REF": (
            "arniesaha/drover/.github/workflows/trusted-mac.yml@refs/heads/main"
        ),
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "arniesaha",
        "GITHUB_EVENT_PATH": str(event_path),
    }


def test_pre_job_accepts_push_to_main(tmp_path: Path) -> None:
    env = base_env(tmp_path, push_payload())
    PRE_JOB.validate_job(env, push_payload())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REPOSITORY", "attacker/fork"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/pull/7/merge"),
        (
            "GITHUB_WORKFLOW_REF",
            "arniesaha/drover/.github/workflows/evil.yml@refs/heads/main",
        ),
    ],
)
def test_pre_job_rejects_untrusted_metadata(
    tmp_path: Path, key: str, value: str
) -> None:
    env = base_env(tmp_path, push_payload())
    env[key] = value
    with pytest.raises(PRE_JOB.GuardError):
        PRE_JOB.validate_job(env, push_payload())


def test_pre_job_accepts_owner_workflow_dispatch(tmp_path: Path) -> None:
    payload = push_payload() | {"ref": "main"}
    env = base_env(tmp_path, payload)
    env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    PRE_JOB.validate_job(env, payload)


def test_pre_job_rejects_dispatch_by_another_actor(tmp_path: Path) -> None:
    payload = push_payload() | {"ref": "main"}
    env = base_env(tmp_path, payload)
    env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    env["GITHUB_ACTOR"] = "attacker"
    with pytest.raises(PRE_JOB.GuardError):
        PRE_JOB.validate_job(env, payload)


@pytest.mark.parametrize(
    ("payload", "event"),
    [
        (push_payload() | {"repository": {"full_name": "attacker/fork"}}, "push"),
        (push_payload() | {"ref": "refs/heads/feature"}, "push"),
        (
            push_payload() | {"ref": "main", "sender": {"login": "attacker"}},
            "workflow_dispatch",
        ),
    ],
)
def test_pre_job_rejects_mismatched_payload(
    tmp_path: Path, payload: dict[str, object], event: str
) -> None:
    env = base_env(tmp_path, push_payload())
    env["GITHUB_EVENT_NAME"] = event
    with pytest.raises(PRE_JOB.GuardError):
        PRE_JOB.validate_job(env, payload)


def test_pre_job_rejects_missing_variables(tmp_path: Path) -> None:
    env = base_env(tmp_path, push_payload())
    del env["GITHUB_EVENT_NAME"]
    with pytest.raises(PRE_JOB.GuardError):
        PRE_JOB.validate_job(env, push_payload())


def test_pre_job_cli_rejects_missing_event_file(tmp_path: Path) -> None:
    env = base_env(tmp_path, push_payload())
    env["GITHUB_EVENT_PATH"] = str(tmp_path / "missing-event.json")
    result = subprocess.run(
        [sys.executable, str(PRE_JOB_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "event" in result.stderr.lower()
    assert "arniesaha" not in result.stderr


def test_pre_job_cli_rejects_malformed_json(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    env = base_env(tmp_path, push_payload())
    event_path.write_text("not json")
    env["GITHUB_EVENT_PATH"] = str(event_path)
    result = subprocess.run(
        [sys.executable, str(PRE_JOB_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "json" in result.stderr.lower()
    assert "arniesaha" not in result.stderr


def runner_cleanup_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    work_root = tmp_path / "runner"
    workspace = work_root / "_work" / "drover" / "drover"
    temp_dir = work_root / "_work" / "_temp"
    unrelated = work_root / "_work" / "drover" / "unrelated"
    workspace.mkdir(parents=True)
    temp_dir.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    (workspace / "checkout.txt").write_text("remove")
    (temp_dir / "temp.txt").write_text("remove")
    (unrelated / "keep.txt").write_text("keep")
    return work_root, workspace, temp_dir, unrelated


def cleanup_environment(
    work_root: Path, workspace: Path, temp_dir: Path
) -> dict[str, str]:
    return {
        "DROVER_RUNNER_WORK_ROOT": str(work_root),
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(temp_dir),
    }


def test_cleanup_job_removes_only_validated_targets(tmp_path: Path) -> None:
    work_root, workspace, temp_dir, unrelated = runner_cleanup_layout(tmp_path)

    removed = POST_JOB.cleanup_job(cleanup_environment(work_root, workspace, temp_dir))

    assert removed == (workspace.resolve(), temp_dir.resolve())
    assert not workspace.exists()
    assert not temp_dir.exists()
    assert (unrelated / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "unsafe_target",
    [
        "empty-root",
        "filesystem-root",
        "home-directory",
        "workspace-equal-root",
        "workspace-outside-root",
        "workspace-name",
        "temp-name",
    ],
)
def test_cleanup_job_rejects_unsafe_targets(tmp_path: Path, unsafe_target: str) -> None:
    work_root, workspace, temp_dir, _ = runner_cleanup_layout(tmp_path)
    environ = cleanup_environment(work_root, workspace, temp_dir)

    if unsafe_target == "empty-root":
        environ["DROVER_RUNNER_WORK_ROOT"] = ""
    elif unsafe_target == "filesystem-root":
        environ["DROVER_RUNNER_WORK_ROOT"] = os.sep
    elif unsafe_target == "home-directory":
        environ["DROVER_RUNNER_WORK_ROOT"] = str(Path.home())
    elif unsafe_target == "workspace-equal-root":
        environ["GITHUB_WORKSPACE"] = str(work_root)
    elif unsafe_target == "workspace-outside-root":
        environ["GITHUB_WORKSPACE"] = str(tmp_path / "outside" / "drover")
    elif unsafe_target == "workspace-name":
        environ["GITHUB_WORKSPACE"] = str(work_root / "_work" / "drover" / "checkout")
    elif unsafe_target == "temp-name":
        environ["RUNNER_TEMP"] = str(work_root / "_work" / "temp")
    else:  # pragma: no cover - protects the parameterized test itself
        raise AssertionError(f"unknown unsafe target: {unsafe_target}")

    with pytest.raises(POST_JOB.CleanupError):
        POST_JOB.cleanup_job(environ)

    assert workspace.exists()
    assert temp_dir.exists()
