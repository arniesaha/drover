"""Pilot acceptance crosses WorkService and the GitHub HTTP client seam."""

import copy
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_work_queue_execution import (  # noqa: F401
    ACTOR,
    complete,
    launch,
    service,
    update,
)
from test_work_queue_github import Response
from test_hub_work_items import _register

from drover.server.harness.registry import HarnessRegistry
from drover.server.work_queue.config import (
    GitHubAcceptanceProfile,
    GitHubExistingSessionBinding,
    GoalScope,
)
from drover.server.work_queue.github import GitHubProvider
from drover.server.work_queue.github_events import GitHubWebhookWorker
from drover.server.work_queue.runtime import HarnessBridge
from drover.server.work_queue.service import WorkActor, identity

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


def pilot(service, profile=PROFILE):
    work = launch(service)
    service.harness.sessions[work["run_key"]].status = "completed"
    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {"1": profile}
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


def ready(service, monkeypatch, profile=PROFILE):
    monkeypatch.setenv("TEST_TOKEN", "synthetic")
    client, verify = pilot(service, profile)
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


def independent_profile():
    profile = dict(PROFILE, review_mode="independent_check")
    profile.pop("owner_id")
    return profile


def independent_verdict(service, verify):
    return update(service, verify, "verified", evidence_key="check-run:7")


def test_signed_exact_head_ci_continues_opted_github_deferred_pair(
    service, monkeypatch
):
    """A signed CI rollup advances only the installed GitHub delivery path."""
    monkeypatch.setenv("TEST_TOKEN", "synthetic")
    monkeypatch.setenv("TEST_GITHUB_WEBHOOK_SECRET", "not-a-real-secret")
    profile = independent_profile()
    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {"1": profile}
    config["github_event_delivery_profiles"] = {"1": profile_revision(profile)}
    config.update(
        execution_enabled=True,
        execution_cwds={"host": "/configured/checkout"},
        brief_ref="docs/quality-pilots/github-event.md",
        continuity_owner_id="drover:owner/goal",
        continuity_owner_harness="codex",
    )
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    scope = GoalScope.model_validate(config)
    webhook = service.config.github_webhook.model_copy(
        update={"secret_env": "TEST_GITHUB_WEBHOOK_SECRET"}
    )
    service.config = service.config.model_copy(
        update={"goals": [scope], "github_webhook": webhook}
    )
    service.github_webhooks.config = webhook
    client = GitHub()
    client.overrides["/repos/example/project/issues/1"] = {
        "number": 1,
        "state": "open",
        "body": BODY,
        "labels": [{"name": "goal:test"}, {"name": "status:queued"}],
    }
    service.providers["goal"] = GitHubProvider(
        service.config.workspace_id, scope, session=client
    )
    service.harness.github_event_delivery = lambda run_key, revision: (
        revision == profile_revision(profile)
    )
    service.harness.terminal_work_done = lambda run_key: (
        service.harness.sessions[run_key].status == "completed"
    )
    work = launch(service, "github-event-work")
    host = WorkActor("credential:host", "worker", host_id="host")
    assert service.call(
        "update",
        {
            "goal_id": "goal",
            "number": 1,
            "run_key": work["run_key"],
            "action": "complete",
        },
        host,
    ) == {"outcome": "deferred"}
    service.harness.sessions[work["run_key"]].status = "completed"
    client.overrides["/repos/example/project/issues/1"]["labels"][-1][
        "name"
    ] = "status:review"

    _register(service, session_id="github-event-work-item")
    item = service.store.hub_items()[0]
    service.store.patch_hub_item(
        item["id"], pr="https://github.com/example/project/pull/9"
    )
    worker = GitHubWebhookWorker(
        service.store, webhook, service.github_webhooks, service=service
    )

    def deliver(delivery, event, payload):
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": "sha256="
            + hmac.new(b"not-a-real-secret", body, hashlib.sha256).hexdigest(),
        }
        assert service.github_webhooks.accept(headers, body)[0] == 202

    deliver(
        "github-event-sync",
        "pull_request",
        {
            "action": "synchronize",
            "number": 9,
            "repository": {"full_name": "example/project"},
            "pull_request": {"head": {"ref": "feature", "sha": SHA}},
        },
    )
    deliver(
        "github-event-stale-ci",
        "check_suite",
        {
            "repository": {"full_name": "example/project"},
            "check_suite": {
                "status": "completed",
                "conclusion": "success",
                "head_sha": "b" * 40,
                "head_branch": "feature",
                "pull_requests": [{"number": 9}],
            },
        },
    )
    assert worker.process_once() == 2
    assert [row["run_role"] for row in service.store.links()] == ["work"]

    deliver(
        "github-event-current-ci",
        "check_suite",
        {
            "repository": {"full_name": "example/project"},
            "check_suite": {
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "head_branch": "feature",
                "pull_requests": [{"number": 9}],
            },
        },
    )
    assert worker.process_once() == 1
    links = service.store.links(goal_id="goal", number=1)
    assert [row["run_role"] for row in links] == ["work", "verify"]
    verify = links[-1]
    client.evidence = {
        "id": 7,
        "head_sha": SHA,
        "name": "owner-journey",
        "app": {"id": 42},
        "external_id": profile_revision(profile),
        "status": "completed",
        "conclusion": "success",
        "output": {"summary": "journey passed", "text": "negative control failed"},
    }
    assert service.call(
        "update",
        {
            "goal_id": "goal",
            "number": 1,
            "run_key": verify["run_key"],
            "action": "verified",
            "evidence_key": "check-run:7",
        },
        host,
    ) == {"outcome": "deferred"}
    service.harness.sessions[verify["run_key"]].status = "completed"
    final_payload = {
        "repository": {"full_name": "example/project"},
        "workflow_run": {
            "status": "completed",
            "conclusion": "success",
            "head_sha": SHA,
            "head_branch": "feature",
            "pull_requests": [{"number": 9}],
        },
    }
    deliver("github-event-verify-ci", "workflow_run", final_payload)
    assert worker.process_once() == 1
    assert client.closed == 1
    assert service.call(
        "update",
        {
            "goal_id": "goal",
            "number": 1,
            "run_key": verify["run_key"],
            "action": "verified",
            "evidence_key": "check-run:7",
        },
        ACTOR,
    ) == {"outcome": "verified"}
    assert client.closed == 1


