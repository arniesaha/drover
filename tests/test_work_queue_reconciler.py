import json
import threading
from types import SimpleNamespace

import pytest
import requests
from test_work_queue_policy import scope

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore
from drover.server.work_queue.config import WorkQueueConfig, load_policy
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.reconciler import (
    ShadowReconciler,
    start_shadow_reconciler,
)


def issue(number=7, *labels, state="open", body="public acceptance"):
    return {
        "number": number,
        "state": state,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "created_at": "2026-10-13T00:00:00Z",
    }


class ReadOnlyProvider:
    actor_principal = "github:user:42"

    def __init__(self, workspace_id, scope):
        self.workspace_id = workspace_id
        self.capabilities = frozenset(scope.credential.capabilities)
        self.items = [issue(7, "status:queued", "agent:worker", "goal:2026-w42")]
        self.attention = []
        self.unavailable = False
        self.polls = 0

    def authenticate(self):
        if self.unavailable:
            raise ProviderUnavailable("private upstream error")

    def list_items(self):
        self.polls += 1
        return self.items

    def list_attention(self):
        return self.attention

    # Any attempted mutation or launch is a test failure.
    def __getattr__(self, name):
        raise AssertionError("unexpected provider operation: " + name)


def service(*goals, clock=lambda: 1000):
    config = WorkQueueConfig(
        enabled=True, workspace_id="workspace-test", goals=list(goals or [scope()])
    )
    return ShadowReconciler(
        config,
        service_principal="control:server:synthetic-test-identity",
        provider_factory=ReadOnlyProvider,
        clock=clock,
    )


def test_repeated_polls_are_level_triggered_without_duplicate_actions():
    r = service()
    first = r.poll()
    action = first["actions"][0]
    assert action["kind"] == "launch_candidate"
    assert first["new_action_ids"] == [action["id"]]
    for _ in range(3):
        again = r.poll()
        assert again["actions"] == first["actions"]
        assert again["new_action_ids"] == []
        assert again["actionable_count"] == 1
        assert not again["actions"][0]["execution_allowed"]
    assert r.providers["goal-1"].polls == 4
    assert action["workspace_id"] == "workspace-test"
    assert action["actor_principal"] == "github:user:42"


@pytest.mark.parametrize(
    "labels,reason",
    [
        (["status:queued", "status:running"], "invalid_status_labels"),
        (["status:unknown"], "invalid_status_labels"),
        ([], "invalid_status_labels"),
        (["status:queued", "agent:nix", "agent:jenny"], "invalid_agent_labels"),
        (["status:queued", "agent:unknown"], "invalid_agent_labels"),
        (
            ["status:queued", "priority:normal", "priority:urgent"],
            "invalid_priority_labels",
        ),
        (["status:queued", "priority:unknown"], "invalid_priority_labels"),
        (["status:queued", "goal:first", "goal:second"], "conflicting_goal_labels"),
    ],
)
def test_conflicts_and_unknown_labels_are_diagnosed_never_guessed(labels, reason):
    r = service()
    r.providers["goal-1"].items = [issue(7, *labels)]
    report = r.poll()
    assert report["actions"] == []
    diagnostic = report["goals"][0]["diagnostics"][0]
    assert diagnostic["kind"] == reason
    assert diagnostic["workspace_id"] == "workspace-test"
    assert diagnostic["actor_principal"] == "github:user:42"


def test_refresh_age_and_failed_refresh_preserve_last_success_without_private_text():
    now = [1000]
    r = service(clock=lambda: now[0])
    assert r.poll()["goals"][0]["refresh_age_seconds"] == 0
    now[0] += 121
    aged = r.report()
    assert aged["goals"][0]["refresh_age_seconds"] == 121
    assert aged["goals"][0]["stale"]
    assert aged["actions"][0]["refresh_age_seconds"] == 121
    r.providers["goal-1"].unavailable = True
    for _ in range(2):
        failed = r.poll()
        assert failed["goals"][0]["refresh_age_seconds"] == 121
        assert failed["goals"][0]["diagnostics"][0]["kind"] == "refresh_failed"
        assert failed["actions"] == []
        assert "private upstream" not in json.dumps(failed)
    r.providers["goal-1"].unavailable = False
    assert r.poll()["new_action_ids"] == []
    assert not r.report()["goals"][0]["stale"]


def test_unavailable_initial_refresh_reports_unknown_age():
    r = service()
    r.providers["goal-1"].unavailable = True
    first = r.poll()["goals"][0]
    assert first["refresh_age_seconds"] is None and first["stale"]


