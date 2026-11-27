"""Pilot acceptance crosses WorkService and the GitHub HTTP client seam."""

import copy
import json

import pytest
from test_work_queue_execution import complete, launch, service, update  # noqa: F401
from test_work_queue_github import Response

from drover.server.work_queue.config import GoalScope
from drover.server.work_queue.github import GitHubProvider
from drover.server.work_queue.service import identity

SHA = "a" * 40
CRITERIA = "The selected journey succeeds and its negative control fails."
BODY = f"<!-- drover:acceptance -->\n{CRITERIA}\n<!-- /drover:acceptance -->"
PROFILE = dict(
    pull_number=9,
    criteria=CRITERIA,
    instructions="Run the selected journey and its negative control in isolation.",
    behavioral_check="owner-journey",
    app_id=42,
    owner_id=81,
    amendment_record="Initial independently reviewed pilot contract",
)


class GitHub:
    def __init__(self):
        self.body = BODY
        self.head = SHA
        self.closed = 0
        self.reads = []
        self.evidence = None
        self.overrides = {}
        self.close_timeout = False

    def request(self, method, url, **kwargs):
        path = url.removeprefix("https://api.github.com")
        self.reads.append((method, path))
        if method == "PATCH":
            self.closed += 1
            if self.close_timeout:
                raise TimeoutError("uncertain close")
            return Response({})
        if method == "PUT":
            return Response({})
        routes = {
            "/user": {"id": 50},
            "/repos/example/project/issues/1": {
                "number": 1,
                "state": "open",
                "body": self.body,
                "labels": [{"name": "goal:test"}, {"name": "status:review"}],
            },
            "/repos/example/project/issues/1/events": [],
            "/repos/example/project/pulls/9": {
                "head": {"sha": self.head},
                "base": {"ref": "main", "repo": {"full_name": "example/project"}},
            },
            "/repos/example/project/branches/main/protection/required_status_checks": {
                "checks": [{"context": "required", "app_id": 42}],
            },
            f"/repos/example/project/commits/{SHA}/check-runs": {
                "check_runs": [
                    {
                        "id": 3,
                        "name": "required",
                        "head_sha": SHA,
                        "app": {"id": 42},
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            },
        }
        if path in self.overrides:
            data = copy.deepcopy(self.overrides[path])
            if isinstance(data, dict) and "check_runs" in data and self.evidence:
                data["check_runs"].append(copy.deepcopy(self.evidence))
            return Response(data)
        if path.endswith("/check-runs/7") and self.evidence:
            return Response(self.evidence)
        if path.endswith("/reviews/8") and self.evidence:
            return Response(
                {
                    "id": 8,
                    "state": "APPROVED",
                    "commit_id": SHA,
                    "user": {"id": 81, "type": "User"},
                    "body": "drover-acceptance:" + self.evidence["external_id"],
                }
            )
        data = copy.deepcopy(routes.get(path))
        if isinstance(data, dict) and "check_runs" in data and self.evidence:
            data["check_runs"].append(copy.deepcopy(self.evidence))
        return Response(data, 200 if path in routes else 404)


def pilot(service):
    work = launch(service)
    service.harness.sessions[work["run_key"]].status = "completed"
    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {"1": PROFILE}
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    scope = GoalScope.model_validate(config)
    service.config = service.config.model_copy(update={"goals": [scope]})
    client = GitHub()
    service.providers["goal"] = GitHubProvider(
        service.config.workspace_id, scope, session=client
    )
    return client, complete(service, work)


def test_forged_flags_and_evidence_label_cannot_close_pilot(service, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "synthetic")
    client, verify = pilot(service)
    service.harness.sessions[verify["run_key"]].status = "completed"
    with pytest.raises(ValueError, match="evidence"):
        update(
            service,
            verify,
            "verified",
            behavioral_passed=True,
            checks_rerun=True,
            evidence_key="check:passed",
        )
    assert client.closed == 0


def ready(service, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "synthetic")
    client, verify = pilot(service)
    service.harness.sessions[verify["run_key"]].status = "completed"
    profile = service.config.goals[0].github_acceptance_profiles["1"]
    client.evidence = dict(
        id=7,
        head_sha=SHA,
        name="owner-journey",
        app={"id": 42},
        external_id=profile.revision,
        status="completed",
        conclusion="success",
        output={
            "summary": "Journey and negative-control observations recorded.",
            "text": "The reviewed journey succeeded; the negative control failed.",
        },
    )
    return client, verify


def verdict(service, verify):
    # Forged flags do not override provider facts, and provider evidence does
    # not require callers to assert success booleans.
    return update(service, verify, "verified", evidence_key="check-run:7:review:8")


def test_provider_evidence_and_owner_review_close_once_despite_routine_notes(
    service, monkeypatch
):
    client, verify = ready(service, monkeypatch)
    client.body += "\nRoutine progress note and status marker."
    assert verdict(service, verify) == {"outcome": "verified"}
    assert verdict(service, verify) == {"outcome": "verified"}
    assert client.closed == 1
    assert sum(path.endswith("/check-runs/7") for _, path in client.reads) == 2
    event = service.store.events(identity(verify["run_key"], "verified", None))[-1]
    proof = json.loads(event["provider_operation_id"])
    assert proof["head"] == SHA
    assert proof["required_checks"] == [["required", 3, "success", 42]]
    assert proof["evidence"] == "check-run:7:review:8"
    assert proof["behavioral_result"]["bytes"] > 0
    assert len(proof["behavioral_result"]["sha256"]) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("id", 99),
        ("app", {"id": 99}),
        ("name", "different-journey"),
        ("external_id", "old-profile"),
        ("status", "in_progress"),
        ("conclusion", "skipped"),
        ("conclusion", "failure"),
    ],
)
def test_mismatched_behavioral_evidence_cannot_close(
    service, monkeypatch, field, value
):
    client, verify = ready(service, monkeypatch)
    client.evidence[field] = value
    with pytest.raises(ValueError, match="behavioral evidence"):
        verdict(service, verify)
    assert client.closed == 0


