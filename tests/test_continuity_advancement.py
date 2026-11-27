"""Pilot progress is evidence identity, not a fresh observation or status note."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_hub import ContinuityHub
from drover.server.continuity_loop import action_identity, decide, normalize_facts

NOW = datetime(2026, 11, 27, 7, 8, tzinfo=timezone.utc)
POLICY = {"require_advancement": True, "explicit_deadlines": True}


def facts():
    return {
        "number": 1024,
        "labels": ["autonomy:through-rollout"],
        "created": (NOW - timedelta(hours=2)).isoformat(),
        "advancement": [
            {"ref": "commit:abc123", "at": (NOW - timedelta(hours=1)).isoformat()}
        ],
        "markers": {"owner": "pilot-owner", "session": "original-worker", "done": []},
        "worker": {
            "state": "idle_undeclared",
            "state_at": NOW.isoformat(),
            "last_activity": NOW.isoformat(),
        },
        "recovery": {
            "scope": "bounded pilot1024; no producer permission change",
            "checkpoint": "commit:abc123",
            "result": "retained proof complete; live acceptance pending",
            "next_action": "continue authorized structural correction",
        },
    }


def test_status_checks_and_heartbeat_do_not_postpone_progress_deadline():
    value = facts()
    before = decide(value, NOW, POLICY)
    assert before.action == "nudge"
    value["human_activity"] = [NOW.isoformat()]
    value["external_session_activity"] = {"pilot-owner": NOW.isoformat()}
    value["prs"] = [
        {
            "pr": 1032,
            "state": "OPEN",
            "draft": True,
            "checks": "pending",
            "last": NOW.isoformat(),
        }
    ]
    assert decide(value, NOW, POLICY) == before
    assert decide(value, NOW).action == "none"  # legacy is unchanged
    value["prs"][0]["checks"] = "pass"
    assert action_identity(decide(value, NOW, POLICY)) == action_identity(before)


def test_identical_snapshot_recovery_and_real_advancement():
    value = facts()
    first = decide(value, NOW, POLICY)
    assert decide(deepcopy(value), NOW + timedelta(minutes=1), POLICY) == first
    value["bot"] = [{"mark": "nudge", "at": NOW.isoformat()}]
    assert decide(value, NOW + timedelta(minutes=31), POLICY).action == "stall"
    value["advancement"].append(
        {"ref": "commit:def456", "at": (NOW + timedelta(minutes=30)).isoformat()}
    )
    assert decide(value, NOW + timedelta(minutes=31), POLICY).action == "none"


def test_duplicate_receipt_cannot_refresh_clock_or_identity():
    value = facts()
    first = decide(value, NOW, POLICY)
    value["advancement"].append({"ref": "commit:abc123", "at": NOW.isoformat()})
    assert decide(value, NOW, POLICY) == first
    normalized = normalize_facts(value)
    assert len(normalized["advancement"]) == 1
    assert normalize_facts(normalized) == normalized


def test_explicit_human_hold_never_bypassed_even_when_unprepared():
    value = facts()
    value["labels"].append("blocked:human")
    held = decide(value, NOW, POLICY)
    assert (held.action, held.reason) == ("none", "blocked_human_unprepared")
    value["human_handoff"] = {"artifact": "reviewable:packet1032"}
    assert decide(value, NOW, POLICY).reason == "blocked_human_unprepared"
    value["human_handoff"].update(
        {
            "tested_capability": "read credential cannot publish checks",
            "requested_action": "independently review narrow check-write job",
            "resume_when": "reviewed producer plus eligible independent human",
        }
    )
    held = decide(value, NOW, POLICY)
    assert (held.action, held.reason) == ("none", "blocked_human")
    # A prepared packet is diagnostic, never authority or permission to proceed.
    assert decide(value, NOW).reason == "blocked_human"


@pytest.mark.parametrize(
    "change",
    [
        {"advancement": [{"ref": "", "at": NOW.isoformat()}]},
        {"advancement": [{"ref": "sha", "at": "not-time"}]},
        {"advancement": [{}] * 101},
        {"human_handoff": {"approved": True}},
        {"human_handoff": {"artifact": " "}},
        {"recovery": {"scope": "x" * 2049}},
    ],
)
def test_malformed_fact_packets_rejected(change):
    value = facts()
    value.update(change)
    with pytest.raises(ValueError):
        decide(value, NOW, POLICY)


def test_policy_flag_requires_boolean():
    with pytest.raises(ValueError, match="boolean"):
        decide(facts(), NOW, {"require_advancement": "true"})


def test_real_outbox_preserves_checkpoint_on_retry_without_duplicate_effect(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    hub = ContinuityHub(path, clock=lambda: NOW)
    value = facts()
    original = deepcopy(value)
    key = "pilot/repo#1024"
    first = hub.decide(issue_key=key, facts=value, now=NOW, policy=POLICY)
    value["human_activity"] = [NOW.isoformat()]
    repeated = hub.decide(
        issue_key=key, facts=value, now=NOW + timedelta(minutes=1), policy=POLICY
    )
    assert first["wake_id"] == repeated["wake_id"]
    lease = hub.lease(key, owner_id="credential:pilot-owner", lease_seconds=60)
    args = {"owner_id": "credential:pilot-owner", "owner_epoch": lease["owner_epoch"]}
    delivery = hub.consume(key, **args)
    assert delivery["payload"]["facts"]["recovery"] == original["recovery"]
    assert delivery["payload"]["facts"]["markers"]["session"] == "original-worker"
    assert delivery["payload"]["decision"]["stage"] == "rollout"
    hub.acknowledge(
        key,
        **args,
        wake_id=delivery["wake_id"],
        delivery_token=delivery["delivery_token"],
    )
    after_ack = hub.decide(
        issue_key=key, facts=value, now=NOW + timedelta(minutes=2), policy=POLICY
    )
    assert after_ack["wake_id"] == first["wake_id"]
    assert hub.consume(key, **args) is None
    assert hub.status(key)["wake_counts"] == {"acknowledged": 1}


def test_future_receipt_cannot_postpone_recovery_or_mint_another_wake():
    value = facts()
    before = decide(value, NOW, POLICY)
    value["advancement"].append(
        {"ref": "commit:future", "at": (NOW + timedelta(days=365)).isoformat()}
    )
    assert decide(value, NOW, POLICY) == before
    value["advancement"] = value["advancement"][-1:]
    assert decide(value, NOW, POLICY).action == "nudge"
    value["advancement"] = [
        {"ref": "commit:recent", "at": (NOW - timedelta(minutes=1)).isoformat()}
    ]
    assert decide(value, NOW, POLICY).action == "none"


def test_retry_cannot_replace_original_checkpoint_before_consume(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    hub = ContinuityHub(path, clock=lambda: NOW)
    value = facts()
    original = deepcopy(value["recovery"])
    key = "pilot/repo#1024"
    first = hub.decide(issue_key=key, facts=value, now=NOW, policy=POLICY)
    value["recovery"]["checkpoint"] = "unsubstantiated changed checkpoint"
    value["recovery"]["next_action"] = "different action without new advancement"
    repeated = hub.decide(issue_key=key, facts=value, now=NOW, policy=POLICY)
    assert repeated["wake_id"] == first["wake_id"]
    lease = hub.lease(key, owner_id="credential:pilot-owner", lease_seconds=60)
    args = {"owner_id": "credential:pilot-owner", "owner_epoch": lease["owner_epoch"]}
    delivery = hub.consume(key, **args)
    assert delivery["payload"]["facts"]["recovery"] == original
    assert hub.status(key)["wake_counts"] == {"delivered": 1}
