from datetime import datetime, timedelta, timezone

from drover.server.continuity_loop import (
    ContinuityPolicy,
    ShadowCompareService,
    action_identity,
    decide,
)

NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)


def facts(**changes):
    value = {
        "number": 780,
        "labels": ["autonomy:through-pr"],
        "created": (NOW - timedelta(minutes=61)).isoformat(),
        "human_activity": [],
        "prs": [],
        "markers": {"session": "session-1", "done": []},
        "bot": [],
        "worker": {
            "state": "working",
            "state_at": (NOW - timedelta(minutes=46)).isoformat(),
        },
    }
    value.update(changes)
    return value


def test_decide_ports_legacy_stage_reach_and_handoff_once():
    reached = facts(
        prs=[
            {
                "pr": 1,
                "state": "OPEN",
                "draft": False,
                "checks": "pass",
                "last": NOW.isoformat(),
            }
        ]
    )
    decision = decide(reached, NOW)
    assert decision.action == "handoff"
    assert decision.stage == "pr"
    reached["bot"] = [
        {"mark": "handoff", "at": (NOW + timedelta(seconds=1)).isoformat()}
    ]
    assert decide(reached, NOW).action == "none"


def test_decide_ports_legacy_nudge_and_stall_when_worker_state_is_absent():
    legacy = facts(worker={}, created=(NOW - timedelta(minutes=61)).isoformat())
    assert decide(legacy, NOW).action == "nudge"
    legacy["bot"] = [{"mark": "nudge", "at": (NOW - timedelta(minutes=1)).isoformat()}]
    assert decide(legacy, NOW).action == "stall"


def test_decide_nudges_then_stalls_without_provider_writes():
    overdue = facts(
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=46)).isoformat(),
        }
    )
    assert decide(overdue, NOW).action == "nudge"
    overdue["bot"] = [{"mark": "nudge", "at": (NOW - timedelta(minutes=1)).isoformat()}]
    assert decide(overdue, NOW).action == "stall"


def test_decide_worker_states_and_approval_coverage():
    approval = facts(
        worker={"state": "needs_approval", "gate": "pr", "state_at": NOW.isoformat()}
    )
    assert decide(approval, NOW).action == "auto_approve"
    approval["worker"]["gate"] = "merge"
    assert decide(approval, NOW).action == "wake"
    blocked = facts(worker={"state": "blocked", "state_at": NOW.isoformat()})
    assert decide(blocked, NOW).action == "wake"


def test_decide_idle_undeclared_uses_short_deadline():
    idle = facts(
        worker={
            "state": "idle_undeclared",
            "state_at": (NOW - timedelta(minutes=11)).isoformat(),
        }
    )
    assert decide(idle, NOW, ContinuityPolicy(stall_minutes=60)).action == "nudge"


def test_shadow_compare_records_bounded_action_mismatches_with_rule_identity():
    service = ShadowCompareService(limit=2)
    compared = service.compare(facts(), {"action": "none"}, now=NOW)
    assert not compared["matches"]
    assert compared["recorded"]
    assert compared["identity"][0] == "continuity:nudge"
    hub = decide(facts(), NOW)
    assert compared["identity"] == action_identity(hub)
    assert service.compare(facts(number=781), {"action": "none"}, now=NOW)["recorded"]
    assert service.compare(facts(number=782), {"action": "none"}, now=NOW)["recorded"]
    assert len(service.mismatches) == 2


def test_shadow_compare_accepts_legacy_action_only_payload():
    service = ShadowCompareService()
    result = service.compare(facts(), {"action": "nudge"}, now=NOW)
    assert result["matches"]
    assert not service.mismatches