def existing_binding(profile, **changes):
    binding = {
        "profile_revision": GitHubAcceptanceProfile.model_validate(profile).revision,
        "source_head": SHA,
        "work_session_id": "existing-work-session",
        "work_turn_id": "existing-work-turn",
        "verifier_session_id": "existing-verify-session",
        "verifier_turn_id": "existing-verify-turn",
    }
    binding.update(changes)
    return binding


def existing_pilot(service, monkeypatch, binding):
    monkeypatch.setenv("TEST_TOKEN", "synthetic")
    profile = independent_profile()
    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {"1": profile}
    config["github_existing_session_bindings"] = {"1": binding}
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    scope = GoalScope.model_validate(config)
    service.config = service.config.model_copy(update={"goals": [scope]})
    client = GitHub()
    service.providers["goal"] = GitHubProvider(
        service.config.workspace_id, scope, session=client
    )
    for session_id, turn_id in (
        (binding["work_session_id"], binding["work_turn_id"]),
        (binding["verifier_session_id"], binding["verifier_turn_id"]),
    ):
        service.harness.existing_sessions[(session_id, turn_id)] = SimpleNamespace(
            session_id=session_id,
            parent_session_id=None,
            status="completed",
            repo="example/project",
            turn_completed=True,
            host_id="host",
        )
    return client


