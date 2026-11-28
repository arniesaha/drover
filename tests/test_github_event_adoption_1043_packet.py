"""Protect the source-pinned #1043 packet and its non-installing config projection."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from drover.server.work_queue.config import GitHubAcceptanceProfile

ROOT = Path(__file__).parents[1]
PACKET = ROOT / "docs/quality-pilots/github-event-adoption-1043"
PIN = "f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc"


def proof(source=PIN):
    return {
        "source": source,
        "passed": True,
        "commands": [{"returncode": 0}] * 3,
        "counts": {"tests": 4, "skipped": 0, "errors": 0, "failures": 0},
        "invalid_result_rejected": {"case": "wrong_source", "rejected": True},
    }


def publish(tmp_path, payload):
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(payload))
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(PACKET / "publish-check.py"),
            "--profile",
            str(PACKET / "profile-candidate.json"),
            "--proof",
            str(path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )


def test_1043_publisher_binds_the_new_source_and_rejects_invalid_proof(tmp_path):
    accepted = publish(tmp_path, proof())
    assert accepted.returncode == 0, accepted.stderr
    publication = json.loads(accepted.stdout)
    profile = GitHubAcceptanceProfile.model_validate(
        json.loads((PACKET / "profile-candidate.json").read_text())
    )
    assert profile.pull_number == 1042
    assert profile.review_mode == "independent_check" and profile.owner_id is None
    assert publication["profile_revision"] == profile.revision
    assert publication["payload"]["head_sha"] == PIN
    assert publish(tmp_path, proof("0" * 40)).returncode != 0


def test_1043_private_projection_keeps_p1_unchanged_and_requires_fresh_binding(
    tmp_path,
):
    p1 = json.loads(
        (ROOT / "docs/quality-pilots/p1-runner/profile-candidate.json").read_text()
    )
    q = json.dumps
    baseline = tmp_path / "baseline.toml"
    baseline.write_text(f"""[work_queue]
enabled = true
shadow_mode = false
workspace_id = "packet-test"
[work_queue.execution]
global_max_workers = 2
[[work_queue.goals]]
goal_id = "quality-cohort-1024"
repo = "arniesaha/drover"
goal_label = "goal:quality-cohort-1024"
[work_queue.goals.credential]
token_env = "DROVER_WORK_QUEUE_GITHUB_TOKEN"
capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read"]
[work_queue.goals.github_acceptance_profiles."1031"]
pull_number = {p1['pull_number']}
criteria = {q(p1['criteria'])}
instructions = {q(p1['instructions'])}
behavioral_check = {q(p1['behavioral_check'])}
app_id = {p1['app_id']}
review_mode = {q(p1['review_mode'])}
amendment_record = {q(p1['amendment_record'])}
[work_queue.goals.github_existing_session_bindings."1031"]
profile_revision = "{GitHubAcceptanceProfile.model_validate(p1).revision}"
source_head = "5202c2359483a58b2916c58acac5f11ee2df20b8"
work_session_id = "p1-work"
work_turn_id = "p1-work-turn"
verifier_session_id = "p1-verify"
verifier_turn_id = "p1-verify-turn"
[[work_queue.goals]]
goal_id = "native-owner-canary-848"
repo = "arniesaha/drover"
goal_label = "goal:native-owner-canary-848"
execution_enabled = true
brief_ref = "authorized:goal:native-owner-canary-848"
continuity_owner_id = "drover:owner/native-owner-canary-848"
continuity_owner_harness = "codex"
continuity_owner_model = "gpt-5.6-terra"
[work_queue.goals.execution_cwds]
studio = "/owned/checkout"
[work_queue.goals.credential]
token_env = "DROVER_WORK_QUEUE_GITHUB_TOKEN"
capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read"]
""")
    projection = tmp_path / "projection.json"
    prepared = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PACKET / "prepare-private-config.py"),
            "--baseline-config",
            str(baseline),
            "--native-template-goal",
            "native-owner-canary-848",
            "--profile",
            str(PACKET / "profile-candidate.json"),
            "--output",
            str(projection),
        ],
        capture_output=True,
        text=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    record = json.loads(projection.read_text())
    p1_digest = hashlib.sha256(
        json.dumps(record["work_queue"]["goals"][0], sort_keys=True).encode()
    ).hexdigest()
    validated = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PACKET / "validate-private-config.py"),
            "--baseline-config",
            str(baseline),
            "--projection",
            str(projection),
        ],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr
    report = json.loads(validated.stdout)
    assert report["p1_goal_digest"] == p1_digest
    assert report["p1_unchanged"] and not report["binding_required"]


def test_1043_workflow_isolates_candidate_proof_from_check_publication():
    import yaml

    workflow = yaml.load(
        (ROOT / ".github/workflows/github-event-adoption-1043.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    proof = workflow["jobs"]["proof"]
    publish = workflow["jobs"]["publish"]
    assert proof["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"contents": "read", "checks": "write"}
    assert publish["needs"] == "proof"
    assert proof["services"]["postgres"]["ports"] == ["5432:5432"]
    proof_commands = "\n".join(step.get("run", "") for step in proof["steps"])
    publish_commands = "\n".join(step.get("run", "") for step in publish["steps"])
    assert PIN in proof_commands and "GITHUB_TOKEN" not in proof_commands
    assert "git -C candidate" not in publish_commands
    assert "candidate/.venv" not in publish_commands
