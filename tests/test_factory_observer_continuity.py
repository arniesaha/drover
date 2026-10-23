"""Foreground contracts for durable, observer-only owner continuation (#497)."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.db import control_plane_connection
from drover.server.harness.continuity import (
    MAX_COMPLETION_PROJECTION_BATCH,
    ContinuityConflict,
    FactoryObserverContinuity,
    continuity_request,
)
from drover.server.harness.registry import HarnessRegistry

RUN = "run_FACTORY497"
SESSION = "observer-497"


class Clock:
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def bridge(tmp_path):
    path = tmp_path / "drover.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id=SESSION,
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{RUN}@4",
    )
    clock = Clock()
    store = FactoryObserverContinuity(path, clock=clock)
    store.initialize(
        session_id=SESSION, objective="Fix #497", checkpoint="Implementation pending"
    )
    return store, clock, registry


def report(store, kind="ci_green", *, sequence=1, source_event_id="ci-attempt-1"):
    return store.report(
        RUN,
        source="openclaw",
        source_event_id=source_event_id,
        subject="worker-1" if kind.startswith("worker_") else "ci-commit-a",
        sequence=sequence,
        kind=kind,
        summary="Report only",
    )


def handoff(store, owner_id="openclaw-owner"):
    return store.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Implementation pending",
        authorized_owner_id=owner_id,
    )


def owner(store, owner_id="openclaw-owner", **kwargs):
    handoff(store, owner_id)
    lease = store.lease(RUN, owner_id=owner_id, **kwargs)
    return {"owner_id": owner_id, "owner_epoch": lease["owner_epoch"]}


def test_restart_persists_checkpoint_inbox_lease_ack_and_action(bridge):
    store, clock, _ = bridge
    fence = owner(store)
    first = report(store, "ci_red")
    action = store.consume(RUN, **fence)
    # Read/recover in a different interpreter, after all DuckDB windows closed.
    script = """
import json, sys
from datetime import datetime
from drover.server.harness.continuity import FactoryObserverContinuity
s = FactoryObserverContinuity(sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[2]))
print(json.dumps(s.status(sys.argv[3])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), clock.now.isoformat(), RUN],
        check=True,
        capture_output=True,
        text=True,
    )
    restored = json.loads(result.stdout)
    assert restored["objective"] == "Fix #497"
    assert restored["checkpoint"] == "Implementation pending"
    assert restored["owner"]["epoch"] == fence["owner_epoch"]
    assert restored["next_action"] == action
    assert restored["inbox_counts"] == {"delivered": 1}
    store.acknowledge(
        RUN, **fence, event_id=first, checkpoint="Corrective commit recorded"
    )
    report(store, sequence=2, source_event_id="ci-attempt-2")
    restarted = FactoryObserverContinuity(store.path, clock=clock)
    assert restarted.status(RUN)["checkpoint"] == "Corrective commit recorded"
    assert restarted.status(RUN)["inbox_counts"] == {"acknowledged": 1, "pending": 1}
    assert restarted.status(RUN)["next_action"] is None
    # Initialization and a delayed ack retry cannot overwrite newer progress.
    restarted.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Old checkpoint",
        authorized_owner_id="openclaw-owner",
    )
    restarted.acknowledge(RUN, **fence, event_id=first, checkpoint="Old checkpoint")
    assert restarted.status(RUN)["checkpoint"] == "Corrective commit recorded"


def test_duplicate_report_dedupe_and_divergent_identity_rejection(bridge):
    store, _, _ = bridge
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: report(store), range(2)))
    assert ids[0] == ids[1]
    assert store.status(RUN)["inbox_counts"] == {"pending": 1}
    with pytest.raises(ContinuityConflict, match="divergent"):
        report(store, "ci_red")
    fence = owner(store)
    store.consume(RUN, **fence)
    store.acknowledge(RUN, **fence, event_id=ids[0], checkpoint="Reviewed CI")
    assert report(store) == ids[0]
    assert store.consume(RUN, **fence) is None


def test_competing_owner_claims_have_one_winner(bridge):
    store, _, _ = bridge

    def claim(name):
        try:
            return owner(store, name)
        except ContinuityConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["owner-a", "owner-b"]))
    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    assert store.status(RUN)["owner"]["id"] == winners[0]["owner_id"]


