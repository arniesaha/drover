import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from drover.server.continuity_loop import (
    HANDOFF_MARK,
    NUDGE_MARK,
    ContinuityPolicy,
    ShadowCompareService,
    action_identity,
    decide,
)
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings

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
        {"mark": HANDOFF_MARK, "at": (NOW + timedelta(seconds=1)).isoformat()}
    ]
    assert decide(reached, NOW).action == "none"


def test_decide_ports_legacy_nudge_and_stall_when_worker_state_is_absent():
    legacy = facts(worker={}, created=(NOW - timedelta(minutes=61)).isoformat())
    assert decide(legacy, NOW).action == "nudge"
    legacy["bot"] = [
        {"mark": NUDGE_MARK, "at": (NOW - timedelta(minutes=1)).isoformat()}
    ]
    # The real wire marker starts a new legacy 30-minute idle window.
    assert decide(legacy, NOW).action == "none"
    assert decide(legacy, NOW + timedelta(minutes=31)).action == "stall"


def test_decide_nudges_then_stalls_without_provider_writes():
    overdue = facts(
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=46)).isoformat(),
        }
    )
    policy = ContinuityPolicy(explicit_deadlines=True)
    assert decide(overdue, NOW, policy).action == "nudge"
    overdue["bot"] = [{"mark": "nudge", "at": (NOW - timedelta(minutes=1)).isoformat()}]
    assert decide(overdue, NOW, policy).action == "stall"


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
    assert (
        decide(
            idle,
            NOW,
            ContinuityPolicy(stall_minutes=60, explicit_deadlines=True),
        ).action
        == "nudge"
    )


def test_shadow_default_preserves_legacy_window_with_explicit_worker_states():
    working = facts(
        created=(NOW - timedelta(minutes=31)).isoformat(),
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=31)).isoformat(),
            "last_activity": (NOW - timedelta(minutes=31)).isoformat(),
        },
    )
    assert decide(working, NOW).action == "nudge"
    idle = facts(
        created=(NOW - timedelta(minutes=11)).isoformat(),
        worker={
            "state": "idle_undeclared",
            "state_at": (NOW - timedelta(minutes=11)).isoformat(),
            "last_activity": (NOW - timedelta(minutes=11)).isoformat(),
        },
    )
    assert decide(idle, NOW).action == "none"


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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(labels="not-a-list"),
        lambda value: value.update(prs=["not-an-object"]),
        lambda value: value.update(bot=[{"mark": NUDGE_MARK, "at": "not-a-time"}]),
        lambda value: value.update(worker="not-an-object"),
        lambda value: value.update(markers={"done": "not-a-list"}),
        lambda value: value.update(release={"running": "yes"}),
        lambda value: value.update(external_session_activity="not-a-dict"),
        lambda value: value.update(external_session_activity={"a": "not-a-time"}),
        lambda value: value.update(
            external_session_activity={"x" * 200: NOW.isoformat()}
        ),
        lambda value: value.update(
            external_session_activity={f"k{i}": NOW.isoformat() for i in range(33)}
        ),
    ],
)
def test_shadow_compare_rejects_malformed_nested_facts(mutate):
    value = facts()
    mutate(value)
    with pytest.raises(ValueError):
        ShadowCompareService().compare(value, {"action": "none"}, now=NOW)


def test_shadow_compare_mutation_is_thread_safe_and_bounded():
    service = ShadowCompareService(limit=7)

    def compare(number):
        return service.compare(facts(number=number), {"action": "none"}, now=NOW)

    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(compare, range(1000, 1050)))
    assert all(result["recorded"] for result in results)
    assert len(service.mismatches) == 7