def test_nonexistent_evidence_cannot_close(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    client.evidence = None
    with pytest.raises(RuntimeError, match="github_http_404"):
        verdict(service, verify)
    assert client.closed == 0


@pytest.mark.parametrize(
    "output",
    [None, {}, {"summary": ""}, {"summary": "recorded", "text": "x" * 65537}],
)
def test_missing_or_unbounded_behavioral_result_cannot_close(
    service, monkeypatch, output
):
    client, verify = ready(service, monkeypatch)
    if output is None:
        client.evidence.pop("output")
    else:
        client.evidence["output"] = output
    with pytest.raises(ValueError, match="behavioral result"):
        verdict(service, verify)
    assert client.closed == 0


@pytest.mark.parametrize(
    "change", ["missing", "skipped", "pending", "failed", "wrong_app", "wrong_head"]
)
def test_required_ci_negative_controls(service, monkeypatch, change):
    client, verify = ready(service, monkeypatch)
    run = dict(
        id=3,
        name="required",
        head_sha=SHA,
        app={"id": 42},
        status="completed",
        conclusion="success",
    )
    if change == "skipped":
        run["conclusion"] = "skipped"
    elif change == "pending":
        run["status"] = "in_progress"
    elif change == "failed":
        run["conclusion"] = "failure"
    elif change == "wrong_app":
        run["app"] = {"id": 99}
    elif change == "wrong_head":
        run["head_sha"] = "b" * 40
    client.overrides[f"/repos/example/project/commits/{SHA}/check-runs"] = {
        "check_runs": [] if change == "missing" else [run]
    }
    with pytest.raises(ValueError, match="required checks"):
        verdict(service, verify)
    assert client.closed == 0


@pytest.mark.parametrize(
    "change", ["head", "criteria", "profile", "unfinished", "work_resumed"]
)
def test_stale_verdict_and_unfinished_verifier(service, monkeypatch, change):
    client, verify = ready(service, monkeypatch)
    if change == "head":
        client.head = "b" * 40
    elif change == "criteria":
        client.body = client.body.replace(CRITERIA, "Only print success.")
    elif change == "profile":
        scope = service.config.goals[0]
        config = scope.model_dump()
        config["github_acceptance_profiles"]["1"][
            "instructions"
        ] = "Weakened instructions"
        updated = GoalScope.model_validate(config)
        service.config = service.config.model_copy(update={"goals": [updated]})
        service.providers["goal"].scope = updated
    elif change == "work_resumed":
        work = service.store.links(goal_id="goal", number=1)[0]
        service.harness.sessions[work["run_key"]].status = "running"
    else:
        service.harness.sessions[verify["run_key"]].status = "running"
    with pytest.raises(ValueError, match="criteria|session must complete"):
        verdict(service, verify)
    assert client.closed == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "DISMISSED"),
        ("commit_id", "b" * 40),
        ("user", {"id": 99, "type": "User"}),
        ("body", "approved"),
    ],
)
def test_owner_review_is_independent_and_revision_bound(
    service, monkeypatch, field, value
):
    client, verify = ready(service, monkeypatch)
    review = dict(
        id=8,
        state="APPROVED",
        commit_id=SHA,
        user={"id": 81, "type": "User"},
        body="drover-acceptance:" + client.evidence["external_id"],
    )
    review[field] = value
    client.overrides["/repos/example/project/pulls/9/reviews/8"] = review
    with pytest.raises(ValueError, match="independent owner"):
        verdict(service, verify)
    assert client.closed == 0