def test_control_store_bootstrap_and_legacy_inventory_preserve_continuity(bridge):
    from drover.server.control_migration import (
        _source_table_rows,
        _validate_source_inventory,
    )

    store, _, _ = bridge
    fence = owner(store)
    report(store)
    action = store.consume(RUN, **fence)
    bootstrap_control_plane_store(store.path)
    assert store.status(RUN)["next_action"] == action
    with control_plane_connection(store.path) as con:
        rows = _source_table_rows(con, ZoneInfo("UTC"))
        assert rows["factory_observer_runs"][0]["next_action_json"]
        assert rows["factory_observer_inbox"][0]["attempts"] == 1
        _validate_source_inventory(
            con,
            source_snapshot=con,
            credential_rows={"control_server_identity": [], "control_credentials": []},
        )
        # A supplied partial schema is refused rather than losing durable data.
        con.execute("ALTER TABLE factory_observer_runs DROP COLUMN checkpoint")
        with pytest.raises(ValueError, match="checkpoint"):
            _validate_source_inventory(
                con,
                source_snapshot=con,
                credential_rows={
                    "control_server_identity": [],
                    "control_credentials": [],
                },
            )
        # Snapshots from before #497 are still accepted with empty continuity.
        con.execute("DROP TABLE factory_observer_runs")
        with pytest.raises(ValueError, match="both continuity"):
            _source_table_rows(con, ZoneInfo("UTC"))
        with pytest.raises(ValueError, match="both continuity"):
            _validate_source_inventory(
                con,
                source_snapshot=con,
                credential_rows={
                    "control_server_identity": [],
                    "control_credentials": [],
                },
            )
        con.execute("DROP TABLE factory_observer_inbox")
        rows = _source_table_rows(con, ZoneInfo("UTC"))
        assert rows["factory_observer_runs"] == rows["factory_observer_inbox"] == []
        _validate_source_inventory(
            con,
            source_snapshot=con,
            credential_rows={"control_server_identity": [], "control_credentials": []},
        )


def test_no_owner_keeps_event_pending_and_projects_recovery(bridge):
    store, _, _ = bridge
    report(store)
    projection = store.status(RUN)
    assert projection["recovery"] == "claim_owner"
    assert not projection["owner_wake"]
    assert projection["inbox_counts"] == {"pending": 1}
    with pytest.raises(ContinuityConflict, match="handoff"):
        store.consume(RUN, owner_id="absent", owner_epoch=0)


def test_unbound_run_denies_owner_mutations_until_explicit_handoff(bridge):
    store, _, _ = bridge
    event_id = report(store, "worker_brief_completed")
    for mutate in (
        lambda: store.lease(RUN, owner_id="unrelated-owner"),
        lambda: store.consume(RUN, owner_id="unrelated-owner", owner_epoch=0),
        lambda: store.acknowledge(
            RUN,
            owner_id="unrelated-owner",
            owner_epoch=0,
            event_id=event_id,
            checkpoint="Unauthorized acknowledgement",
        ),
    ):
        with pytest.raises(ContinuityConflict, match="handoff"):
            mutate()
    handoff(store, "authorized-owner")
    fence = owner(store, "authorized-owner")
    assert store.consume(RUN, **fence)["type"] == "review_worker_result"


def test_sequenced_drover_completion_is_durably_linked_to_observer_owner(bridge):
    """The worker event and owner action commit through the existing run/session link."""
    store, _, registry = bridge
    store.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Implementation pending",
        authorized_owner_id="authorized-owner",
    )
    fence = owner(store, owner_id="authorized-owner")

    registry.append_event(
        event_id="drover-worker-completion-1",
        session_id=SESSION,
        event_type="status",
        payload={"turn_complete": True},
        seq=7,
    )

    pending = store.status(RUN)
    [event] = pending["events"]
    assert event["event_id"] == store.report(
        RUN,
        source="drover",
        source_event_id="drover-worker-completion-1",
        subject=SESSION,
        sequence=7,
        kind="worker_brief_completed",
        summary="Drover harness session reported a completed turn.",
    )
    assert event["report"]["source"] == "drover"
    assert event["report"]["subject"] == SESSION
    action = store.consume(RUN, **fence)
    assert action["type"] == "review_worker_result"
    assert action["authorized"]
    store.acknowledge(
        RUN,
        **fence,
        event_id=event["event_id"],
        checkpoint="Authorized owner reviewed Drover completion",
    )

    # Redelivery of the source event remains the same harness and continuity
    # fact; it cannot manufacture another continuation or alter the checkpoint.
    registry.append_event(
        event_id="drover-worker-completion-1",
        session_id=SESSION,
        event_type="status",
        payload={"turn_complete": True},
        seq=7,
    )
    assert store.status(RUN)["inbox_counts"] == {"acknowledged": 1}
    assert (
        store.status(RUN)["checkpoint"] == "Authorized owner reviewed Drover completion"
    )


