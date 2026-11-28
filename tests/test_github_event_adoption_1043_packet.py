"""Protect the source-pinned #1043 packet and its non-installing config projection."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from drover.server.work_queue.config import GitHubAcceptanceProfile, GoalScope
from drover.server.work_queue.github import GitHubProvider
from drover.server.work_queue.provider import ItemRef

ROOT = Path(__file__).parents[1]
PACKET = ROOT / "docs/quality-pilots/github-event-adoption-1043"
PIN = "bde283aa6bd4f97073867930153c8d12e9b8b85e"
MERGE_COMMIT = "f68653d9be5adbe8eed1ba53a832d5ebb55d7dbc"


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
    assert publish(tmp_path, proof(MERGE_COMMIT)).returncode != 0


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


class _Response:
    def __init__(self, data):
        self.status_code = 200
        self.headers = {}
        self.content = json.dumps(data).encode()
        self._data = data

    def json(self):
        return json.loads(json.dumps(self._data))


class _CurrentHeadTransport:
    def __init__(self):
        self.pull_head = PIN
        self.evidence_head = PIN

    def request(self, _method, url, **_kwargs):
        path = url.removeprefix("https://api.github.com")
        evidence = {
            "id": 77,
            "name": "github-event-adoption-1043-outcome",
            "head_sha": self.evidence_head,
            "app": {"id": 15368},
            "external_id": GitHubAcceptanceProfile.model_validate(
                json.loads((PACKET / "profile-candidate.json").read_text())
            ).revision,
            "status": "completed",
            "conclusion": "success",
            "output": {"summary": "bounded result", "text": "retained output"},
        }
        routes = {
            "/repos/arniesaha/drover/pulls/1042": {
                "head": {"sha": self.pull_head},
                "base": {"ref": "main", "repo": {"full_name": "arniesaha/drover"}},
            },
            "/repos/arniesaha/drover/check-runs/77": evidence,
            "/repos/arniesaha/drover/branches/main/protection/required_status_checks": {
                "checks": [{"context": "required-ci", "app_id": 15368}]
            },
            f"/repos/arniesaha/drover/commits/{PIN}/check-runs": {
                "check_runs": [
                    evidence,
                    {
                        "id": 78,
                        "name": "required-ci",
                        "head_sha": PIN,
                        "app": {"id": 15368},
                        "status": "completed",
                        "conclusion": "success",
                    },
                ]
            },
        }
        return _Response(routes[path])


def test_1043_provider_fixture_rejects_merge_commit_or_stale_head(monkeypatch):
    monkeypatch.setenv("PACKET_GITHUB_TOKEN", "synthetic")
    profile = json.loads((PACKET / "profile-candidate.json").read_text())
    scope = GoalScope.model_validate(
        {
            "goal_id": "github-event-adoption-1043",
            "repo": "arniesaha/drover",
            "goal_label": "goal:github-event-adoption-1043",
            "credential": {
                "token_env": "PACKET_GITHUB_TOKEN",
                "capabilities": ["issues_read", "pulls_read", "checks_read"],
            },
            "github_acceptance_profiles": {"1043": profile},
        }
    )
    provider = GitHubProvider("packet", scope, session=_CurrentHeadTransport())
    ref = ItemRef("packet", "github", "arniesaha/drover", 1043, "")
    provider.get_item = lambda _ref: {
        "state": "open",
        "body": "<!-- drover:acceptance -->\n"
        + profile["criteria"]
        + "\n<!-- /drover:acceptance -->",
    }
    assert provider.acceptance_facts(ref, "check-run:77")["head"] == PIN

    provider.session.pull_head = MERGE_COMMIT
    provider._cache.clear()
    with pytest.raises(ValueError, match="behavioral evidence mismatch"):
        provider.acceptance_facts(ref, "check-run:77")

    provider.session.pull_head = PIN
    provider.session.evidence_head = MERGE_COMMIT
    provider._cache.clear()
    with pytest.raises(ValueError, match="behavioral evidence mismatch"):
        provider.acceptance_facts(ref, "check-run:77")
