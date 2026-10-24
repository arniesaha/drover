from datetime import datetime, timezone

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import FactoryObserverContinuity, record_work_coordinator_state
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import parse_work_coordinator_continuity


def test_work_coordinator_binding_is_exact_and_projects_start_done_blocked_and_approval(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    binding, body = parse_work_coordinator_continuity({
        "harness": "codex", "work_coordinator_continuity": {
            "run_id": "work-abc123", "owner_id": "agent:main:telegram:owner",
            "authority_scope": "implementation", "objective": "Issue #804 work",
        },
    })
    assert binding and "work_coordinator_continuity" not in body
    registry = HarnessRegistry(path)
    session = registry.create_session(
        session_id="harness-work", host_id="studio", harness="codex", command="codex",
        status="running", handoff_mode="work_coordinator", source_session_id=binding.source_session_id,
    )
    store = FactoryObserverContinuity(path)
    run = store.initialize(session_id=session.session_id, objective=binding.objective,
                           checkpoint="launched", authority_scope=binding.authority_scope,
                           authorized_owner_id=binding.owner_id)
    store.report(run, source="coordinator", source_event_id="launch:harness-work", subject="harness-work",
                 sequence=0, kind="worker_started", summary="started")
    with store._transaction(run) as (con, _):
        for seq, state in enumerate((
            "Drover-State: blocked reason=missing-check",
            "Drover-State: needs-approval gate=merge reason=ready",
            "Drover-State: done sha=" + "a" * 40,
        ), 1):
            assert record_work_coordinator_state(
                con, event_id=f"event-{seq}", session_id="harness-work", event_type="assistant_output",
                sequence=seq, payload={"text": state}, received_at=datetime.now(timezone.utc),
            )
    reports = [event["report"]["kind"] for event in store.status(run)["events"]]
    assert reports == ["worker_started", "worker_blocked", "worker_needs_approval", "worker_done"]
    assert store.status(run)["owner_handoff"]["authorized_owner_id"] == binding.owner_id


def test_work_coordinator_binding_rejects_noncanonical_owner():
    try:
        parse_work_coordinator_continuity({"work_coordinator_continuity": {
            "run_id": "work-abc123", "owner_id": "operator", "authority_scope": "implementation", "objective": "x",
        }})
    except ValueError as exc:
        assert "canonical" in str(exc)
    else:
        raise AssertionError("noncanonical owner accepted")