def test_closed_blocked_running_review_and_changed_criteria():
    r = service()
    p = r.providers["goal-1"]
    p.items = [
        issue(1, "status:queued", state="closed"),
        issue(2, "status:blocked"),
        issue(3, "status:running"),
        issue(4, "status:review"),
    ]
    first = r.poll()
    assert [a["kind"] for a in first["actions"]] == [
        "inspect_execution",
        "verify_candidate",
    ]
    assert all(not a["execution_allowed"] for a in first["actions"])
    p.items[-1]["body"] = "changed public acceptance"
    revised = r.poll()
    assert len(revised["new_action_ids"]) == 1
    assert revised["new_action_ids"][0] != first["actions"][-1]["id"]
    assert "changed public acceptance" not in json.dumps(revised)
    p.items[-1]["state"] = "closed"
    assert r.poll()["actionable_count"] == 1


def test_environment_approval_and_overlapping_scopes_are_deduplicated():
    r = service(scope(), scope(goal_id="other-goal"))
    entry = {
        "source_key": "environment:9:1:10",
        "kind": "environment_approval",
        "url": "https://github.com/example/project/actions/runs/9",
        "updated_at": "2026-10-13T00:00:00Z",
    }
    for p in r.providers.values():
        p.attention = [entry, entry]
    first = r.poll()
    assert first["actionable_count"] == 2
    assert len(first["new_action_ids"]) == 2
    assert r.poll()["new_action_ids"] == []


def test_policy_threshold_and_default_priority_are_read_from_goal_config(tmp_path):
    data = load_policy().model_dump()
    data["poll"]["freshness_seconds"] = 8
    data["priority"]["default"] = "urgent"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    now = [1000]
    r = service(scope(policy_path=str(path)), clock=lambda: now[0])
    assert r.poll()["actions"][0]["priority"] == "urgent"
    now[0] += 9
    assert r.report()["goals"][0]["stale"]
    path.write_text("invalid policy")
    assert r.poll()["goals"][0]["diagnostics"][0]["kind"] == "refresh_failed"


def test_disabled_flag_starts_no_worker_and_stopped_loop_does_no_reads():
    assert (
        start_shadow_reconciler(SimpleNamespace(), WorkQueueConfig(), threading.Event())
        is None
    )
    r = service()
    stop = threading.Event()
    stop.set()
    r.run(stop)
    assert r.providers["goal-1"].polls == 0


def test_control_report_route_requires_paired_device_and_is_read_only(tmp_path):
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, device_token = credentials.issue(scope="device", label="test device")
    _, host_token = credentials.issue(
        scope="host", label="test host", host_id="test-host"
    )
    auth = AuthSettings(
        enabled=True, api_token="synthetic-legacy-token", credentials=credentials
    )
    r = service()
    r.poll()
    collector = SimpleNamespace(work_queue=r)
    httpd = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=auth
    )
    url = f"http://127.0.0.1:{httpd.server_port}/work-queue/report"
    try:
        for token, expected in [
            (None, 401),
            ("synthetic-legacy-token", 403),
            (host_token, 403),
            (device_token, 200),
        ]:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=5,
            )
            assert response.status_code == expected
        assert response.json()["actions"][0]["workspace_id"] == "workspace-test"
        assert response.headers["Cache-Control"] == "no-store"
        assert r.providers["goal-1"].polls == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_report_does_not_wait_for_a_slow_provider_refresh():
    r = service()
    initial = r.poll()
    started = threading.Event()
    release = threading.Event()
    read_done = threading.Event()
    result = []
    provider = r.providers["goal-1"]
    original = provider.list_items

    def slow_read():
        started.set()
        assert release.wait(5)
        return original()

    provider.list_items = slow_read
    poller = threading.Thread(target=r.poll)
    poller.start()
    reader = None
    try:
        assert started.wait(2)

        def read_report():
            result.append(r.report())
            read_done.set()

        reader = threading.Thread(target=read_report)
        reader.start()
        assert read_done.wait(2), "report blocked behind network refresh"
        assert result[0]["actions"] == initial["actions"]
    finally:
        release.set()
        poller.join(5)
        if reader:
            reader.join(5)
    assert not poller.is_alive()


def test_missing_attention_permissions_are_explicit_diagnostics():
    r = service(
        scope(
            credential={
                "token_env": "TEST_WORK_QUEUE_TOKEN",
                "capabilities": ["issues_read"],
            }
        )
    )
    report = r.poll()
    assert {d["kind"] for d in report["goals"][0]["diagnostics"]} == {
        "permission_unavailable:pulls_read",
        "permission_unavailable:checks_read",
        "permission_unavailable:actions_read",
    }
    assert report["actionable_count"] == 1


def test_restart_keeps_stable_proposal_identity_without_any_side_effect():
    first = service().poll()
    restarted = service().poll()
    assert first["actions"][0]["id"] == restarted["actions"][0]["id"]
    assert not restarted["actions"][0]["execution_allowed"]


def test_enabled_worker_requires_control_credential_identity():
    config = WorkQueueConfig(
        enabled=True, workspace_id="workspace-test", goals=[scope()]
    )
    with pytest.raises(ValueError, match="requires control credentials"):
        start_shadow_reconciler(SimpleNamespace(), config, threading.Event())
    with pytest.raises(ValueError, match="authenticated control principal"):
        ShadowReconciler(config, service_principal="")
