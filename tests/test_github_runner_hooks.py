import importlib.util
import json
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
