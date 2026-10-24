from datetime import datetime, timezone

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    FactoryObserverContinuity,
    record_work_coordinator_state,
)
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import (
    parse_work_coordinator_continuity,
    sign_work_coordinator_continuity,
)


def test_work_coordinator_binding_is_exact_and_projects_start_done_blocked_and_approval(
    tmp_path,
):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    binding, body = parse_work_coordinator_continuity(
        {
            "harness": "codex",
            "work_coordinator_continuity": {
                **unsigned,
                "binding_signature": sign_work_coordinator_continuity(unsigned, "hub"),
            },
        },
        hub_secret="hub",
    )
    assert binding and "work_coordinator_continuity" not in body
    registry = HarnessRegistry(path)
    session = registry.create_session(
        session_id="harness-work",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id=binding.source_session_id,
    )
    store = FactoryObserverContinuity(path)
    run = store.initialize(
        session_id=session.session_id,
        objective=binding.objective,
        checkpoint="launched",
        authority_scope=binding.authority_scope,
        authorized_owner_id=binding.owner_id,
    )
    store.report(
        run,
        source="coordinator",
        source_event_id="launch:harness-work",
        subject="harness-work",
        sequence=0,
        kind="worker_started",
        summary="started",
    )
    with store._transaction(run) as (con, _):
        for seq, state in enumerate(
            (
                "Drover-State: blocked reason=missing-check",
                "Drover-State: needs-approval gate=merge reason=ready",
                "Drover-State: done sha=" + "a" * 40,
            ),
            1,
        ):
            assert record_work_coordinator_state(
                con,
                event_id=f"event-{seq}",
                session_id="harness-work",
                event_type="assistant_output",
                sequence=seq,
                payload={"text": state},
                received_at=datetime.now(timezone.utc),
            )
    reports = [event["report"]["kind"] for event in store.status(run)["events"]]
    assert reports == [
        "worker_started",
        "worker_blocked",
        "worker_needs_approval",
        "worker_done",
    ]
    assert store.status(run)["owner_handoff"]["authorized_owner_id"] == binding.owner_id


def test_work_coordinator_binding_rejects_noncanonical_owner():
    try:
        unsigned = {
            "run_id": "work-abc123",
            "owner_id": "operator",
            "authority_scope": "implementation",
            "objective": "x",
        }
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub"
                    ),
                }
            },
            hub_secret="hub",
        )
    except ValueError as exc:
        assert "canonical" in str(exc)
    else:
        raise AssertionError("noncanonical owner accepted")


def test_work_coordinator_binding_rejects_forged_hub_signature():
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    with pytest.raises(ValueError, match="hub authenticated"):
        parse_work_coordinator_continuity(
            {
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": "forged",
                }
            },
            hub_secret="hub",
        )


def test_hub_relay_replay_projects_coordinator_state_once(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="harness-relay",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id="work/work-relay@0",
    )
    store = FactoryObserverContinuity(path)
    run = store.initialize(
        session_id="harness-relay",
        objective="Relay state",
        checkpoint="launched",
        authorized_owner_id="agent:main:telegram:owner",
    )
    record = {
        "event_id": "relay-state-1",
        "session_id": "harness-relay",
        "event_type": "assistant_output",
        "seq": 1,
        "payload": {"text": "Drover-State: done sha=" + "b" * 40},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    assert registry.ingest_structured_events([record]) == 1
    assert registry.ingest_structured_events([record]) == 0
    reports = [event["report"]["kind"] for event in store.status(run)["events"]]
    assert reports == ["worker_done"]


def test_hub_sync_is_the_only_ledger_initializer(tmp_path):
    import json

    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
    )
    unsigned = {
        "run_id": "work-hub-init",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Hub owns this ledger",
    }
    payload = {
        "harness": "codex",
        "mode": "structured",
        "work_coordinator_continuity": {
            **unsigned,
            "binding_signature": sign_work_coordinator_continuity(
                unsigned, "hub-secret"
            ),
        },
    }
    MetricsCollector._sync_created_harness_session(
        collector,
        "studio",
        payload,
        json.dumps({"session_id": "harness-hub", "status": "running"}),
    )
    session = HarnessRegistry(path).get_session("harness-hub")
    assert session.handoff_mode == "work_coordinator"
    assert session.source_session_id == "work/work-hub-init@0"
    status = FactoryObserverContinuity(path).status("work-hub-init")
    assert [event["report"]["kind"] for event in status["events"]] == ["worker_started"]
