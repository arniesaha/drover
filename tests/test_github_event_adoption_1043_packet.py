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
from drover.server.work_queue.worker_mcp import BoundWorkControl

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


def test_1043_live_driver_uses_bound_worker_mcp_for_deferred_stages(monkeypatch):
    calls = []

    def deferred(_control, operation, arguments):
        calls.append((operation, arguments))
        return {"outcome": "deferred"}

    monkeypatch.setattr(BoundWorkControl, "_call", deferred)
    control = BoundWorkControl(
        "http://127.0.0.1:8080",
        "paired-host-bearer",
        "signed-run-key",
        "github-event-adoption-1043",
        1043,
    )
    assert control.update("complete") == {"outcome": "deferred"}
    assert control.update("verified", evidence_key="check-run:77") == {
        "outcome": "deferred"
    }
    complete, verified = calls
    assert complete == (
        "update",
        {
            "goal_id": "github-event-adoption-1043",
            "number": 1043,
            "run_key": "signed-run-key",
            "action": "complete",
            "evidence_key": None,
            "estimated_cost": None,
        },
    )
    assert verified == (
        "update",
        {
            "goal_id": "github-event-adoption-1043",
            "number": 1043,
            "run_key": "signed-run-key",
            "action": "verified",
            "evidence_key": "check-run:77",
            "estimated_cost": None,
            "behavioral_passed": True,
            "checks_rerun": True,
        },
    )


