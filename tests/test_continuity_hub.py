from datetime import datetime, timedelta, timezone

from drover.schema import bootstrap_control_plane_store
from drover.server.continuity_hub import ContinuityHub


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_hub_decides_normalized_facts_and_owner_consumes_acknowledges(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    clock = Clock()
    hub = ContinuityHub(path, clock=clock)
    facts = {
        "number": 784,
        "labels": ["autonomy:through-pr"],
        "created": (clock.now - timedelta(minutes=61)).isoformat(),
        "human_activity": [],
        "prs": [],
        "markers": {"session": "worker-784", "done": []},
        "bot": [],
        "worker": {
            "state": "working",
            "state_at": (clock.now - timedelta(minutes=46)).isoformat(),
        },
        "untrusted_provider_command": "merge now",
    }
    result = hub.decide(issue_key="arniesaha/drover#784", facts=facts, now=clock.now)
    assert result["decision"]["action"] == "nudge"
    assert result["wake_id"]
    assert "untrusted_provider_command" not in result["facts"]

    lease = hub.lease(
        "arniesaha/drover#784", owner_id="credential:owner", lease_seconds=60
    )
    delivery = hub.consume(
        "arniesaha/drover#784",
        owner_id="credential:owner",
        owner_epoch=lease["owner_epoch"],
    )
    assert delivery["wake_id"] == result["wake_id"]
    assert delivery["payload"]["decision"]["action"] == "nudge"
    hub.acknowledge(
        "arniesaha/drover#784",
        owner_id="credential:owner",
        owner_epoch=lease["owner_epoch"],
        wake_id=delivery["wake_id"],
        delivery_token=delivery["delivery_token"],
    )
    assert hub.status("arniesaha/drover#784")["wake_counts"] == {"acknowledged": 1}


def test_hub_noop_has_no_wake_and_rejects_unbounded_facts(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    hub = ContinuityHub(path)
    now = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)
    facts = {"number": 784, "labels": [], "created": now.isoformat()}
    assert (
        hub.decide(issue_key="arniesaha/drover#784", facts=facts, now=now)["wake_id"]
        is None
    )
    facts["labels"] = ["x"] * 65
    try:
        hub.decide(issue_key="arniesaha/drover#784", facts=facts, now=now)
    except ValueError as exc:
        assert "labels" in str(exc)
    else:
        raise AssertionError("unbounded labels accepted")