def test_harness_events_http_nested_completion_projects_and_rejects_malformed(
    bridge, tmp_path
):
    """Exercise the actual EventPusher envelope, not a flattened registry call."""
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings
    from drover.server.web.credentials import CredentialStore

    store, _, _ = bridge
    handoff(store, "http-event-owner")
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, token = credentials.issue(
        scope="host", label="HTTP event host", host_id="studio"
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=store.path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(
            enabled=True,
            api_token="",
            credentials=credentials,
            legacy_token_enabled=False,
        ),
    )
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/harness/events"

    def post(events):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"events": events}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    try:
        status, body = post(
            [
                {
                    "event_id": "http-nested-completion",
                    "session_id": SESSION,
                    "seq": 8,
                    "type": "status",
                    "role": "system",
                    "text": "turn complete",
                    "payload": {"turn_complete": True},
                    "turn_id": "turn-http-8",
                    "ts": "2026-10-03T00:00:08+00:00",
                },
                {
                    "event_id": "http-nested-malformed-completion",
                    "session_id": SESSION,
                    "type": "status",
                    "role": "system",
                    "text": "turn complete without a sequence",
                    "payload": {"turn_complete": True},
                    "turn_id": "turn-http-malformed",
                    "ts": "2026-10-03T00:00:09+00:00",
                },
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200 and body == {"ingested": 2}
    projection = store.status(RUN)
    assert [event["report"]["source_event_id"] for event in projection["events"]] == [
        "http-nested-completion"
    ]
    assert projection["completion_receipts"] == {
        "counts": {"projected": 1, "rejected": 1},
        "errors": [
            {
                "harness_event_id": "http-nested-malformed-completion",
                "state": "rejected",
                "reason": "completion_requires_nonnegative_sequence",
            }
        ],
    }


def test_unsequenced_or_uninitialized_harness_completion_does_not_claim_owner(bridge):
    store, _, registry = bridge
    registry.append_event(
        event_id="unsequenced-completion",
        session_id=SESSION,
        event_type="status",
        payload={"turn_complete": True},
    )
    assert store.status(RUN)["inbox_counts"] == {}

    registry.create_session(
        session_id="ordinary-session",
        host_id="studio",
        harness="codex",
        command="codex",
    )
    registry.append_event(
        event_id="ordinary-completion",
        session_id="ordinary-session",
        event_type="status",
        payload={"turn_complete": True},
        seq=1,
    )
    assert store.status(RUN)["inbox_counts"] == {}


def test_pending_completion_recovers_after_authorized_owner_handoff(bridge):
    store, _, registry = bridge
    registry.append_event(
        event_id="completion-before-owner-handoff",
        session_id=SESSION,
        event_type="status",
        payload={"turn_complete": True},
        seq=1,
    )
    pending = store.status(RUN)
    assert pending["owner_handoff"] == {
        "authorized_owner_id": None,
        "state": "authorization_required",
    }
    assert pending["completion_receipts"] == {
        "counts": {"pending_authorization": 1},
        "errors": [
            {
                "harness_event_id": "completion-before-owner-handoff",
                "state": "pending_authorization",
                "reason": "authorized_owner_handoff_required",
            }
        ],
    }
    store.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Implementation pending",
        authorized_owner_id="recovery-owner",
    )
    recovered = store.status(RUN)
    assert recovered["owner_handoff"] == {
        "authorized_owner_id": "recovery-owner",
        "state": "owner_lease_required",
    }
    assert recovered["completion_receipts"] == {
        "counts": {"projected": 1},
        "errors": [],
    }
    fence = owner(store, "recovery-owner")
    action = store.consume(RUN, **fence)
    assert action["type"] == "review_worker_result"
    assert store.status(RUN)["owner_handoff"]["state"] == "acknowledgement_required"
    store.acknowledge(
        RUN,
        **fence,
        event_id=action["event_id"],
        checkpoint="Recovered owner acknowledged retained completion",
    )
    assert store.status(RUN)["owner_handoff"]["state"] == "acknowledged"


def test_structured_batch_completion_projection_is_ordered_idempotent_and_records_failure(
    bridge,
):
    store, clock, registry = bridge
    store.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Implementation pending",
        authorized_owner_id="batch-owner",
    )
    records = [
        {
            "event_id": "batch-completion-2",
            "session_id": SESSION,
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 2,
            "created_at": clock.now + timedelta(seconds=2),
        },
        {
            "event_id": "batch-completion-1",
            "session_id": SESSION,
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 1,
            "created_at": clock.now + timedelta(seconds=1),
        },
        {
            "event_id": "batch-malformed-completion",
            "session_id": SESSION,
            "event_type": "status",
            "payload": {"turn_complete": True},
            "created_at": clock.now + timedelta(seconds=3),
        },
        # A retry in the same batch is one source record, not a second action.
        {
            "event_id": "batch-completion-1",
            "session_id": SESSION,
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 1,
            "created_at": clock.now + timedelta(seconds=1),
        },
    ]
    # This is the remote/structured production ingestion transaction, not the
    # local one-event convenience API.
    assert registry.ingest_structured_events(records) == 3

    pending = store.status(RUN)
    assert [entry["report"]["source_event_id"] for entry in pending["events"]] == [
        "batch-completion-1",
        "batch-completion-2",
    ]
    assert pending["owner_handoff"] == {
        "authorized_owner_id": "batch-owner",
        # A valid action is waiting too, but the mixed batch is not falsely
        # presented as wholly ready while one completion was rejected.
        "state": "failed",
    }
    assert pending["completion_receipts"] == {
        "counts": {"projected": 2, "rejected": 1},
        "errors": [
            {
                "harness_event_id": "batch-malformed-completion",
                "state": "rejected",
                "reason": "completion_requires_nonnegative_sequence",
            }
        ],
    }
    assert registry.ingest_structured_events(records) == 0
    assert store.status(RUN)["completion_receipts"] == pending["completion_receipts"]