def test_final_boundary_rechecks_head_movement(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    original = client.request

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/check-runs"):
            client.head = "b" * 40
        return response

    client.request = request
    with pytest.raises(ValueError, match="source changed"):
        verdict(service, verify)
    assert client.closed == 0


def test_uncertain_close_never_causes_second_effect(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    client.close_timeout = True
    with pytest.raises(TimeoutError):
        verdict(service, verify)
    assert verdict(service, verify) == {"outcome": "deferred"}
    assert client.closed == 1


def test_wrong_repository_source_cannot_close(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    client.overrides["/repos/example/project/pulls/9"] = {
        "head": {"sha": SHA},
        "base": {"ref": "main", "repo": {"full_name": "other/project"}},
    }
    with pytest.raises(ValueError, match="source mismatch"):
        verdict(service, verify)
    assert client.closed == 0


def test_replaced_required_run_at_final_boundary_cannot_close(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    original = client.request
    reads = []

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/check-runs"):
            reads.append(url)
            if len(reads) == 2:
                response.data["check_runs"][0]["id"] = 4
        return response

    client.request = request
    with pytest.raises(ValueError, match="evidence changed"):
        verdict(service, verify)
    assert client.closed == 0


def test_changed_behavioral_result_at_final_boundary_cannot_close(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    original = client.request
    reads = []

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/check-runs/7"):
            reads.append(url)
            if len(reads) == 2:
                response.data["output"]["summary"] = "changed observation"
        return response

    client.request = request
    with pytest.raises(ValueError, match="evidence changed"):
        verdict(service, verify)
    assert client.closed == 0


def test_profiles_are_github_only_and_require_provider_permissions(service):
    from pydantic import ValidationError

    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {"1": PROFILE}
    with pytest.raises(ValidationError, match="require provider reads"):
        GoalScope.model_validate(config)
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    config["candidate_source"] = "local_file"
    with pytest.raises(ValidationError, match="GitHub acceptance profiles"):
        GoalScope.model_validate(config)


def test_older_success_cannot_replace_latest_behavioral_failure(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    original = client.request

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/check-runs"):
            latest = response.data["check_runs"][-1]
            latest.update(id=9, conclusion="failure")
        return response

    client.request = request
    with pytest.raises(ValueError, match="stale behavioral evidence"):
        verdict(service, verify)
    assert client.closed == 0