def resumable_existing_pilot(service, monkeypatch, pg_control_path):
    """Install real retained-turn reads below the WorkService boundary."""
    profile = independent_profile()
    binding = existing_binding(
        profile,
        work_session_id="resumable-work",
        work_turn_id="retained-work-turn",
        verifier_session_id="resumable-verify",
        verifier_turn_id="retained-verify-turn",
    )
    client = existing_pilot(service, monkeypatch, binding)
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="fixture", display_name="Fixture", kind="macos")
    live = {}

    def recorded_turn(session_id, harness, turn_id, seq):
        registry.append_event(
            session_id=session_id,
            event_type="user_input",
            seq=seq,
            payload={"turn_id": turn_id},
        )
        if harness == "codex":
            terminal = {
                "turn_id": turn_id,
                "payload": {"exited": 0},
            }
        else:
            terminal = {
                "type": "status",
                "role": "system",
                "turn_id": None,
                "payload": {
                    "turn_complete": True,
                    "awaiting": "input",
                    "result": {
                        "type": "result",
                        "is_error": False,
                        "subtype": "success",
                        "terminal_reason": "completed",
                        "queued_turn_count": 0,
                    },
                },
            }
        registry.append_event(
            session_id=session_id, event_type="status", seq=seq + 1, payload=terminal
        )
        registry.update_session_activity(session_id, awaiting="input")

    def settled(session_id, harness, turn_id):
        registry.create_session(
            session_id=session_id,
            host_id="fixture",
            harness=harness,
            command=harness,
            status="running",
            mode="structured",
            repo_owner="example",
            repo_name="project",
        )
        recorded_turn(session_id, harness, turn_id, 10)
        live[session_id] = {
            "session_id": session_id,
            "harness": harness,
            "status": "running",
            "mode": "structured",
            "awaiting": "input",
            "turn_active": False,
        }

    settled("resumable-work", "codex", "retained-work-turn")
    settled("resumable-verify", "claude-code", "retained-verify-turn")
    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry
    bridge.collector = SimpleNamespace(
        _harness_request=lambda host, path, **kwargs: (
            200,
            json.dumps(live[path.rsplit("/", 1)[-1]]),
        )
    )
    service.harness = bridge
    return client, binding, registry, live, settled, recorded_turn


def pilot_evidence(client, profile):
    client.evidence = dict(
        id=7,
        head_sha=SHA,
        name="owner-journey",
        app={"id": 42},
        external_id=profile_revision(profile),
        status="completed",
        conclusion="success",
        output={"summary": "journey passed", "text": "negative control failed"},
    )


def install_existing_binding(service, binding):
    config = service.config.goals[0].model_dump()
    config["github_existing_session_bindings"] = {"1": binding}
    scope = GoalScope.model_validate(config)
    service.config = service.config.model_copy(update={"goals": [scope]})


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


def test_explicit_independent_check_profile_does_not_require_human_review(
    service, monkeypatch
):
    client, verify = ready(service, monkeypatch, independent_profile())
    assert independent_verdict(service, verify) == {"outcome": "verified"}
    assert client.closed == 1
    assert not any("/reviews/" in path for _, path in client.reads)


def test_existing_completed_sessions_bind_once_without_relaunch(service, monkeypatch):
    profile = independent_profile()
    client = existing_pilot(service, monkeypatch, existing_binding(profile))

    bound = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)

    assert [row["run_role"] for row in bound["links"]] == ["work", "verify"]
    assert [row["session_id"] for row in bound["links"]] == [
        "existing-work-session",
        "existing-verify-session",
    ]
    assert service.harness.calls == []
    repeated = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    assert [(row["id"], row["run_key"]) for row in repeated["links"]] == [
        (row["id"], row["run_key"]) for row in bound["links"]
    ]
    binding_events = [
        event
        for link in bound["links"]
        for event in service.store.events(
            identity(link["run_key"], "bind-existing-session")
        )
    ]
    assert [
        (event["event_kind"], event["action_category"], event["outcome"])
        for event in binding_events
    ] == [
        ("policy", "bind_existing_session", "bound"),
        ("policy", "bind_existing_session", "bound"),
    ]
    assert {event["execution_link_id"] for event in binding_events} == {
        link["id"] for link in bound["links"]
    }

    client.evidence = dict(
        id=7,
        head_sha=SHA,
        name="owner-journey",
        app={"id": 42},
        external_id=profile_revision(profile),
        status="completed",
        conclusion="success",
        output={"summary": "journey passed", "text": "negative control failed"},
    )
    verify = bound["links"][1]
    assert independent_verdict(service, verify) == {"outcome": "verified"}
    assert independent_verdict(service, verify) == {"outcome": "verified"}
    assert client.closed == 1


