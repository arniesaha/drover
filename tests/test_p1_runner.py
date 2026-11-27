"""P1 publisher rejects a proof that lacks the direct admission observation."""

import json
import subprocess
import sys
from pathlib import Path

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


def test_publisher_requires_direct_unknown_quota_admission_observation(tmp_path):
    missing = publish(tmp_path, proof(observation=False))
    assert missing.returncode != 0

    accepted = publish(tmp_path, proof())
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["payload"]["head_sha"] == proof()["source"]
