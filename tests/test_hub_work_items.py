"""Hub-owned continuity items share launch and event transactions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import requests
from test_work_queue_execution import service
from test_work_queue_transport import transport

from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector


def _register(service, session_id="session-work", **payload):
    body = {
        "intent": "Keep continuity durable",
        "repo_owner": "example",
        "repo_name": "project",
        "branch": "feature/continuity",
        **payload,
    }
    # Exercise the accepted POST /sessions response path, including its
    # transaction callback, without requiring a real remote harness daemon.
    collector = SimpleNamespace(duckdb_path=service.store.path, work_control=service)
    MetricsCollector._sync_created_harness_session(
        collector,
        "host",
        body,
        json.dumps({"session_id": session_id, "harness": "codex", "status": "running"}),
        work_item_owner="Studio",
    )
    return HarnessRegistry(service.store.path)


def test_launch_registration_is_in_the_session_transaction(service):
    registry = _register(service)
    item = service.store.hub_items()[0]
    assert item["session_id"] == "session-work"
    assert item["title"] == "Keep continuity durable"
    assert item["repo"] == "example/project"
    assert item["stage"] == "launched"
    assert registry.get_session("session-work") is not None


def test_lifecycle_event_is_applied_once_and_shadow_does_not_block_registration(
    service,
):
    service.config = service.config.model_copy(update={"shadow_mode": True})
    registry = _register(service)
    item = service.store.hub_items()[0]
    registry.append_event(
        event_id="work-turn-1",
        session_id="session-work",
        event_type="status",
        payload={"turn_complete": True, "commit_sha": "a" * 40},
    )
    registry.append_event(
        event_id="work-turn-1",
        session_id="session-work",
        event_type="status",
        payload={"turn_complete": True, "commit_sha": "a" * 40},
    )
    result = service.store.hub_item(item["id"])
    lifecycle = [row for row in result["timeline"] if row["source_event_id"]]
    assert result["stage"] == "worker_done"
    assert result["gate"] == "ci_green"
    assert len(lifecycle) == 1
    assert lifecycle[0]["source_event_id"] == "work-turn-1"
    assert lifecycle[0]["kind"] == "worker_done"
    assert lifecycle[0]["detail"] == "a" * 40


def test_work_item_http_read_patch_auth_and_export(transport):
    service, _, url, device, host, _ = transport
    _register(service, session_id="http-work")
    item = service.store.hub_items()[0]
    headers = {"Authorization": "Bearer " + device}

    response = requests.get(
        url + "/work-items", params={"state": "open"}, headers=headers, timeout=5
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == item["id"]
    assert (
        requests.get(
            url + "/work-items", headers={"Authorization": "Bearer " + host}, timeout=5
        ).status_code
        == 403
    )

    response = requests.patch(
        url + "/work-items/" + item["id"],
        json={
            "stage": "review",
            "next": "wait for CI",
            "pr": "https://example/pr/1",
            "acceptance": "tests pass",
        },
        headers=headers,
        timeout=5,
    )
    assert response.status_code == 200
    assert response.json()["stage"] == "review"
    assert response.json()["next_action"] == "wait for CI"

    exported = requests.get(url + "/work-items/export", headers=headers, timeout=5)
    assert exported.status_code == 200
    assert exported.json() == {
        "items": [
            {
                "id": item["id"],
                "stage": "review",
                "pr": "https://example/pr/1",
                "branch": "feature/continuity",
                "sid": "http-work",
                "next": "wait for CI",
                "after": exported.json()["items"][0]["after"],
            }
        ]
    }