def test_verified_accepts_current_strict_resumable_existing_pair(
    service, monkeypatch, pg_control_path
):
    """A proven parked turn is distinct from a completed session lifetime."""
    client, binding, _, _, _, _ = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    bound = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    pilot_evidence(client, independent_profile())

    verify = bound["links"][1]
    assert independent_verdict(service, verify) == {"outcome": "verified"}
    assert independent_verdict(service, verify) == {"outcome": "verified"}
    assert client.closed == 1
    assert binding["verifier_session_id"] == verify["session_id"]


@pytest.mark.parametrize("change", ["busy", "unavailable", "failed", "newer_input"])
def test_verified_rejects_unsafe_resumable_verifier_at_service_boundary(
    service, monkeypatch, pg_control_path, change
):
    client, _, registry, live, _, _ = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    verify = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)[
        "links"
    ][1]
    pilot_evidence(client, independent_profile())
    if change == "busy":
        live["resumable-verify"]["turn_active"] = True
    elif change == "unavailable":
        service.harness.collector = SimpleNamespace(
            _harness_request=lambda host, path, **kwargs: (503, "{}")
        )
    elif change == "failed":
        registry.append_event(
            session_id="resumable-verify",
            event_type="error",
            seq=12,
            payload={"returncode": 1},
        )
    else:
        registry.append_event(
            session_id="resumable-verify",
            event_type="user_input",
            seq=12,
            payload={"turn_id": "newer-turn"},
        )

    with pytest.raises(ValueError, match="existing verification session"):
        independent_verdict(service, verify)

    assert client.closed == 0


def test_running_unbound_verifier_cannot_use_existing_session_exception(
    service, monkeypatch
):
    client, verify = ready(service, monkeypatch, independent_profile())
    service.harness.sessions[verify["run_key"]].status = "running"

    with pytest.raises(ValueError, match="verification session must complete"):
        independent_verdict(service, verify)

    assert client.closed == 0


def test_existing_pair_replacement_keeps_old_links_and_rejects_them_at_verdict(
    service, monkeypatch, pg_control_path
):
    client, old_binding, _, _, settled, _ = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    old = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    profile = independent_profile()
    replacement = existing_binding(
        profile,
        work_session_id="replacement-work",
        work_turn_id="replacement-work-turn",
        verifier_session_id="replacement-verify",
        verifier_turn_id="replacement-verify-turn",
    )
    settled("replacement-work", "codex", "replacement-work-turn")
    settled("replacement-verify", "claude-code", "replacement-verify-turn")
    install_existing_binding(service, replacement)

    current = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    assert {row["id"] for row in old["links"]}.isdisjoint(
        {row["id"] for row in current["links"]}
    )
    assert all(len(service.store.link_events(row["id"])) == 1 for row in old["links"])
    pilot_evidence(client, profile)

    with pytest.raises(ValueError, match="binding superseded"):
        independent_verdict(service, old["links"][1])
    assert independent_verdict(service, current["links"][1]) == {"outcome": "verified"}
    assert client.closed == 1
    assert old_binding["source_head"] == replacement["source_head"] == SHA


def test_existing_pair_with_uncertain_close_cannot_be_replaced(
    service, monkeypatch, pg_control_path
):
    client, _, _, _, settled, _ = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    old = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    pilot_evidence(client, independent_profile())
    client.close_timeout = True
    with pytest.raises(TimeoutError):
        independent_verdict(service, old["links"][1])

    profile = independent_profile()
    replacement = existing_binding(
        profile,
        work_session_id="replacement-work",
        work_turn_id="replacement-work-turn",
        verifier_session_id="replacement-verify",
        verifier_turn_id="replacement-verify-turn",
    )
    settled("replacement-work", "codex", "replacement-work-turn")
    settled("replacement-verify", "claude-code", "replacement-verify-turn")
    install_existing_binding(service, replacement)

    with pytest.raises(RuntimeError, match="existing execution requires inspection"):
        service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    assert client.closed == 1


