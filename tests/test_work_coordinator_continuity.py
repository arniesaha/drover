import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    FactoryObserverContinuity,
    record_work_coordinator_state,
)
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import (
    parse_unverified_work_coordinator_continuity,
    parse_work_coordinator_continuity,
    sign_work_coordinator_continuity,
)


def test_bridge_recovery_reconstructs_signed_continuity_and_initializes_ledger(
    tmp_path,
):
    from drover.server.metrics import MetricsCollector
    from drover.server.work_queue.runtime import HarnessBridge

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    run_key = "recovery-key"
    remote = {
        "session_id": "harness-recovery",
        "client_session_id": run_key,
        "harness": "codex",
        "status": "running",
    }
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    collector.work_control = SimpleNamespace(
        store=SimpleNamespace(
            links=lambda *, run_key: [
                {
                    "run_key": run_key,
                    "goal_id": "goal",
                    "run_role": "work",
                    "item_url": "https://github.com/example/project/issues/804",
                }
            ]
        ),
        register_hub_launch=lambda *args, **kwargs: None,
    )
    collector._harness_request = lambda *args, **kwargs: (
        200,
        json.dumps({"sessions": [remote]}),
    )
    registry = HarnessRegistry(path)
    bridge = object.__new__(HarnessBridge)
    bridge.collector = collector
    bridge.config = SimpleNamespace(
        goals=[
            SimpleNamespace(
                goal_id="goal", continuity_owner_id="agent:main:telegram:owner"
            )
        ]
    )
    bridge.registry = SimpleNamespace(
        session_by_client_id=registry.session_by_client_id,
        list_hosts=lambda: [
            SimpleNamespace(
                host_id="studio", liveness=lambda: SimpleNamespace(usable=True)
            )
        ],
    )

    session = bridge.resolve(run_key)

    assert session.session_id == "harness-recovery"
    status = FactoryObserverContinuity(path).status("work-" + run_key)
    assert [event["report"]["kind"] for event in status["events"]] == ["worker_started"]


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


def test_hub_only_binding_secret_is_not_a_harness_bearer_token():
    unsigned = {
        "run_id": "work-abc123",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Issue #804 work",
    }
    body = {
        "work_coordinator_continuity": {
            **unsigned,
            "binding_signature": sign_work_coordinator_continuity(
                unsigned, "hub-continuity-secret"
            ),
        }
    }
    with pytest.raises(ValueError, match="hub authenticated"):
        parse_work_coordinator_continuity(body, hub_secret="harness-bearer-token")
    binding, _ = parse_work_coordinator_continuity(
        body, hub_secret="hub-continuity-secret"
    )
    assert binding is not None
    host_binding, _ = parse_unverified_work_coordinator_continuity(body)
    assert host_binding == binding


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
        work_coordinator_continuity_secret="hub-continuity-secret",
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
                unsigned, "hub-continuity-secret"
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


def test_hub_sync_fails_closed_when_binding_cannot_initialize(tmp_path):
    import json

    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    HarnessRegistry(path).create_session(
        session_id="harness-conflict",
        host_id="studio",
        harness="codex",
        command="codex",
        source_session_id="work/other-run@0",
        handoff_mode="work_coordinator",
    )
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token="hub-secret",
        work_coordinator_continuity_secret="hub-continuity-secret",
    )
    unsigned = {
        "run_id": "work-hub-conflict",
        "owner_id": "agent:main:telegram:owner",
        "authority_scope": "implementation",
        "objective": "Must not be rebound",
    }
    with pytest.raises(RuntimeError, match="hub continuity initialization failed"):
        MetricsCollector._sync_created_harness_session(
            collector,
            "studio",
            {
                "harness": "codex",
                "work_coordinator_continuity": {
                    **unsigned,
                    "binding_signature": sign_work_coordinator_continuity(
                        unsigned, "hub-continuity-secret"
                    ),
                },
            },
            json.dumps({"session_id": "harness-conflict", "status": "running"}),
        )
    with pytest.raises(KeyError):
        FactoryObserverContinuity(path).status("work-hub-conflict")


def test_hub_sync_rejects_success_response_without_session_id(tmp_path):
    from drover.server.metrics import MetricsCollector

    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    collector = MetricsCollector(
        duckdb_path=path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
    )
    with pytest.raises(ValueError, match="lacks session_id"):
        collector._sync_created_harness_session("studio", {}, '{"status": "running"}')
    with pytest.raises(ValueError, match="valid JSON"):
        collector._sync_created_harness_session("studio", {}, "not-json")
