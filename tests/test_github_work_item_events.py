"""GitHub facts enter the existing work-item timeline/event transaction."""

import hashlib
import hmac
import json

import requests
from test_hub_work_items import _register
from test_work_queue_execution import service
from test_work_queue_transport import transport

from drover.server.work_queue.github_events import GitHubWebhookWorker


def _configure(service, monkeypatch):
    monkeypatch.setenv("TEST_GITHUB_WEBHOOK_SECRET", "not-a-real-secret")
    webhook = service.config.github_webhook.model_copy(
        update={"secret_env": "TEST_GITHUB_WEBHOOK_SECRET", "max_body_bytes": 1024}
    )
    config = service.config.model_copy(update={"github_webhook": webhook})
    service.config = config
    service.github_webhooks.config = config.github_webhook


def _headers(
    body, delivery="delivery-1", event="pull_request", secret=b"not-a-real-secret"
):
    return {
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret, body, hashlib.sha256).hexdigest(),
    }


def _item(service):
    _register(service, session_id="github-work")
    item = service.store.hub_items()[0]
    service.store.patch_hub_item(
        item["id"], pr="https://github.com/example/project/pull/7"
    )
    return item


def _post(url, body, **headers):
    return requests.post(
        url + "/integrations/github/webhook", data=body, headers=headers, timeout=5
    )


def _pr(action="synchronize", sha="b" * 40, merged=False):
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "example/project", "default_branch": "main"},
        "pull_request": {
            "merged": merged,
            "head": {"ref": "feature/continuity", "sha": sha},
        },
    }


def test_signature_rejection_dedup_and_body_cap(transport, monkeypatch):
    service, _, url, _, _, _ = transport
    _configure(service, monkeypatch)
    body = json.dumps(_pr()).encode()
    assert _post(url, body).status_code == 401
    assert _post(url, body, **_headers(body, secret=b"wrong")).status_code == 401
    good = _headers(body)
    assert _post(url, body, **good).status_code == 202
    assert _post(url, body, **good).json()["deduped"] is True
    assert (
        _post(url, b"x" * 1025, **_headers(b"x" * 1025, delivery="large")).status_code
        == 413
    )


def test_pr_ci_review_and_default_push_append_event_facts(service, monkeypatch):
    _configure(service, monkeypatch)
    item = _item(service)
    receiver = service.github_webhooks
    worker = GitHubWebhookWorker(service.store, service.config.github_webhook, receiver)

    fixtures = [
        ("pull_request", _pr("synchronize")),
        (
            "pull_request_review",
            {
                "action": "submitted",
                "repository": {"full_name": "example/project"},
                "pull_request": {"number": 7, "head": {"ref": "feature/continuity"}},
                "review": {"state": "approved", "commit_id": "b" * 40},
            },
        ),
        (
            "push",
            {
                "ref": "refs/heads/main",
                "after": "c" * 40,
                "repository": {
                    "full_name": "example/project",
                    "default_branch": "main",
                },
            },
        ),
        ("pull_request", _pr("closed")),
    ]
    for number, (event, payload) in enumerate(fixtures):
        body = json.dumps(payload).encode()
        assert (
            receiver.accept(
                _headers(body, delivery=f"delivery-{number}", event=event), body
            )[0]
            == 202
        )
    assert worker.process_once() == len(fixtures)
    kinds = [
        event["kind"]
        for event in service.store.work_item_events(since=0, limit=30)["events"]
    ]
    for kind in ("pr_synchronized", "review_submitted", "pr_behind", "pr_closed"):
        assert kind in kinds
    assert service.store.hub_item(item["id"])["head_sha"] == "b" * 40


def test_remaining_handled_pr_actions_and_check_run_map_to_facts(service, monkeypatch):
    _configure(service, monkeypatch)
    _item(service)
    receiver = service.github_webhooks
    worker = GitHubWebhookWorker(service.store, service.config.github_webhook, receiver)
    deliveries = [
        ("pull_request", _pr(action))
        for action in ("opened", "ready_for_review", "converted_to_draft", "reopened")
    ]
    deliveries.append(("pull_request", _pr("closed", merged=True)))
    deliveries.append(
        (
            "check_run",
            {
                "action": "completed",
                "repository": {"full_name": "example/project"},
                "check_run": {
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "b" * 40,
                    "head_branch": "feature/continuity",
                    "pull_requests": [{"number": 7}],
                },
            },
        )
    )
    for number, (event, payload) in enumerate(deliveries):
        body = json.dumps(payload).encode()
        receiver.accept(
            _headers(body, delivery=f"remaining-{number}", event=event), body
        )
    worker.process_once()
    kinds = {
        event["kind"]
        for event in service.store.work_item_events(since=0, limit=40)["events"]
    }
    assert {
        "pr_opened",
        "pr_ready_for_review",
        "pr_draft",
        "pr_reopened",
        "pr_merged",
        "ci_completed",
    } <= kinds
    assert (
        "ci_green_exact_head" not in kinds
    )  # a constituent check-run is never a rollup