def test_shadow_compare_http_returns_400_for_malformed_nested_facts(tmp_path):
    collector = MetricsCollector(
        duckdb_path=tmp_path / "metrics.duckdb",
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="test-token"),
    )
    payload = facts(worker="not-an-object")
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/continuity/shadow-compare",
        data=json.dumps(
            {"facts": payload, "local_decision": {"action": "none"}}
        ).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer test-token",
        },
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        assert raised.value.code == 400
        assert "facts.worker" in json.loads(raised.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_openclaw_session_activity_prevents_false_stall():
    # Worker activity was 45m ago (> 30m idle window) so without external activity it nudges.
    stale_worker = facts(
        created=(NOW - timedelta(minutes=60)).isoformat(),
        markers={"session": "agent:coder:main", "done": []},
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=45)).isoformat(),
            "last_activity": (NOW - timedelta(minutes=45)).isoformat(),
        },
    )
    assert decide(stale_worker, NOW).action == "nudge"

    # With recent OpenClaw session activity (within 30m), decide() returns "none" (no false stall).
    active_worker = facts(
        created=(NOW - timedelta(minutes=60)).isoformat(),
        markers={"session": "agent:coder:main", "done": []},
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=45)).isoformat(),
            "last_activity": (NOW - timedelta(minutes=45)).isoformat(),
        },
        external_session_activity={
            "agent:coder:main": (NOW - timedelta(minutes=10)).isoformat(),
        },
    )
    assert decide(active_worker, NOW).action == "none"

    # External activity older than 30m does not prevent nudge/stall.
    stale_external = facts(
        created=(NOW - timedelta(minutes=60)).isoformat(),
        markers={"session": "agent:coder:main", "done": []},
        worker={
            "state": "working",
            "state_at": (NOW - timedelta(minutes=45)).isoformat(),
            "last_activity": (NOW - timedelta(minutes=45)).isoformat(),
        },
        external_session_activity={
            "agent:coder:main": (NOW - timedelta(minutes=35)).isoformat(),
        },
    )
    assert decide(stale_external, NOW).action == "nudge"


def test_lease_renewal_survives_hub_restart(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from drover.server.continuity_leases import LeaseConflict, OwnerLeaseStore
    from drover.server.postgres_schema import CONTINUITY_LEASES_DDL

    db_path = tmp_path / "control.duckdb"

    # First hub connection: acquire initial lease
    con1 = duckdb.connect(str(db_path))
    for ddl in CONTINUITY_LEASES_DDL[:2]:
        con1.execute(ddl)
    store1 = OwnerLeaseStore(con1, clock=lambda: NOW)
    initial_lease = store1.acquire(
        "drover#821", owner_id="agent:coder:main", lease_seconds=60
    )
    assert initial_lease["owner_epoch"] == 1
    assert initial_lease["owner_id"] == "agent:coder:main"
    con1.close()

    # Hub restart simulation: reconnect with new connection/store instance
    con2 = duckdb.connect(str(db_path))
    store2 = OwnerLeaseStore(con2, clock=lambda: NOW + timedelta(seconds=15))

    # Same owner renews lease with epoch 1 across the restart
    renewed = store2.renew(
        "drover#821", owner_id="agent:coder:main", owner_epoch=1, lease_seconds=60
    )
    assert renewed["owner_epoch"] == 1
    assert renewed["owner_id"] == "agent:coder:main"

    # Competing owner takeover during live lease is rejected
    with pytest.raises(LeaseConflict):
        store2.acquire("drover#821", owner_id="agent:other:main")

    # Mismatched epoch renew is rejected
    with pytest.raises(LeaseConflict):
        store2.renew("drover#821", owner_id="agent:coder:main", owner_epoch=99)

    # After expiry/release, re-acquisition monotonically advances epoch
    store2.release("drover#821", owner_id="agent:coder:main", owner_epoch=1)
    reacquired = store2.acquire(
        "drover#821", owner_id="agent:coder:main", lease_seconds=60
    )
    assert reacquired["owner_epoch"] == 2

    # Old epoch 1 is now fenced
    with pytest.raises(LeaseConflict):
        store2.renew("drover#821", owner_id="agent:coder:main", owner_epoch=1)
    con2.close()
