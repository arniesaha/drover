"""Phase 3 attention projection and resolved decision exit checks."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_work_queue_execution import ACTOR, HUMAN, launch, service, update
from test_work_queue_transport import transport

from drover.server.work_queue.client import control_call
from drover.server.work_queue.service import WorkActor


def attention(service):
    return service.call("list", {"view": "attention"}, HUMAN)


def question(service):
    return service.call(
        "question",
        dict(
            goal_id="goal",
            number=1,
            request_key="review:1",
            category="revision",
            options=["approve", "reject", "open"],
        ),
        ACTOR,
    )["decision_id"]


def provider_attention(service):
    service.providers["goal"].list_attention = lambda: [
        dict(
            source_key="environment:1:1:2",
            kind="environment_approval",
            url="https://github.com/example/project/actions/runs/1",
        ),
        dict(
            source_key="pr:2",
            kind="draft_pr",
            url="https://github.com/example/project/pull/2",
        ),
    ]


def test_four_kinds_have_scoped_identity_age_and_safe_actions(service):
    provider_attention(service)
    row = launch(service)
    service.harness.sessions[row["run_key"]].status = "failed"
    provider = service.providers["goal"]
    provider.data["labels"][-1] = {"name": "status:blocked"}
    provider.data["body"] = "private source text never copied into attention"
    decision_id = question(service)
    result = attention(service)
    assert not result["stale"]
    assert {r["kind"] for r in result["items"]} == {
        "environment_approval",
        "draft_pr",
        "agent_question",
        "blocked",
        "failed_session",
    }
    for entry in result["items"]:
        assert entry["workspace_id"] == "test-workspace"
        assert entry["actor_principal"] == HUMAN.principal
        assert entry["refreshed_at"] == result["refreshed_at"]
        assert entry["url"].startswith("https://github.com/example/project/")
        assert "body" not in entry
    q = next(r for r in result["items"] if r["kind"] == "agent_question")
    assert q["actions"] == ["reject"]
    assert q["decision_id"] == decision_id
    assert q["session_id"] == row["session_id"]
    assert all(
        not r["actions"] for r in result["items"] if r["kind"] != "agent_question"
    )
    assert [r["id"] for r in attention(service)["items"]] == [
        r["id"] for r in result["items"]
    ]


def test_two_devices_and_stale_list_do_not_repeat_resolved_action(transport):
    service, _, url, device, _, _ = transport
    provider_attention(service)
    decision_id = question(service)
    args = dict(goal_id="goal", number=1, decision_id=decision_id, action="reject")
    writes = []
    original = service.providers["goal"].set_labels

    def record(ref, labels):
        writes.append(labels)
        original(ref, labels)

    service.providers["goal"].set_labels = record
    before = control_call(
        url, {"Authorization": "Bearer " + device}, "list", {"view": "attention"}
    )
    assert any(r.get("decision_id") == decision_id for r in before["items"])
    other = WorkActor("credential:second-device", "human", True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda actor: service.call("decide", args, actor), [HUMAN, other])
        )
    assert all(r["resolved"] for r in results)
    assert len(writes) == 1
    assert service.call("decide", args, HUMAN)["resolved"]
    assert len(writes) == 1
    assert not any(
        r.get("decision_id") == decision_id for r in attention(service)["items"]
    )
    assert [e["event_kind"] for e in service.store.events(decision_id)] == [
        "question",
        "intent",
        "outcome",
    ]


def test_uncertain_shadow_and_nonhuman_rows_offer_no_decision(service):
    provider_attention(service)
    decision_id = question(service)
    service.config = service.config.model_copy(update={"shadow_mode": True})
    assert all(not r["actions"] for r in attention(service)["items"])
    service.config = service.config.model_copy(update={"shadow_mode": False})
    assert all(
        not r["actions"]
        for r in service.call("list", {"view": "attention"}, ACTOR)["items"]
    )
    _, _, ref, _ = service.item("goal", 1)
    service.store.event(
        HUMAN.principal, ref, decision_id, "intent", "revision", outcome="reject"
    )
    q = next(r for r in attention(service)["items"] if r["kind"] == "agent_question")
    assert q["uncertain"] and not q["actions"]
    with pytest.raises(RuntimeError, match="uncertain"):
        service.call(
            "decide",
            dict(goal_id="goal", number=1, decision_id=decision_id, action="reject"),
            HUMAN,
        )


def test_attention_bounded_and_cross_workspace_rejected(service):
    provider_attention(service)
    result = service.call("list", {"view": "attention", "limit": 1}, HUMAN)
    assert len(result["items"]) == 1 and result["truncated"]
    with pytest.raises(PermissionError):
        service.call("list", {"view": "attention", "goal_id": "other-workspace"}, HUMAN)


def test_recovered_session_supersedes_historical_failure(service):
    provider_attention(service)
    first = launch(service)
    service.harness.sessions[first["run_key"]].status = "failed"
    assert any(r["kind"] == "failed_session" for r in attention(service)["items"])
    update(service, first, "failed")
    second = launch(service, "recovery")
    assert not any(r["kind"] == "failed_session" for r in attention(service)["items"])
    service.harness.sessions[second["run_key"]].status = "completed"
    assert not any(r["kind"] == "failed_session" for r in attention(service)["items"])


def test_attention_poll_and_freshness_follow_versioned_policy(service, tmp_path):
    import json

    from drover.server.work_queue.config import load_policy

    policy = load_policy().model_dump()
    policy["poll"] = {"interval_seconds": 91, "freshness_seconds": 183}
    policy["labels"]["status_prefix"] = "state:"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    goal = service.config.goals[0].model_copy(update={"policy_path": str(path)})
    service.config = service.config.model_copy(update={"goals": [goal]})
    service.providers["goal"].data["labels"][-1] = {"name": "state:blocked"}
    provider_attention(service)
    result = attention(service)
    assert result["poll_interval_seconds"] == 91
    assert all(r["freshness_seconds"] == 183 for r in result["items"])
    assert any(r["kind"] == "blocked" for r in result["items"])


def test_conflicting_labels_do_not_hide_environment_gate(service):
    provider_attention(service)
    service.providers["goal"].data["labels"].append({"name": "status:blocked"})
    result = attention(service)
    assert any(r["kind"] == "environment_approval" for r in result["items"])
    diagnostic = next(r for r in result["items"] if r["kind"] == "blocked")
    assert diagnostic["category"] == "inspect_work_labels"
    assert not diagnostic["actions"]