def test_structured_completion_projection_chunks_beyond_its_bound(bridge):
    store, clock, registry = bridge
    store.initialize(
        session_id=SESSION,
        objective="Fix #497",
        checkpoint="Implementation pending",
        authorized_owner_id="chunked-owner",
    )
    records = [
        {
            "event_id": f"chunked-completion-{sequence}",
            "session_id": SESSION,
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": sequence,
            "created_at": clock.now + timedelta(seconds=sequence),
        }
        for sequence in range(MAX_COMPLETION_PROJECTION_BATCH + 1)
    ]
    # Ingestion owns one transaction but calls the bounded projector twice.
    assert registry.ingest_structured_events(records) == len(records)
    assert store.status(RUN)["completion_receipts"]["counts"] == {
        "projected": len(records)
    }
    assert registry.ingest_structured_events(records) == 0


def test_expiry_recovers_same_action_and_fences_stale_owner(bridge):
    store, clock, _ = bridge
    stale = owner(store, lease_seconds=5)
    event_id = report(store)
    action = store.consume(RUN, **stale)
    with pytest.raises(ContinuityConflict):
        owner(store, "second-owner")
    clock.advance(5)  # Exact expiry is expired, not a grace interval.
    assert store.status(RUN)["recovery"] == "reclaim_expired_owner"
    with pytest.raises(ContinuityConflict):
        store.acknowledge(RUN, **stale, event_id=event_id, checkpoint="stale")
    current = owner(store)  # Same owner name still requires a new epoch.
    assert current["owner_epoch"] == stale["owner_epoch"] + 1
    assert store.consume(RUN, **current) == action
    with pytest.raises(ContinuityConflict):
        store.consume(RUN, **stale)
    renewed = store.lease(RUN, **current, lease_seconds=30)
    assert renewed["owner_epoch"] == current["owner_epoch"]
    store.acknowledge(
        RUN, **current, event_id=event_id, checkpoint="Recovered owner continued"
    )