def test_existing_turn_rebind_reuses_session_without_unique_association_conflict(
    service, monkeypatch, pg_control_path
):
    """Later successful turns share the retained session without rewriting history."""
    client, old_binding, _, _, _, recorded_turn = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    old = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    recorded_turn("resumable-work", "codex", "later-work-turn", 12)
    recorded_turn("resumable-verify", "claude-code", "later-verify-turn", 12)
    profile = independent_profile()
    replacement = existing_binding(
        profile,
        work_session_id=old_binding["work_session_id"],
        work_turn_id="later-work-turn",
        verifier_session_id=old_binding["verifier_session_id"],
        verifier_turn_id="later-verify-turn",
    )
    install_existing_binding(service, replacement)

    current = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    repeated = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    assert [(row["id"], row["run_key"]) for row in repeated["links"]] == [
        (row["id"], row["run_key"]) for row in current["links"]
    ]
    assert [row["session_id"] for row in current["links"]] == [None, None]
    assert {row["id"] for row in old["links"]}.isdisjoint(
        {row["id"] for row in current["links"]}
    )
    assert all(len(service.store.link_events(row["id"])) == 1 for row in old["links"])
    pilot_evidence(client, profile)

    with pytest.raises(ValueError, match="binding superseded"):
        independent_verdict(service, old["links"][1])
    assert independent_verdict(service, current["links"][1]) == {"outcome": "verified"}
    assert independent_verdict(service, current["links"][1]) == {"outcome": "verified"}
    assert client.closed == 1


def test_existing_turn_rebind_recovers_bind_only_partial_work_link(
    service, monkeypatch, pg_control_path
):
    """Retry the exact persisted state left before a former association collision."""
    _, old_binding, _, _, _, recorded_turn = resumable_existing_pilot(
        service, monkeypatch, pg_control_path
    )
    old = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    recorded_turn("resumable-work", "codex", "later-work-turn", 12)
    recorded_turn("resumable-verify", "claude-code", "later-verify-turn", 12)
    replacement = existing_binding(
        independent_profile(),
        work_session_id=old_binding["work_session_id"],
        work_turn_id="later-work-turn",
        verifier_session_id=old_binding["verifier_session_id"],
        verifier_turn_id="later-verify-turn",
    )
    install_existing_binding(service, replacement)
    _, _, ref, _ = service.item("goal", 1)
    scope = service.config.goals[0]
    request = identity(
        "existing-session",
        replacement["profile_revision"],
        replacement["source_head"],
        replacement["work_session_id"],
        replacement["work_turn_id"],
    )
    work_key = identity(service.config.workspace_id, "goal", 1, request, "work")
    partial = service.store.intent(
        ACTOR.principal,
        ref,
        "goal",
        work_key,
        "work",
        0,
        scope.goal_label,
    )
    service.store.bind_existing_session(
        ACTOR.principal,
        ref,
        identity(work_key, "bind-existing-session"),
        partial["id"],
        service._existing_binding_payload(
            GitHubExistingSessionBinding.model_validate(replacement), role="work"
        ),
    )
    assert partial["session_id"] is None
    assert len(service.store.link_events(partial["id"])) == 1

    recovered = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    repeated = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)
    assert recovered["links"][0]["id"] == partial["id"]
    assert recovered["links"][0]["session_id"] is None
    assert recovered["links"][1]["session_id"] is None
    assert [(row["id"], row["run_key"]) for row in repeated["links"]] == [
        (row["id"], row["run_key"]) for row in recovered["links"]
    ]
    assert all(len(service.store.link_events(row["id"])) == 1 for row in old["links"])


def profile_revision(profile):
    return GitHubAcceptanceProfile.model_validate(profile).revision


@pytest.mark.parametrize(
    "change,error",
    [
        ({"source_head": "b" * 40}, "source changed"),
    ],
)
def test_existing_session_binding_rejects_wrong_reviewed_contract(
    service, monkeypatch, change, error
):
    profile = independent_profile()
    client = existing_pilot(service, monkeypatch, existing_binding(profile, **change))

    with pytest.raises((ValueError, RuntimeError), match=error):
        service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)

    assert service.store.links() == []
    assert client.closed == 0