def test_1043_private_projection_keeps_p1_unchanged_and_requires_fresh_binding(
    tmp_path,
):
    p1 = json.loads(
        (ROOT / "docs/quality-pilots/p1-runner/profile-candidate.json").read_text()
    )
    q = json.dumps
    baseline = tmp_path / "baseline.toml"
    policy_path = ROOT / "src/drover/server/work_queue/policy-v1.json"
    brief_ref = (
        "docs/quality-pilots/github-event-adoption-1043/owner-held-live-brief.md"
    )
    isolated_cwd = tmp_path / "isolated-execution"
    native_cwd = tmp_path / "native-execution"
    isolated_cwd.mkdir()
    native_cwd.mkdir()
    execution_brief = isolated_cwd / brief_ref
    execution_brief.parent.mkdir(parents=True)
    execution_brief.write_bytes((PACKET / "owner-held-live-brief.md").read_bytes())
    for command in (
        ["git", "init", "-q", str(isolated_cwd)],
        ["git", "-C", str(isolated_cwd), "add", brief_ref],
        [
            "git",
            "-C",
            str(isolated_cwd),
            "-c",
            "user.name=packet",
            "-c",
            "user.email=packet@example.invalid",
            "commit",
            "-qm",
            "add owner-held brief",
        ],
    ):
        subprocess.run(command, check=True)
    baseline.write_text(f"""[work_queue]
enabled = true
shadow_mode = false
workspace_id = "packet-test"
principal_roles = {{ owner = "coordinator" }}
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
policy_path = {q(str(policy_path))}
execution_enabled = true
execution_budget = 9.0
execution_estimated_cost = 2.5
execution_max_workers = 1
brief_ref = "authorized:goal:native-owner-canary-848"
continuity_owner_id = "drover:owner/native-owner-canary-848"
continuity_owner_harness = "codex"
continuity_owner_model = "gpt-5.6-terra"
[work_queue.goals.execution_cwds]
studio = {q(str(native_cwd))}
[work_queue.goals.credential]
token_env = "DROVER_WORK_QUEUE_GITHUB_TOKEN"
capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read"]
[server]
metrics_host = "127.0.0.1"
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
            "--execution-host",
            "studio",
            "--execution-cwd",
            str(isolated_cwd),
            "--brief-ref",
            brief_ref,
            "--profile",
            str(PACKET / "profile-candidate.json"),
            "--output",
            str(projection),
        ],
        capture_output=True,
        text=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    live_brief_args = [
        sys.executable,
        "-I",
        str(PACKET / "verify-live-brief.py"),
        "--execution-cwd",
        str(isolated_cwd),
        "--brief-ref",
        brief_ref,
    ]
    assert (
        subprocess.run(live_brief_args, capture_output=True, text=True).returncode == 0
    )
    original_brief = execution_brief.read_bytes()
    execution_brief.write_text("tampered\n")
    assert (
        subprocess.run(live_brief_args, capture_output=True, text=True).returncode != 0
    )
    execution_brief.write_bytes(original_brief)
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
            "--native-template-goal",
            "native-owner-canary-848",
            "--execution-host",
            "studio",
            "--execution-cwd",
            str(isolated_cwd),
            "--brief-ref",
            brief_ref,
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
    assert report["baseline_unchanged_outside_target_and_ingestion"]
    target = next(
        goal
        for goal in record["work_queue"]["goals"]
        if goal["goal_id"] == "github-event-adoption-1043"
    )
    assert target["execution_cwds"] == {"studio": str(isolated_cwd.resolve())}
    assert target["policy_path"] == str(policy_path)
    assert target["execution_estimated_cost"] == 2.5
    assert target["brief_ref"] == brief_ref

    overlap = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PACKET / "prepare-private-config.py"),
            "--baseline-config",
            str(baseline),
            "--native-template-goal",
            "native-owner-canary-848",
            "--execution-host",
            "studio",
            "--execution-cwd",
            str(native_cwd),
            "--brief-ref",
            brief_ref,
            "--profile",
            str(PACKET / "profile-candidate.json"),
            "--output",
            str(tmp_path / "overlap.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert overlap.returncode != 0
    wrong_brief = tmp_path / "wrong-brief.md"
    wrong_brief.write_text("not the reviewed task contract\n")
    arbitrary_brief = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PACKET / "prepare-private-config.py"),
            "--baseline-config",
            str(baseline),
            "--native-template-goal",
            "native-owner-canary-848",
            "--execution-host",
            "studio",
            "--execution-cwd",
            str(isolated_cwd),
            "--brief-ref",
            str(wrong_brief),
            "--profile",
            str(PACKET / "profile-candidate.json"),
            "--output",
            str(tmp_path / "arbitrary-brief.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert arbitrary_brief.returncode != 0

    def reject_projection(mutator):
        rejected = tmp_path / "rejected-projection.json"
        mutated = json.loads(json.dumps(record))
        mutator(mutated)
        rejected.write_text(json.dumps(mutated))
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(PACKET / "validate-private-config.py"),
                "--baseline-config",
                str(baseline),
                "--native-template-goal",
                "native-owner-canary-848",
                "--execution-host",
                "studio",
                "--execution-cwd",
                str(isolated_cwd),
                "--brief-ref",
                brief_ref,
                "--projection",
                str(rejected),
            ],
            capture_output=True,
            text=True,
        )

    assert (
        reject_projection(
            lambda projection: projection["work_queue"]["principal_roles"].update(
                {"owner": "agent"}
            )
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: next(
                goal
                for goal in projection["work_queue"]["goals"]
                if goal["goal_id"] == "quality-cohort-1024"
            )["credential"]["capabilities"].append("contents_read")
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: next(
                goal
                for goal in projection["work_queue"]["goals"]
                if goal["goal_id"] == "github-event-adoption-1043"
            )["credential"]["capabilities"].append("contents_read")
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: next(
                goal
                for goal in projection["work_queue"]["goals"]
                if goal["goal_id"] == "github-event-adoption-1043"
            ).update({"execution_max_workers": 2})
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: projection["work_queue"]["github_webhook"].update(
                {"batch_size": 17}
            )
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: next(
                goal
                for goal in projection["work_queue"]["goals"]
                if goal["goal_id"] == "native-owner-canary-848"
            ).update({"brief_ref": "changed:unrelated-goal"})
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: projection["work_queue"][
                "github_provider_ingestion"
            ].update({"retry_seconds": 120})
        ).returncode
        != 0
    )
    assert (
        reject_projection(
            lambda projection: next(
                goal
                for goal in projection["work_queue"]["goals"]
                if goal["goal_id"] == "github-event-adoption-1043"
            ).update({"execution_estimated_cost": 99.0})
        ).returncode
        != 0
    )

    bound = json.loads(json.dumps(record))
    bound_target = next(
        goal
        for goal in bound["work_queue"]["goals"]
        if goal["goal_id"] == "github-event-adoption-1043"
    )
    bound_target["github_existing_session_bindings"] = {
        "1043": {
            "profile_revision": bound["profile_revision"],
            "source_head": PIN,
            "work_session_id": "1043-work",
            "work_turn_id": "1043-work-turn",
            "verifier_session_id": "1043-verifier",
            "verifier_turn_id": "1043-verifier-turn",
        }
    }
    bound_projection = tmp_path / "bound-projection.json"
    bound_projection.write_text(json.dumps(bound))
    bound_args = [
        sys.executable,
        "-I",
        str(PACKET / "validate-private-config.py"),
        "--baseline-config",
        str(baseline),
        "--native-template-goal",
        "native-owner-canary-848",
        "--execution-host",
        "studio",
        "--execution-cwd",
        str(isolated_cwd),
        "--brief-ref",
        brief_ref,
        "--projection",
        str(bound_projection),
        "--require-binding",
    ]
    assert subprocess.run(bound_args, capture_output=True, text=True).returncode == 0
    bound_target["execution_max_workers"] = 2
    bound_projection.write_text(json.dumps(bound))
    assert subprocess.run(bound_args, capture_output=True, text=True).returncode != 0

    profile = json.loads((PACKET / "profile-candidate.json").read_text())
    candidate = tmp_path / "candidate.toml"
    candidate.write_text(baseline.read_text() + f"""
[work_queue.github_provider_ingestion]
enabled = true
interval_seconds = 60
batch_size = 2
retry_seconds = 60
max_backoff_seconds = 300
settled_seconds = 300
[[work_queue.goals]]
goal_id = "github-event-adoption-1043"
repo = "arniesaha/drover"
goal_label = "goal:github-event-adoption-1043"
policy_path = {q(str(policy_path))}
execution_enabled = true
execution_budget = 9.0
execution_estimated_cost = 2.5
execution_max_workers = 1
brief_ref = {q(brief_ref)}
continuity_owner_id = "drover:owner/github-event-adoption-1043"
continuity_owner_harness = "codex"
continuity_owner_model = "gpt-5.6-terra"
integration_serialization_key = "github-event-adoption-1043"
candidate_source = "github"
[work_queue.goals.execution_cwds]
studio = {q(str(isolated_cwd.resolve()))}
[work_queue.goals.credential]
token_env = "DROVER_WORK_QUEUE_GITHUB_TOKEN"
capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read"]
[work_queue.goals.github_acceptance_profiles."1043"]
pull_number = {profile['pull_number']}
criteria = {q(profile['criteria'])}
instructions = {q(profile['instructions'])}
behavioral_check = {q(profile['behavioral_check'])}
app_id = {profile['app_id']}
review_mode = {q(profile['review_mode'])}
amendment_record = {q(profile['amendment_record'])}
[work_queue.goals.github_event_delivery_profiles]
"1043" = "{GitHubAcceptanceProfile.model_validate(profile).revision}"
[work_queue.goals.github_provider_ingestion_profiles]
"1043" = "{GitHubAcceptanceProfile.model_validate(profile).revision}"
""")
    candidate_args = [
        sys.executable,
        "-I",
        str(PACKET / "validate-private-config.py"),
        "--baseline-config",
        str(baseline),
        "--native-template-goal",
        "native-owner-canary-848",
        "--execution-host",
        "studio",
        "--execution-cwd",
        str(isolated_cwd),
        "--brief-ref",
        brief_ref,
        "--candidate-config",
        str(candidate),
    ]
    final = subprocess.run(candidate_args, capture_output=True, text=True)
    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["prepared_ingestion"]["batch_size"] == 2
    candidate_text = candidate.read_text()

    def reject_final(content):
        candidate.write_text(content)
        return subprocess.run(candidate_args, capture_output=True, text=True)

    assert (
        reject_final(
            candidate_text.replace("batch_size = 2", "batch_size = 3")
        ).returncode
        != 0
    )
    assert (
        reject_final(
            candidate_text.replace(
                'candidate_source = "github"', 'candidate_source = "local_file"'
            )
        ).returncode
        != 0
    )
    assert (
        reject_final(
            candidate_text.replace(
                q(brief_ref), q("authorized:goal:native-owner-canary-848")
            )
        ).returncode
        != 0
    )
    assert (
        reject_final(
            candidate_text.replace(
                f'execution_max_workers = 1\nbrief_ref = {q(brief_ref)}\ncontinuity_owner_id = "drover:owner/github-event-adoption-1043"',
                f'execution_max_workers = 2\nbrief_ref = {q(brief_ref)}\ncontinuity_owner_id = "drover:owner/github-event-adoption-1043"',
            )
        ).returncode
        != 0
    )
    assert (
        reject_final(
            candidate_text.replace(
                'capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read"]\n[work_queue.goals.github_acceptance_profiles."1043"]',
                'capabilities = ["issues_read", "issues_write", "pulls_read", "checks_read", "actions_read", "contents_read"]\n[work_queue.goals.github_acceptance_profiles."1043"]',
            )
        ).returncode
        != 0
    )
    assert (
        reject_final(
            candidate_text.replace(
                'metrics_host = "127.0.0.1"', 'metrics_host = "0.0.0.0"'
            )
        ).returncode
        != 0
    )
    candidate.write_text(candidate_text)


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