@pytest.mark.parametrize(
    "kind,action_type", [("ci_red", "correct_ci"), ("ci_green", "review_ci")]
)
def test_ci_result_wakes_owner_and_has_scoped_next_action(bridge, kind, action_type):
    store, _, registry = bridge
    before = registry.get_session(SESSION)
    fence = owner(store)
    report(store, kind)
    assert store.status(RUN)["owner_wake"]
    action = store.consume(RUN, **fence)
    assert action["type"] == action_type
    assert action["authority_scope"] == "implementation"
    assert action["authorized"]
    assert store.status(RUN)["scope_limit"] == "commit_only"
    assert registry.get_session(SESSION) == before


def test_worker_awaiting_input_is_distinct_and_completion_never_releases(bridge):
    store, _, registry = bridge
    before = registry.get_session(SESSION)
    fence = owner(store)
    event_id = report(store, "worker_awaiting_input", source_event_id="worker-input-1")
    assert store.consume(RUN, **fence)["type"] == "request_input"
    assert store.status(RUN)["worker_state"] == "awaiting_input"
    store.acknowledge(
        RUN, **fence, event_id=event_id, checkpoint="Input needed from owner"
    )
    event_id = report(
        store,
        "worker_brief_completed",
        sequence=2,
        source_event_id="worker-completed-2",
    )
    assert store.consume(RUN, **fence)["type"] == "review_worker_result"
    status = store.status(RUN)
    assert status["worker_state"] == "brief_completed"
    assert status["owner"]["live"]
    assert not status["terminal_release"]
    store.acknowledge(
        RUN, **fence, event_id=event_id, checkpoint="Owner reviewed completion"
    )
    assert not store.status(RUN)["terminal_release"]
    assert registry.get_session(SESSION) == before


def test_late_worker_report_cannot_regress_completed_projection(bridge):
    store, _, _ = bridge
    fence = owner(store)
    event_id = report(store, "worker_brief_completed", sequence=3)
    store.consume(RUN, **fence)
    store.acknowledge(RUN, **fence, event_id=event_id, checkpoint="Reviewed completion")
    report(store, "worker_awaiting_input", sequence=2, source_event_id="late-input")
    assert store.consume(RUN, **fence)["type"] == "review_late_report"
    assert store.status(RUN)["worker_state"] == "brief_completed"


@pytest.mark.parametrize(
    "scope,action_type,authorized",
    [
        ("integration", "review_ci", True),
        ("deployment", "request_explicit_approval", False),
    ],
)
def test_scope_is_immutable_and_deployment_approval_is_blocked(
    bridge, scope, action_type, authorized
):
    store, _, registry = bridge
    registry.create_session(
        session_id="scoped-observer",
        host_id="studio",
        harness="codex",
        command="codex",
        source_session_id="factory/run_SCOPED497@4",
        handoff_mode="factory_observer",
    )
    run = store.initialize(
        session_id="scoped-observer",
        objective="Review release",
        checkpoint="Waiting",
        authority_scope=scope,
        authorized_owner_id="owner",
    )
    with pytest.raises(ContinuityConflict, match="immutable"):
        store.initialize(
            session_id="scoped-observer",
            objective="Review release",
            checkpoint="Waiting",
            authority_scope="implementation",
        )
    lease = store.lease(run, owner_id="owner")
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    event_id = store.report(
        run,
        source="openclaw",
        source_event_id="ci-green",
        subject="ci-a",
        sequence=1,
        kind="ci_green",
        summary="Green",
    )
    action = store.consume(run, **fence)
    assert action["type"] == action_type and action["authorized"] == authorized
    if not authorized:
        assert store.status(run)["recovery"] == "approval_required"
        assert not store.status(run)["owner_wake"]
        with pytest.raises(ContinuityConflict, match="approval"):
            store.acknowledge(run, **fence, event_id=event_id, checkpoint="Deployed")
        assert store.status(run)["checkpoint"] == "Waiting"