@pytest.mark.parametrize(
    "change,error",
    [
        ({"profile_revision": "0" * 64}, "profile revision"),
        ({"verifier_session_id": "existing-work-session"}, "separate"),
    ],
)
def test_existing_session_binding_rejects_invalid_installed_contract(
    service, monkeypatch, change, error
):
    from pydantic import ValidationError

    profile = independent_profile()
    with pytest.raises(ValidationError, match=error):
        existing_pilot(service, monkeypatch, existing_binding(profile, **change))


@pytest.mark.parametrize("change", ["wrong_repo", "active", "queued", "missing_turn"])
def test_existing_session_binding_requires_terminal_persisted_sessions(
    service, monkeypatch, change
):
    profile = independent_profile()
    binding = existing_binding(profile)
    client = existing_pilot(service, monkeypatch, binding)
    session = service.harness.existing_sessions[
        (binding["work_session_id"], binding["work_turn_id"])
    ]
    if change == "wrong_repo":
        session.repo = "other/project"
    elif change == "active":
        session.status = "running"
    elif change == "queued":
        session.status = "created"
    else:
        session.turn_completed = False

    with pytest.raises(ValueError, match="existing work session"):
        service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)

    assert service.store.links() == []
    assert client.closed == 0


def test_existing_session_binding_rechecks_terminal_provenance_at_verdict(
    service, monkeypatch
):
    profile = independent_profile()
    binding = existing_binding(profile)
    client = existing_pilot(service, monkeypatch, binding)
    verify = service.call("bind_existing", {"goal_id": "goal", "number": 1}, ACTOR)[
        "links"
    ][1]
    client.evidence = dict(
        id=7,
        head_sha=SHA,
        name="owner-journey",
        app={"id": 42},
        external_id=profile_revision(profile),
        status="completed",
        conclusion="success",
        output={"summary": "journey passed", "text": "negative control failed"},
    )
    service.harness.existing_sessions[
        (binding["verifier_session_id"], binding["verifier_turn_id"])
    ].turn_completed = False

    with pytest.raises(ValueError, match="existing verification session"):
        independent_verdict(service, verify)

    assert client.closed == 0


def test_existing_session_reader_requires_exact_persisted_terminal_turn(
    pg_control_path,
):
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="fixture", display_name="Fixture", kind="macos")
    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry

    def session(name):
        registry.create_session(
            session_id=name,
            host_id="fixture",
            harness="codex",
            command="codex",
            status="completed",
            repo_owner="example",
            repo_name="project",
        )

    def input_turn(name, turn):
        registry.append_event(
            session_id=name, event_type="user_input", payload={"turn_id": turn}
        )

    def terminal_turn(name, turn, exited):
        registry.append_event(
            session_id=name,
            event_type="status",
            payload={"turn_id": turn, "payload": {"exited": exited}},
        )

    session("existing-registry-session")
    input_turn("existing-registry-session", "retained-turn")
    terminal_turn("existing-registry-session", "retained-turn", 0)
    assert (
        bridge.existing_session(
            "existing-registry-session", "retained-turn", "example/project"
        ).session_id
        == "existing-registry-session"
    )
    assert (
        bridge.existing_session(
            "existing-registry-session", "wrong-turn", "example/project"
        )
        is None
    )

    # A newer queued or active input invalidates the older terminal result,
    # even while a stale session row still says completed.
    input_turn("existing-registry-session", "newer-queued-or-active-turn")
    assert (
        bridge.existing_session(
            "existing-registry-session", "retained-turn", "example/project"
        )
        is None
    )

    session("existing-failed-session")
    input_turn("existing-failed-session", "retained-turn")
    terminal_turn("existing-failed-session", "retained-turn", 1)
    assert (
        bridge.existing_session(
            "existing-failed-session", "retained-turn", "example/project"
        )
        is None
    )

    session("existing-missing-status-session")
    input_turn("existing-missing-status-session", "retained-turn")
    assert (
        bridge.existing_session(
            "existing-missing-status-session", "retained-turn", "example/project"
        )
        is None
    )

    session("existing-missing-input-session")
    terminal_turn("existing-missing-input-session", "retained-turn", 0)
    assert (
        bridge.existing_session(
            "existing-missing-input-session", "retained-turn", "example/project"
        )
        is None
    )

    session("existing-completed-only-session")
    assert (
        bridge.existing_session(
            "existing-completed-only-session", "retained-turn", "example/project"
        )
        is None
    )
    assert (
        bridge.existing_session(
            "existing-registry-session", "retained-turn", "other/project"
        )
        is None
    )


