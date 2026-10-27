"""Phase 2 exit checks against the real control Postgres store."""

import json
from types import SimpleNamespace

import pytest

from drover.server.work_queue.config import WorkQueueConfig, load_policy
from drover.server.work_queue.service import WorkActor, WorkService, identity, route
from drover.server.work_queue.store import WorkStore


class Provider:
    def __init__(self):
        self.data = {
            "number": 1,
            "state": "open",
            "body": "Public behavioral acceptance",
            "labels": [{"name": "goal:test"}, {"name": "status:queued"}],
        }
        self.comments = []
        self.closed = 0

    def authenticate(self):
        pass

    def get_item(self, ref):
        return self.data

    def list_items(self):
        return [self.data]

    def set_labels(self, ref, labels):
        self.data["labels"] = [{"name": l} for l in labels]

    def comment(self, ref, text):
        self.comments.append(text)

    def close(self, ref):
        self.closed += 1
        self.data["state"] = "closed"

    def inspect_action(self, ref, action):
        return {"supported": False}


class Harness:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.timeout = False

    def resolve(self, key):
        return self.sessions.get(key)

    def active(self, links, *, release_finalised=True):
        return [
            l
            for l in links
            if not l["session_id"] or self.sessions[l["run_key"]].status == "running"
        ]

    def quota(self):
        return [
            {
                "provider": "openai",
                "status": "ok",
                "hosts": [{"host_id": "host"}],
                "windows": [{"kind": "primary", "status": "ok", "used_percent": 10}],
            }
        ]

    def eligible(self, goal_id=None, parent_session_id=None):
        return {("host", "codex")}

    def launch(self, host, harness, key, ref, role, parent, goal_id, *, authority=None):
        self.calls.append((key, role, authority))
        session = SimpleNamespace(
            session_id="session-" + str(len(self.calls)),
            parent_session_id=parent,
            status="running",
            host_id=host,
        )
        self.sessions[key] = session
        if self.timeout:
            raise TimeoutError("private remote body must never be reflected")
        return session.session_id


ACTOR = WorkActor("credential:coordinator", "nix")
HUMAN = WorkActor("credential:device", "human", True)


@pytest.fixture
def service(pg_control_path):
    cfg = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="test-workspace",
        goals=[
            {
                "goal_id": "goal",
                "repo": "example/project",
                "goal_label": "goal:test",
                "credential": {
                    "token_env": "TEST_TOKEN",
                    "capabilities": ["issues_read", "issues_write"],
                },
            }
        ],
    )
    return WorkService(
        cfg,
        WorkStore(pg_control_path, cfg.workspace_id),
        {"goal": Provider()},
        Harness(),
    )


def launch(service, key="first"):
    return service.call(
        "launch",
        {"goal_id": "goal", "number": 1, "request_key": key, "estimated_cost": 5},
        ACTOR,
    )


def update(service, row, action, **kw):
    return service.call(
        "update",
        dict(goal_id="goal", number=1, run_key=row["run_key"], action=action, **kw),
        ACTOR,
    )


def complete(service, row):
    service.harness.sessions[row["run_key"]].status = "completed"
    return update(service, row, "complete", estimated_cost=5)


def test_restart_resolves_committed_intent_without_relaunch(service):
    service.harness.timeout = True
    with pytest.raises(TimeoutError):
        launch(service)
    intent = service.store.links()[0]
    assert intent["session_id"] is None
    assert intent["workspace_id"] == "test-workspace"
    assert intent["actor_principal"] == ACTOR.principal
    restarted = WorkService(
        service.config,
        WorkStore(service.store.path, "test-workspace"),
        service.providers,
        service.harness,
    )
    linked = launch(restarted)
    assert linked["session_id"] == "session-1"
    assert len(service.harness.calls) == 1
    assert launch(restarted)["id"] == linked["id"]
    assert len(service.harness.calls) == 1


def test_uncertain_intent_blocks_even_a_different_request_key(service):
    service.harness.timeout = True
    with pytest.raises(TimeoutError):
        launch(service)
    service.harness.sessions.clear()
    with pytest.raises(RuntimeError, match="uncertain"):
        launch(service)
    with pytest.raises(RuntimeError, match="inspection"):
        launch(service, "another")
    assert len(service.harness.calls) == 1


def test_failed_behavioral_check_cannot_close(service):
    work = launch(service)
    verify = complete(service, work)
    assert verify["run_role"] == "verify"
    assert verify["session_id"] != work["session_id"]
    assert len(service.harness.calls) == 2
    assert complete(service, work)["id"] == verify["id"]
    with pytest.raises(ValueError, match="behavioral"):
        update(
            service,
            verify,
            "verified",
            behavioral_passed=False,
            checks_rerun=True,
            evidence_key="check:failed",
        )
    assert service.providers["goal"].closed == 0
    update(service, verify, "failed")
    assert "status:queued" in {
        l["name"] for l in service.providers["goal"].data["labels"]
    }
    assert service.providers["goal"].closed == 0