def test_delivery_backoff_and_retry_budget_survive_owner_recovery(bridge):
    store, clock, _ = bridge
    fence = owner(store, lease_seconds=1)
    event_id = report(store)
    action = store.consume(RUN, **fence)
    assert store.consume(RUN, **fence) is None
    clock.advance(5)
    fence = owner(store)
    assert store.consume(RUN, **fence) == action
    clock.advance(10)
    assert store.consume(RUN, **fence) == action
    clock.advance(20)
    assert store.consume(RUN, **fence) is None
    status = store.status(RUN)
    assert status["recovery"] == "manual_attention"
    assert status["events"][0]["attempts"] == 3
    assert status["events"][0]["state"] == "exhausted"
    assert status["next_action"] == action
    # The owner can inspect/reconcile a delivery that exceeded its budget.
    store.acknowledge(RUN, **fence, event_id=event_id, checkpoint="Manually reconciled")
    assert store.status(RUN)["inbox_counts"] == {"acknowledged": 1}


def test_projection_is_bounded_and_rejects_control_vocabulary(bridge):
    store, _, registry = bridge
    for i in range(3):
        report(store, sequence=i, source_event_id=f"ci-{i}")
    assert len(store.status(RUN, limit=2)["events"]) == 2
    assert store.status(RUN, limit=2)["has_more"]
    for limit in (0, 51, True):
        with pytest.raises(ValueError):
            store.status(RUN, limit=limit)
    for seconds in (0, 301, True):
        with pytest.raises(ValueError):
            owner(store, lease_seconds=seconds)
    with pytest.raises(ValueError, match="unsupported"):
        continuity_request(store, {"operation": "deploy", "run_id": RUN})
    with pytest.raises(ValueError, match="unsupported"):
        continuity_request(
            store,
            {
                "operation": "consume",
                "run_id": RUN,
                "owner_id": "owner",
                "owner_epoch": 0,
                "approve": True,
            },
        )
    registry.create_session(
        session_id="ordinary", host_id="studio", harness="codex", command="codex"
    )
    with pytest.raises(ValueError, match="observer session"):
        store.initialize(session_id="ordinary", objective="No", checkpoint="No")