def test_existing_session_reader_accepts_only_an_idle_structured_turn(
    pg_control_path,
):
    """A resumable Claude session is not a completed lifetime session.

    Claude's structured result has no outer turn id.  The exact preceding
    durable input and the host's ``turn_active: false`` reading are therefore
    both required before this narrow existing-session reader can reuse it.
    """
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="fixture", display_name="Fixture", kind="macos")
    registry.create_session(
        session_id="resumable-claude",
        host_id="fixture",
        harness="claude-code",
        command="claude",
        status="running",
        mode="structured",
        repo_owner="example",
        repo_name="project",
    )
    registry.append_event(
        session_id="resumable-claude",
        event_type="user_input",
        seq=2619,
        payload={"turn_id": "retained-turn"},
    )
    registry.append_event(
        session_id="resumable-claude",
        event_type="status",
        seq=2754,
        payload={
            "type": "status",
            "role": "system",
            "turn_id": None,
            "payload": {
                "turn_complete": True,
                "awaiting": "input",
                "result": {
                    "type": "result",
                    "is_error": False,
                    "subtype": "success",
                    "terminal_reason": "completed",
                    "queued_turn_count": 0,
                },
            },
        },
    )
    registry.update_session_activity("resumable-claude", awaiting="input")

    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry
    bridge.collector = SimpleNamespace(
        _harness_request=lambda host, path, **kwargs: (
            200,
            json.dumps(
                {
                    "session_id": "resumable-claude",
                    "harness": "claude-code",
                    "status": "running",
                    "mode": "structured",
                    "awaiting": "input",
                    "turn_active": False,
                }
            ),
        )
    )

    accepted = bridge.existing_session(
        "resumable-claude", "retained-turn", "example/project"
    )
    assert accepted is not None
    assert accepted.status == "running"


def test_existing_session_reader_accepts_idle_resumable_codex_exit(pg_control_path):
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="fixture", display_name="Fixture", kind="macos")
    registry.create_session(
        session_id="resumable-codex",
        host_id="fixture",
        harness="codex",
        command="codex",
        status="running",
        mode="structured",
        repo_owner="example",
        repo_name="project",
    )
    registry.append_event(
        session_id="resumable-codex",
        event_type="user_input",
        seq=797,
        payload={"turn_id": "correlated-turn"},
    )
    registry.append_event(
        session_id="resumable-codex",
        event_type="status",
        seq=798,
        payload={
            "type": "status",
            "role": "system",
            "turn_id": "correlated-turn",
            "payload": {"exited": 0},
        },
    )
    registry.update_session_activity("resumable-codex", awaiting="input")
    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry
    bridge.collector = SimpleNamespace(
        _harness_request=lambda host, path, **kwargs: (
            200,
            json.dumps(
                {
                    "session_id": "resumable-codex",
                    "harness": "codex",
                    "status": "running",
                    "mode": "structured",
                    "awaiting": "input",
                    "turn_active": False,
                }
            ),
        )
    )

    assert (
        bridge.existing_session("resumable-codex", "correlated-turn", "example/project")
        is not None
    )