def test_separate_success_and_changed_criteria(service):
    work = launch(service)
    with pytest.raises(ValueError, match="separate"):
        update(
            service,
            work,
            "verified",
            behavioral_passed=True,
            checks_rerun=True,
            evidence_key="check:pass",
        )
    verify = complete(service, work)
    service.harness.sessions[verify["run_key"]].status = "completed"
    service.providers["goal"].data["body"] = "Amended public criteria"
    with pytest.raises(ValueError, match="criteria changed"):
        update(
            service,
            verify,
            "verified",
            behavioral_passed=True,
            checks_rerun=True,
            evidence_key="check:pass",
        )
    service.providers["goal"].data["body"] = "Public behavioral acceptance"
    update(
        service,
        verify,
        "verified",
        behavioral_passed=True,
        checks_rerun=True,
        evidence_key="check:pass",
    )
    update(
        service,
        verify,
        "verified",
        behavioral_passed=True,
        checks_rerun=True,
        evidence_key="check:pass",
    )
    assert service.providers["goal"].closed == 1


def test_third_failure_blocks_and_writes_systemic_review(service):
    for attempt in range(load_policy().retries.attempts):
        row = launch(service, f"attempt:{attempt}")
        service.harness.sessions[row["run_key"]].status = "failed"
        update(service, row, "failed")
        update(service, row, "failed")
    provider = service.providers["goal"]
    assert "status:blocked" in {l["name"] for l in provider.data["labels"]}
    assert len(provider.comments) == 1
    assert "Systemic review" in provider.comments[0]
    with pytest.raises(RuntimeError, match="systemic review"):
        launch(service, "fourth")
    assert len(service.harness.calls) == 3
    assert max(l["failure_count"] for l in service.store.links()) == 3


def test_launch_passes_issue_authority_to_harness(service):
    service.providers["goal"].data["labels"].append({"name": "autonomy:through-merge"})
    service.providers["goal"].data[
        "body"
    ] = "Public behavioral acceptance\nHard stop: gateway restart"
    launch(service)
    authority = service.harness.calls[0][2]
    assert authority == {
        "labels": ["goal:test", "status:queued", "autonomy:through-merge"],
        "body": "Public behavioral acceptance\nHard stop: gateway restart",
    }


def test_audit_metadata_only_and_append_only(service):
    private = "Private context /Users/private/person secret=value"
    with pytest.raises(TypeError):
        service.call(
            "question",
            dict(
                goal_id="goal",
                number=1,
                request_key="request:1",
                category="revision",
                options=["reject"],
                text=private,
            ),
            ACTOR,
        )
    question = service.call(
        "question",
        dict(
            goal_id="goal",
            number=1,
            request_key="request:1",
            category="revision",
            options=["reject"],
        ),
        ACTOR,
    )
    args = dict(
        goal_id="goal", number=1, decision_id=question["decision_id"], action="reject"
    )
    service.call("decide", args, HUMAN)
    service.call("decide", args, HUMAN)
    events = service.store.events(question["decision_id"])
    assert [e["event_kind"] for e in events] == ["question", "intent", "outcome"]
    assert all(
        e["workspace_id"] == "test-workspace"
        and e["actor_principal"].startswith("credential:")
        for e in events
    )
    assert private not in json.dumps(events, default=str)
    assert not {"body", "text", "comment", "status"} & events[0].keys()
    with service.store.connection() as con:
        with pytest.raises(Exception, match="append only"):
            con.execute(
                "UPDATE decision_events SET outcome = 'reject' WHERE workspace_id = ?",
                ["test-workspace"],
            )
        with pytest.raises(Exception, match="append only"):
            con.execute(
                "DELETE FROM decision_events WHERE workspace_id = ?", ["test-workspace"]
            )


def test_workspace_scope_and_session_uniqueness(service):
    row = launch(service)
    assert WorkStore(service.store.path, "another-workspace").links() == []
    with pytest.raises(PermissionError):
        service.call("get", {"goal_id": "other", "number": 1}, ACTOR)
    with pytest.raises(PermissionError):
        launch_args = dict(
            goal_id="goal", number=1, request_key="new", estimated_cost=5
        )
        service.call("launch", launch_args, WorkActor("credential:worker", "worker"))
    with service.store.connection() as con:
        with pytest.raises(Exception, match="unique"):
            con.execute(
                "INSERT INTO execution_links SELECT 'new-id', workspace_id, actor_principal, provider, repo, item_number, item_url, goal_id, goal_ref, session_id, pr_ref, 'new-key', run_role, failure_count, last_progress_at, created_at, updated_at FROM execution_links WHERE id = ?",
                [row["id"]],
            )