def test_old_head_cannot_make_current_head_green_and_unknown_pr_creates_nothing(
    service, monkeypatch
):
    _configure(service, monkeypatch)
    item = _item(service)
    receiver = service.github_webhooks
    worker = GitHubWebhookWorker(service.store, service.config.github_webhook, receiver)
    sync = json.dumps(_pr("synchronize", "b" * 40)).encode()
    old = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "example/project"},
            "check_suite": {
                "status": "completed",
                "conclusion": "success",
                "head_sha": "a" * 40,
                "head_branch": "feature/continuity",
                "pull_requests": [{"number": 7}],
            },
        }
    ).encode()
    current = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "example/project"},
            "check_suite": {
                "status": "completed",
                "conclusion": "success",
                "head_sha": "b" * 40,
                "head_branch": "feature/continuity",
                "pull_requests": [{"number": 7}],
            },
        }
    ).encode()
    unknown = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "example/project"},
            "workflow_run": {
                "status": "completed",
                "conclusion": "success",
                "head_sha": "z" * 40,
                "head_branch": "other",
                "pull_requests": [{"number": 99}],
            },
        }
    ).encode()
    for index, (event, body) in enumerate(
        (
            ("pull_request", sync),
            ("check_suite", old),
            ("check_suite", current),
            ("workflow_run", unknown),
        )
    ):
        receiver.accept(_headers(body, delivery=f"head-{index}", event=event), body)
    worker.process_once()
    events = service.store.work_item_events(since=0, limit=30)["events"]
    completed = [event for event in events if event["kind"] == "ci_completed"]
    assert json.loads(completed[0]["detail"])["exact_head"] is False
    assert [event["kind"] for event in events].count("ci_green_exact_head") == 1
    assert service.store.hub_items()[0]["id"] == item["id"]
    assert receiver.health()["unlinked"] >= 1


def _deployment(state, *, run_id=91, environment="ios-testflight-upload"):
    return {
        "repository": {"full_name": "example/project"},
        "deployment": {"ref": "feature/continuity", "workflow_run_id": run_id},
        "deployment_status": {
            "state": state,
            "environment": environment,
            "workflow_run_id": run_id,
            "environment_url": f"https://github.com/example/project/actions/runs/{run_id}",
        },
        "sender": {"login": "release-reviewer"},
    }


def test_environment_approval_waiting_then_approved_resumes_once(service, monkeypatch):
    _configure(service, monkeypatch)
    item = _item(service)
    receiver = service.github_webhooks
    worker = GitHubWebhookWorker(service.store, service.config.github_webhook, receiver)
    for delivery, state in (
        ("approval-wait", "pending"),
        ("approval-repeat", "pending"),
        ("approval-ok", "success"),
    ):
        body = json.dumps(_deployment(state)).encode()
        assert (
            receiver.accept(
                _headers(body, delivery=delivery, event="deployment_status"), body
            )[0]
            == 202
        )
    assert worker.process_once() == 3
    result = service.store.hub_item(item["id"])
    assert result["stage"] == "queued"
    facts = [
        row
        for row in result["timeline"]
        if row["kind"].startswith("environment_approval")
    ]
    assert [row["kind"] for row in facts] == [
        "environment_approval_required",
        "environment_approval_approved",
    ]
    with service.store.connection() as con:
        row = con.execute(
            "SELECT environment, run_url, reviewer, state FROM github_environment_approvals"
        ).fetchone()
    assert row == (
        "ios-testflight-upload",
        "https://github.com/example/project/actions/runs/91",
        "release-reviewer",
        "approved",
    )

    from drover.server.work_queue.rules import WorkItemRuleWorker

    rule_worker = WorkItemRuleWorker(
        service.store, {}, type("Config", (), {"batch_size": 50, "dry_run": False})()
    )
    rule_worker.process_once()
    rule_worker.process_once()  # a replay cannot send another wake
    kinds = [row["kind"] for row in service.store.hub_item(item["id"])["timeline"]]
    assert kinds.count("orchestrator_wake") == 1


def test_environment_approval_rejection_is_terminal_and_idempotent(
    service, monkeypatch
):
    _configure(service, monkeypatch)
    item = _item(service)
    receiver = service.github_webhooks
    worker = GitHubWebhookWorker(service.store, service.config.github_webhook, receiver)
    for delivery in ("reject-wait", "reject", "reject-repeat"):
        state = "pending" if delivery == "reject-wait" else "failure"
        body = json.dumps(_deployment(state, run_id=92)).encode()
        receiver.accept(
            _headers(body, delivery=delivery, event="deployment_status"), body
        )
    worker.process_once()
    result = service.store.hub_item(item["id"])
    assert result["stage"] == "closed"
    assert result["gate"] == "environment_rejected"
    assert [row["kind"] for row in result["timeline"]].count(
        "environment_approval_rejected"
    ) == 1
