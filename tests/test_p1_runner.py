"""P1 publisher rejects a proof that lacks the direct admission observation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from drover.server.work_queue.config import GitHubAcceptanceProfile

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "docs/quality-pilots/p1-runner"


def proof(*, observation=True):
    result = {
        "source": "5202c2359483a58b2916c58acac5f11ee2df20b8",
        "passed": True,
        "commands": [
            {"returncode": 0},
            {"returncode": 0},
            {"returncode": 0},
            {"returncode": 0},
        ],
        "counts": {"tests": 36, "skipped": 0, "errors": 0, "failures": 0},
    }
    if observation:
        result["unknown_quota_admission"] = {
            "denied": True,
            "reason": "quota or capability unavailable",
        }
    return result


def publish(tmp_path, payload):
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(payload))
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(RUNNER / "publish-check.py"),
            "--profile",
            str(RUNNER / "profile-candidate.json"),
            "--proof",
            str(path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        lambda payload: payload["commands"].pop(),
        lambda payload: payload.pop("unknown_quota_admission"),
        lambda payload: payload["unknown_quota_admission"].update(denied=False),
        lambda payload: payload["unknown_quota_admission"].update(reason="other"),
        lambda payload: payload["commands"][3].update(returncode=1),
    ],
)
def test_publisher_rejects_incomplete_unknown_quota_admission_proof(tmp_path, invalid):
    rejected = proof()
    invalid(rejected)
    assert publish(tmp_path, rejected).returncode != 0


def test_publisher_uses_the_complete_schema_profile_revision(tmp_path):
    accepted = publish(tmp_path, proof())
    assert accepted.returncode == 0, accepted.stderr
    publication = json.loads(accepted.stdout)
    profile = GitHubAcceptanceProfile.model_validate(
        json.loads((RUNNER / "profile-candidate.json").read_text())
    )
    assert publication["profile_revision"] == profile.revision
    assert publication["payload"]["head_sha"] == proof()["source"]