def test_quota_reserve_unknown_and_exact_fraction(service):
    accounts = service.harness.quota()
    assert route(accounts, load_policy(), 72, service.harness.eligible()) == (
        "host",
        "codex",
    )
    with pytest.raises(RuntimeError):
        route(accounts, load_policy(), 72.1, service.harness.eligible())
    accounts[0]["windows"][0]["used_percent"] = 10.4
    with pytest.raises(RuntimeError):
        route(accounts, load_policy(), 72, service.harness.eligible())
    accounts[0]["status"] = "usage_unavailable"
    with pytest.raises(RuntimeError):
        route(accounts, load_policy(), 1, service.harness.eligible())
    with pytest.raises(RuntimeError):
        route(service.harness.quota(), load_policy(), 1, set())


def test_opt_in_execution_uses_launch_and_respects_budget_caps_and_kill_switch(service):
    from drover.server.work_queue.reconciler import QueueExecutionCoordinator

    scope = service.config.goals[0].model_copy(
        update={
            "execution_enabled": True,
            "execution_budget": 5.0,
            "execution_estimated_cost": 5.0,
            "execution_max_workers": 1,
        }
    )
    service.config = service.config.model_copy(
        update={
            "goals": [scope],
            "execution": service.config.execution.model_copy(
                update={"global_max_workers": 1}
            ),
        }
    )
    coordinator = QueueExecutionCoordinator(service)
    assert coordinator.launch_candidate("goal", 1).get("launched") is True
    assert coordinator.launch_candidate("goal", 1)["denied"] == "global_cap"
    service.config = service.config.model_copy(
        update={
            "execution": service.config.execution.model_copy(
                update={"kill_switch": True}
            )
        }
    )
    assert (
        QueueExecutionCoordinator(service).launch_candidate("goal", 1)["denied"]
        == "kill_switch"
    )


def test_launch_denials_are_logged_and_counted_with_goal_and_issue(service, caplog):
    from drover.server.work_queue.reconciler import QueueExecutionCoordinator

    coordinator = QueueExecutionCoordinator(service)
    assert coordinator.launch_candidate("goal", 1)["denied"] == "execution_disabled"
    scope = service.config.goals[0].model_copy(update={"execution_enabled": True})
    service.config = service.config.model_copy(
        update={
            "goals": [scope],
            "shadow_mode": False,
            "execution": service.config.execution.model_copy(
                update={"kill_switch": True}
            ),
        }
    )
    assert coordinator.launch_candidate("goal", 1)["denied"] == "kill_switch"
    service.config = service.config.model_copy(
        update={
            "execution": service.config.execution.model_copy(
                update={"kill_switch": False, "global_max_workers": 1}
            )
        }
    )
    launch(service)
    assert coordinator.launch_candidate("goal", 2)["denied"] == "global_cap"
    for session in service.harness.sessions.values():
        session.status = "completed"
    scope = scope.model_copy(
        update={"execution_budget": 1.0, "execution_estimated_cost": 2.0}
    )
    service.config = service.config.model_copy(update={"goals": [scope]})
    assert coordinator.launch_candidate("goal", 3)["denied"] == "budget"
    assert coordinator.health == {
        "launches": 0,
        "cap_denials": 1,
        "budget_denials": 1,
        "execution_disabled_denials": 1,
        "kill_switch_denials": 1,
    }
    assert "goal_id=goal issue_number=3" in caplog.text


def test_policy_capacity_and_retry_override(service, tmp_path):
    data = load_policy().model_dump()
    data["capacity"]["workspace_workers"] = 1
    data["retries"]["attempts"] = 1
    data["quota"]["reserve_fraction"] = 0.5
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    scope = service.config.goals[0].model_copy(update={"policy_path": str(path)})
    service.config = service.config.model_copy(update={"goals": [scope]})
    with pytest.raises(RuntimeError, match="quota"):
        service.call(
            "launch",
            dict(goal_id="goal", number=1, request_key="big", estimated_cost=46),
            ACTOR,
        )
    row = launch(service)
    service.providers["goal"].set_labels(None, ["goal:test", "status:queued"])
    with pytest.raises(RuntimeError, match="capacity"):
        launch(service, "second")
    service.harness.sessions[row["run_key"]].status = "failed"
    update(service, row, "failed")
    assert service.providers["goal"].comments


def test_finalised_worker_frees_the_slot_but_not_the_item(service):
    launch(service)
    original = service.harness.active
    # The first worker declared done: capacity ignores it, duplicates do not.
    service.harness.active = lambda links, *, release_finalised=True: (
        [] if release_finalised else original(links)
    )
    # The owner re-queues the item while the finalised session still exists.
    provider = service.providers["goal"]
    provider.set_labels(None, ["goal:test", "status:queued"])
    with pytest.raises(RuntimeError, match="existing execution requires inspection"):
        launch(service, "another")


def test_sessionless_intent_is_retried_without_duplicate_execution(service):
    service.harness.timeout = True
    with pytest.raises(TimeoutError):
        launch(service)
    intent = service.store.links()[0]
    assert intent["session_id"] is None
    service.harness.timeout = False
    retried = launch(service)
    assert retried["session_id"] == "session-2"
    assert len(service.store.links()) == 1