def test_http_boundary_authenticated_bounded_and_report_only(bridge, tmp_path):
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings

    store, _, _ = bridge
    collector = MetricsCollector(
        duckdb_path=store.path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="test-boundary"),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}/harness/factory-observer/continuity"

    def request(body=None, query="", token="test-boundary"):
        req = urllib.request.Request(
            base + query,
            data=json.dumps(body).encode() if body is not None else None,
            headers=(
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                if token
                else {}
            ),
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert request(query=f"?run_id={RUN}", token=None)[0] == 401
        assert (
            request(
                {"operation": "lease", "run_id": RUN, "owner_id": "owner"}, token=None
            )[0]
            == 401
        )
        status, body = request(
            {
                "operation": "lease",
                "run_id": RUN,
                "owner_id": "legacy:operator",
            }
        )
        assert status == 409 and "handoff" in body["error"]
        status, issued = request(
            {
                "operation": "issue_handoff",
                "session_id": SESSION,
                "objective": "Fix #497",
                "checkpoint": "Implementation pending",
                "authorized_owner_id": "legacy:operator",
            }
        )
        assert status == 200
        status, payload = request(
            {
                "operation": "initialize",
                "session_id": SESSION,
                "objective": "Fix #497",
                "checkpoint": "Implementation pending",
                "handoff_token": issued["handoff_token"],
            }
        )
        assert status == 200
        assert payload["continuity"]["owner_handoff"] == {
            "authorized_owner_id": "legacy:operator",
            "state": "awaiting_completion",
        }
        status, payload = request(
            {
                "operation": "report",
                "run_id": RUN,
                "source": "openclaw",
                "source_event_id": "ci-green",
                "subject": "ci-a",
                "sequence": 1,
                "kind": "ci_green",
                "summary": "Report only",
            }
        )
        assert status == 200 and payload["continuity"]["recovery"] == "claim_owner"
        assert request(query=f"?run_id={RUN}&limit=51")[0] == 400
        assert request(query=f"?run_id={RUN}&limit=1")[1]["continuity"][
            "inbox_counts"
        ] == {"pending": 1}
        status, payload = request(
            {
                "operation": "lease",
                "run_id": RUN,
                "owner_id": "legacy:operator",
            }
        )
        assert status == 200
        epoch = payload["continuity"]["owner"]["epoch"]
        status, payload = request(
            {
                "operation": "consume",
                "run_id": RUN,
                "owner_id": "legacy:operator",
                "owner_epoch": epoch,
            }
        )
        assert status == 200 and payload["delivery"]["type"] == "review_ci"
        assert (
            request(
                {
                    "operation": "acknowledge",
                    "run_id": RUN,
                    "owner_id": "legacy:operator",
                    "owner_epoch": epoch,
                    "event_id": [],
                    "checkpoint": "Malformed identity",
                }
            )[0]
            == 400
        )
        assert request({"operation": "deploy", "run_id": RUN})[0] == 400
        assert (
            request(
                {
                    "operation": "report",
                    "run_id": RUN,
                    "source": "openclaw",
                    "source_event_id": "ci-green",
                    "subject": "ci-a",
                    "sequence": 1,
                    "kind": "ci_red",
                    "summary": "Changed identity",
                }
            )[0]
            == 409
        )
        assert (
            request(
                {
                    "operation": "consume",
                    "run_id": RUN,
                    "owner_id": "legacy:operator",
                    "owner_epoch": epoch + 1,
                }
            )[0]
            == 409
        )
        assert request(query="?run_id=run_MISSING")[0] == 404
        assert request({"operation": "report", "summary": "x" * 17000})[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def test_http_owner_identity_is_bound_to_credential_and_acknowledgement(
    bridge, tmp_path
):
    """Only the credential named in the durable handoff may mutate this run."""
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings
    from drover.server.web.credentials import CredentialStore

    store, _, _ = bridge
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, host_token = credentials.issue(scope="host", label="studio", host_id="studio")
    owner_credential, owner_token = credentials.issue(scope="device", label="owner")
    other_credential, other_token = credentials.issue(scope="device", label="other")
    owner_principal = "credential:" + owner_credential.id
    other_principal = "credential:" + other_credential.id
    collector = MetricsCollector(
        duckdb_path=store.path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(
            enabled=True,
            api_token="",
            credentials=credentials,
            legacy_token_enabled=False,
        ),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}/harness/factory-observer/continuity"

    def request(body, token):
        req = urllib.request.Request(
            base,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        status, issued = request(
            {
                "operation": "issue_handoff",
                "session_id": SESSION,
                "objective": "Fix #497",
                "checkpoint": "Implementation pending",
                "authorized_owner_id": owner_principal,
            },
            host_token,
        )
        assert status == 200
        status, payload = request(
            {
                "operation": "initialize",
                "session_id": SESSION,
                "objective": "Fix #497",
                "checkpoint": "Implementation pending",
                "handoff_token": issued["handoff_token"],
            },
            owner_token,
        )
        assert status == 200
        assert payload["continuity"]["owner_handoff"] == {
            "authorized_owner_id": owner_principal,
            "state": "awaiting_completion",
        }
        # Supplying the configured owner identity is not enough: the bearer
        # itself must be that owner before any lease state can change.
        assert (
            request(
                {"operation": "lease", "run_id": RUN, "owner_id": owner_principal},
                other_token,
            )[0]
            == 403
        )
        assert (
            request(
                {"operation": "lease", "run_id": RUN, "owner_id": other_principal},
                other_token,
            )[0]
            == 409
        )
        assert store.status(RUN)["owner"]["id"] is None

        status, _ = request(
            {
                "operation": "report",
                "run_id": RUN,
                "source": "openclaw",
                "source_event_id": "credential-owner-report",
                "subject": "worker-1",
                "sequence": 1,
                "kind": "worker_brief_completed",
                "summary": "Ready for authorized owner acknowledgement",
            },
            owner_token,
        )
        assert status == 200
        status, payload = request(
            {"operation": "lease", "run_id": RUN, "owner_id": owner_principal},
            owner_token,
        )
        assert status == 200
        epoch = payload["continuity"]["owner"]["epoch"]
        assert payload["continuity"]["owner_handoff"]["state"] == "owner_lease_required"
        assert (
            request(
                {
                    "operation": "consume",
                    "run_id": RUN,
                    "owner_id": owner_principal,
                    "owner_epoch": epoch,
                },
                other_token,
            )[0]
            == 403
        )
        status, payload = request(
            {
                "operation": "consume",
                "run_id": RUN,
                "owner_id": owner_principal,
                "owner_epoch": epoch,
            },
            owner_token,
        )
        assert status == 200 and payload["delivery"]["authorized"]
        event_id = payload["delivery"]["event_id"]
        assert (
            payload["continuity"]["owner_handoff"]["state"]
            == "acknowledgement_required"
        )
        assert (
            request(
                {
                    "operation": "acknowledge",
                    "run_id": RUN,
                    "owner_id": owner_principal,
                    "owner_epoch": epoch,
                    "event_id": event_id,
                    "checkpoint": "intruder acknowledgement",
                },
                other_token,
            )[0]
            == 403
        )
        status, payload = request(
            {
                "operation": "acknowledge",
                "run_id": RUN,
                "owner_id": owner_principal,
                "owner_epoch": epoch,
                "event_id": event_id,
                "checkpoint": "authorized acknowledgement",
            },
            owner_token,
        )
        assert status == 200
        assert payload["continuity"]["owner_handoff"]["state"] == "acknowledged"
        assert payload["continuity"]["checkpoint"] == "authorized acknowledgement"
    finally:
        server.shutdown()
        server.server_close()


def test_http_initialize_requires_host_issued_handoff_and_cannot_be_raced(
    bridge, tmp_path
):
    """An unrelated device cannot win an unbound observer run's first init."""
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings
    from drover.server.web.credentials import CredentialStore

    store, _, registry = bridge
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, host_token = credentials.issue(scope="host", label="studio", host_id="studio")
    owner_credential, owner_token = credentials.issue(scope="device", label="owner")
    _, unrelated_token = credentials.issue(scope="device", label="unrelated")
    owner_principal = "credential:" + owner_credential.id
    collector = MetricsCollector(
        duckdb_path=store.path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(
            enabled=True,
            api_token="",
            credentials=credentials,
            legacy_token_enabled=False,
        ),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}/harness/factory-observer/continuity"

    def request(body, token):
        req = urllib.request.Request(
            base,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def initialize(session_id, token, handoff_token=None):
        body = {
            "operation": "initialize",
            "session_id": session_id,
            "objective": "Fix #497",
            "checkpoint": "Implementation pending",
        }
        if handoff_token is not None:
            body["handoff_token"] = handoff_token
        return request(body, token)

    def issue(session_id, target):
        return request(
            {
                "operation": "issue_handoff",
                "session_id": session_id,
                "objective": "Fix #497",
                "checkpoint": "Implementation pending",
                "authorized_owner_id": target,
            },
            host_token,
        )

    try:
        # This is the reviewer's repro: authentication alone is not evidence
        # that the caller owns this unbound observer run.
        status, _ = initialize(SESSION, unrelated_token)
        assert status == 403
        assert store.status(RUN)["owner_handoff"]["authorized_owner_id"] is None

        status, issued = issue(SESSION, owner_principal)
        assert status == 200
        handoff_token = issued["handoff_token"]
        assert initialize(SESSION, unrelated_token, handoff_token)[0] == 403
        assert store.status(RUN)["owner_handoff"]["authorized_owner_id"] is None
        status, initialized = initialize(SESSION, owner_token, handoff_token)
        assert status == 200
        assert initialized["continuity"]["owner_handoff"]["authorized_owner_id"] == (
            owner_principal
        )
        assert initialize(SESSION, owner_token)[0] == 200

        race_run = "run_FACTORY498"
        race_session = "observer-498"
        registry.create_session(
            session_id=race_session,
            host_id="studio",
            harness="codex",
            command="codex",
            status="running",
            handoff_mode="factory_observer",
            source_session_id=f"factory/{race_run}@4",
        )
        status, issued = issue(race_session, owner_principal)
        assert status == 200
        race_token = issued["handoff_token"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda token: initialize(race_session, token, race_token)[0],
                    [unrelated_token, owner_token],
                )
            )
        assert sorted(outcomes) == [200, 403]
        assert store.status(race_run)["owner_handoff"]["authorized_owner_id"] == (
            owner_principal
        )
    finally:
        server.shutdown()
        server.server_close()