@pytest.mark.parametrize(
    "change",
    [
        "busy",
        "unknown_live_state",
        "error_result",
        "wrong_terminal_reason",
        "queued_turn",
        "missing_result",
        "newer_input",
        "post_turn_error",
    ],
)
def test_existing_session_reader_rejects_unsafe_resumable_turns(
    pg_control_path, change
):
    registry = HarnessRegistry(pg_control_path)
    registry.register_host(host_id="fixture", display_name="Fixture", kind="macos")
    registry.create_session(
        session_id="resumable-unsafe",
        host_id="fixture",
        harness="claude-code",
        command="claude",
        status="running",
        mode="structured",
        repo_owner="example",
        repo_name="project",
    )
    registry.append_event(
        session_id="resumable-unsafe",
        event_type="user_input",
        seq=10,
        payload={"turn_id": "retained-turn"},
    )
    result = {
        "type": "result",
        "is_error": False,
        "subtype": "success",
        "terminal_reason": "completed",
        "queued_turn_count": 0,
    }
    if change == "error_result":
        result["is_error"] = True
    elif change == "wrong_terminal_reason":
        result["terminal_reason"] = "interrupted"
    elif change == "queued_turn":
        result["queued_turn_count"] = 1
    terminal = {
        "type": "status",
        "role": "system",
        "turn_id": None,
        "payload": {"turn_complete": True, "awaiting": "input", "result": result},
    }
    if change == "missing_result":
        terminal["payload"] = {"turn_complete": True, "awaiting": "input"}
    registry.append_event(
        session_id="resumable-unsafe", event_type="status", seq=11, payload=terminal
    )
    if change == "newer_input":
        registry.append_event(
            session_id="resumable-unsafe",
            event_type="user_input",
            seq=12,
            payload={"turn_id": "newer-turn"},
        )
    elif change == "post_turn_error":
        registry.append_event(
            session_id="resumable-unsafe",
            event_type="error",
            seq=12,
            payload={"returncode": 1},
        )
    registry.update_session_activity("resumable-unsafe", awaiting="input")
    live_turn = None if change == "unknown_live_state" else change == "busy"
    bridge = object.__new__(HarnessBridge)
    bridge.registry = registry
    bridge.collector = SimpleNamespace(
        _harness_request=lambda host, path, **kwargs: (
            200,
            json.dumps(
                {
                    "session_id": "resumable-unsafe",
                    "harness": "claude-code",
                    "status": "running",
                    "mode": "structured",
                    "awaiting": "input",
                    "turn_active": live_turn,
                }
            ),
        )
    )

    assert (
        bridge.existing_session("resumable-unsafe", "retained-turn", "example/project")
        is None
    )


def test_independent_check_mode_still_requires_configured_producer(
    service, monkeypatch
):
    client, verify = ready(service, monkeypatch, independent_profile())
    client.evidence["app"] = {"id": 99}
    with pytest.raises(ValueError, match="behavioral evidence"):
        independent_verdict(service, verify)
    assert client.closed == 0


def test_human_review_mode_rejects_check_only_evidence(service, monkeypatch):
    client, verify = ready(service, monkeypatch)
    with pytest.raises(ValueError, match="resolvable provider evidence"):
        independent_verdict(service, verify)
    assert client.closed == 0


def test_unsupported_review_mode_cannot_be_installed(service):
    from pydantic import ValidationError

    config = service.config.goals[0].model_dump()
    profile = dict(PROFILE, review_mode="automatic")
    config["github_acceptance_profiles"] = {"1": profile}
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    with pytest.raises(ValidationError):
        GoalScope.model_validate(config)


def test_human_review_mode_requires_a_configured_human(service):
    from pydantic import ValidationError

    config = service.config.goals[0].model_dump()
    profile = dict(PROFILE)
    profile.pop("owner_id")
    config["github_acceptance_profiles"] = {"1": profile}
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    with pytest.raises(ValidationError, match="requires an owner"):
        GoalScope.model_validate(config)


def test_independent_mode_cannot_select_a_human_reviewer(service):
    from pydantic import ValidationError

    config = service.config.goals[0].model_dump()
    config["github_acceptance_profiles"] = {
        "1": dict(PROFILE, review_mode="independent_check")
    }
    config["credential"]["capabilities"] += ["pulls_read", "checks_read"]
    with pytest.raises(ValidationError, match="cannot select a human"):
        GoalScope.model_validate(config)


def test_p1_profile_uses_the_independent_check_revision_contract():
    profile_path = (
        Path(__file__).parents[1]
        / "docs/quality-pilots/p1-runner/profile-candidate.json"
    )
    raw = json.loads(profile_path.read_text())
    profile = GitHubAcceptanceProfile.model_validate(raw)
    assert profile.review_mode == "independent_check"
    assert profile.owner_id is None
    assert (
        profile.revision
        == hashlib.sha256(
            json.dumps(dict(raw, owner_id=None), sort_keys=True).encode()
        ).hexdigest()
    )


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
