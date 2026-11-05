import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.db import control_plane_connection
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
)
from drover.server.harness.registry import HarnessRegistry
from drover.server.web.auth import AuthSettings, request_authorized
from drover.server.web.credentials import CredentialStore
from drover.server.work_queue.config import GoalScope
from drover.server.work_queue.native_owner import NativeOwnerRunner


@pytest.fixture(autouse=True)
def _keep_test_host_launchable(monkeypatch):
    """Registry timestamps are wall-clock based; keep this unit fixture independent of DST."""
    monkeypatch.setenv("DROVER_HOST_STALE_AFTER_SECONDS", "7200")
    monkeypatch.setenv("DROVER_HOST_OFFLINE_AFTER_SECONDS", "7200")


class Clock:
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _scope():
    return GoalScope.model_validate(
        {
            "goal_id": "goal-native",
            "repo": "example/project",
            "goal_label": "goal:native",
            "execution_enabled": True,
            "execution_cwds": {"studio": "/workspace/project"},
            "brief_ref": "docs/brief.md",
            "continuity_owner_id": "drover:owner/goal-native",
            "continuity_owner_harness": "codex",
            "credential": {"token_env": "TOKEN", "capabilities": ["issues_read"]},
        }
    )


def _capabilities():
    return {
        "harnesses": [
            {
                "name": "codex",
                "enabled": True,
                "capabilities": {
                    "schema_version": 1,
                    "harness_id": "codex",
                    "launch_modes": ["structured"],
                },
            }
        ]
    }


class Collector:
    def __init__(self, path, registry):
        self.duckdb_path = path
        self.registry = registry
        self.launches = []
        self.turn_attempts = []
        self.accepted_turns = set()
        self.failed_launches = 0
        self.failed_turns = 0
        self.recorded_failed_turns = 0

    def proxy_create_harness_session(self, host_id, payload):
        self.launches.append((host_id, payload))
        if self.failed_launches:
            self.failed_launches -= 1
            return 503, "{}"
        session = self.registry.create_session(
            session_id="owner-" + payload["client_session_id"][-8:],
            host_id=host_id,
            harness=payload["harness"],
            command="codex",
            status="running",
            cwd=payload["cwd"],
            mode="structured",
            client_session_id=payload["client_session_id"],
            handoff_mode=payload["handoff_mode"],
        )
        return 202, json.dumps({"session_id": session.session_id})

    def proxy_harness_session_action(self, session_id, action, payload):
        assert action == "turns"
        self.turn_attempts.append((session_id, payload))
        if self.failed_turns:
            self.failed_turns -= 1
            if self.recorded_failed_turns:
                self.recorded_failed_turns -= 1
                self.accepted_turns.add(payload["client_turn_id"])
                self.registry.append_event(
                    session_id=session_id,
                    event_type="user_input",
                    payload={"turn_id": payload["client_turn_id"]},
                )
            return 503, "{}"
        self.accepted_turns.add(payload["client_turn_id"])
        self.registry.append_event(
            session_id=session_id,
            event_type="user_input",
            payload={"turn_id": payload["client_turn_id"]},
        )
        return 202, "{}"


def _runner(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.register_host(
        host_id="studio",
        display_name="Studio",
        kind="macos",
        capabilities=_capabilities(),
    )
    registry.create_session(
        session_id="observer-session",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="factory_observer",
        source_session_id="factory/run-native@1",
    )
    clock = Clock()
    store = FactoryObserverContinuity(path, clock=clock)
    owner_id = "drover:owner/goal-native"
    run_id = store.initialize(
        session_id="observer-session",
        objective="Native continuity",
        checkpoint="started",
        authorized_owner_id=owner_id,
    )
    store.report(
        run_id,
        source="hub",
        source_event_id="blocked-1",
        subject="worker-1",
        sequence=1,
        kind="worker_blocked",
        summary="blocked",
    )
    collector = Collector(path, registry)
    credentials = CredentialStore(tmp_path / "credentials.json")
    runner = NativeOwnerRunner(
        collector,
        SimpleNamespace(goals=[_scope()]),
        credential_store=credentials,
        clock=clock,
        claim_seconds=10,
    )
    return runner, collector, store, run_id, clock


def test_owner_wake_launches_one_hub_native_owner_turn(tmp_path):
    runner, collector, store, run_id, _ = _runner(tmp_path)

    assert runner.process_once() == 1
    assert len(collector.launches) == 1
    assert len(collector.turn_attempts) == 1
    prompt = collector.turn_attempts[0][1]["text"]
    assert f"run_id: {run_id}" in prompt
    assert "wake_id:" in prompt and "action_intent" in prompt
    assert "DROVER_CONTINUITY_OWNER_BEARER" in prompt
    payload = collector.launches[0][1]
    token = payload["continuity_owner_bearer"]
    assert runner.credentials.find_active(token).owner_id == "drover:owner/goal-native"
    assert store.status(run_id)["owner"]["live"]


def test_rewake_does_not_dispatch_another_owner_turn_while_claimed(tmp_path):
    runner, collector, _, _, _ = _runner(tmp_path)

    assert runner.process_once() == 1
    assert runner.process_once() == 0
    assert len(collector.turn_attempts) == 1


def test_native_ack_requires_completed_intent_across_redelivery(tmp_path):
    runner, _, store, run_id, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    status = store.status(run_id)
    owner = status["owner"]
    action = status["next_action"]
    fence = {"owner_id": owner["id"], "owner_epoch": owner["epoch"]}
    with pytest.raises(ContinuityConflict, match="completed action intent"):
        store.acknowledge(
            run_id, **fence, event_id=action["event_id"], checkpoint="bypass"
        )

    store.action_intent(
        run_id,
        **fence,
        wake_id=action["event_id"],
        action_fingerprint=action["action_id"],
    )
    clock.advance(5)
    assert store.consume(run_id, **fence)["action_intent"]["state"] == "intent"
    with pytest.raises(ContinuityConflict, match="completed action intent"):
        store.acknowledge(
            run_id, **fence, event_id=action["event_id"], checkpoint="still bypass"
        )

    store.action_intent(
        run_id,
        **fence,
        wake_id=action["event_id"],
        action_fingerprint=action["action_id"],
        artifact="commit:deadbeef",
    )
    store.acknowledge(
        run_id, **fence, event_id=action["event_id"], checkpoint="reconciled"
    )
    assert store.status(run_id)["inbox_counts"] == {"acknowledged": 1}


def test_expired_claim_after_restart_never_redispatches_a_mid_turn(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    first_turn = collector.turn_attempts[0][1]["client_turn_id"]
    # A hub restart must consult the durable claim, not treat its age as
    # permission to send another turn into the same owner session.
    clock.advance(61)
    restarted = NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )

    assert restarted.process_once() == 0
    assert len(collector.turn_attempts) == 1
    assert collector.accepted_turns == {first_turn}


def test_expired_sent_claim_with_completed_receipt_settles_for_successor(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    wake_id = _acknowledge_current_wake(
        store, run_id, runner.config.goals[0].continuity_owner_id
    )
    owner_session = runner.registry.session_by_client_id(
        collector.launches[0][1]["client_session_id"]
    )
    runner.registry.update_session_activity(owner_session.session_id, awaiting="input")
    clock.advance(11)

    runner._settle_expired_claims(runner.config.goals[0])
    with control_plane_connection(runner.path) as con:
        [state] = con.execute(
            "SELECT state FROM continuity_owner_turn_claims WHERE wake_id = ?",
            [wake_id],
        ).fetchone()
    assert state == "completed"
    assert not runner._has_active_claim(runner.config.goals[0])


def test_expired_sent_claim_without_receipt_remains_fenced(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    owner_session = runner.registry.session_by_client_id(
        collector.launches[0][1]["client_session_id"]
    )
    runner.registry.update_session_activity(owner_session.session_id, awaiting="input")
    clock.advance(11)

    runner._settle_expired_claims(runner.config.goals[0])
    assert runner._has_active_claim(runner.config.goals[0])
    assert runner.process_once() == 0
    assert len(collector.turn_attempts) == 1


def test_failed_owner_launches_exhaust_wake_once(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)
    collector.failed_launches = 3

    for delay in (5, 10, 20):
        assert runner.process_once() == 0
        clock.advance(delay)

    assert runner.process_once() == 0
    assert len(collector.launches) == 3
    assert store.status(run_id)["worker_state"] == "blocked:human"
    assert store.owner_runs("drover:owner/goal-native") == []


def test_failed_turn_submission_retries_the_same_owner_turn(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)
    collector.failed_turns = 1

    assert runner.process_once() == 0
    failed_turn = collector.turn_attempts[0][1]["client_turn_id"]
    clock.advance(5)
    assert runner.process_once() == 1
    assert [attempt[1]["client_turn_id"] for attempt in collector.turn_attempts] == [
        failed_turn,
        failed_turn,
    ]


def test_recorded_turn_stays_fenced_after_failed_submission_response(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)
    collector.failed_turns = collector.recorded_failed_turns = 1

    assert runner.process_once() == 0
    clock.advance(5)
    restarted = NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )
    assert restarted.process_once() == 0
    assert len(collector.turn_attempts) == 1


def test_restart_after_failed_submission_dead_letters_once(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)
    collector.failed_turns = 3

    assert runner.process_once() == 0
    clock.advance(5)
    restarted = NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )
    for delay in (10, 20):
        assert restarted.process_once() == 0
        clock.advance(delay)
    assert restarted.process_once() == 0
    status = store.status(run_id)
    assert len(collector.turn_attempts) == 3
    assert status["inbox_counts"] == {"exhausted": 1}
    assert status["worker_state"] == "blocked:human"
    assert store.owner_runs("drover:owner/goal-native") == []
    with control_plane_connection(runner.path) as con:
        [action_json] = con.execute(
            "SELECT action_json FROM factory_observer_inbox WHERE run_id = ?", [run_id]
        ).fetchone()
    assert json.loads(action_json)["escalation"]["state"] == "blocked:human"


def _acknowledge_current_wake(store, run_id, owner_id):
    status = store.status(run_id)
    owner = status["owner"]
    action = status["next_action"]
    fence = {"owner_id": owner_id, "owner_epoch": owner["epoch"]}
    receipt = store.action_intent(
        run_id,
        **fence,
        wake_id=action["event_id"],
        action_fingerprint=action["action_id"],
        artifact="issue-comment:done",
    )
    assert receipt["state"] == "complete"
    store.acknowledge(run_id, **fence, event_id=action["event_id"], checkpoint="done")
    return action["event_id"]


def test_acknowledged_idle_owner_turn_completes_claim_and_delivers_successor(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    first_wake_id = _acknowledge_current_wake(
        store, run_id, runner.config.goals[0].continuity_owner_id
    )
    session_id = collector.launches[0][1]["client_session_id"]
    owner_session = runner.registry.session_by_client_id(session_id)
    runner.registry.update_session_activity(owner_session.session_id, awaiting="input")
    runner.registry.append_event(
        session_id=owner_session.session_id,
        event_type="status",
        seq=2,
        # This is the native-owner terminal path seen in #905: it signals
        # idle input without a turn_complete or exited marker.
        payload={"payload": {"awaiting": "input"}},
    )

    store.report(
        run_id,
        source="hub",
        source_event_id="ci-1",
        subject="worker-1",
        sequence=2,
        kind="ci_green",
        summary="ci green",
    )
    clock.advance(5)
    assert runner.process_once() == 1
    assert len(collector.turn_attempts) == 2
    with control_plane_connection(runner.path) as con:
        [state] = con.execute(
            "SELECT state FROM continuity_owner_turn_claims WHERE wake_id = ?",
            [first_wake_id],
        ).fetchone()
    assert state == "completed"


def test_acknowledged_owner_claim_stays_sent_while_turn_is_executing(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    first_wake_id = _acknowledge_current_wake(
        store, run_id, runner.config.goals[0].continuity_owner_id
    )
    store.report(
        run_id,
        source="hub",
        source_event_id="ci-1",
        subject="worker-1",
        sequence=2,
        kind="ci_green",
        summary="ci green",
    )

    clock.advance(5)
    assert runner.process_once() == 0
    assert len(collector.turn_attempts) == 1
    with control_plane_connection(runner.path) as con:
        [state] = con.execute(
            "SELECT state FROM continuity_owner_turn_claims WHERE wake_id = ?",
            [first_wake_id],
        ).fetchone()
    assert state == "sent"


def test_stale_owner_without_submitted_turn_is_fenced_recreated_and_acked(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)
    _, old_token = runner.credentials.issue(
        scope="owner",
        label="stale native owner",
        owner_id="drover:owner/goal-native",
    )
    scope = runner.config.goals[0]
    owner_id = scope.continuity_owner_id
    stale = runner.registry.create_session(
        session_id="stale-v0650-owner",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        cwd="/workspace/project",
        mode="structured",
        client_session_id="v0650-owner",
        handoff_mode="drover_native_continuity_owner",
    )
    link = runner._link(scope)
    with control_plane_connection(runner.path) as con:
        con.execute(
            """UPDATE continuity_owner_sessions SET session_id = ?,
            client_session_id = ?, updated_at = ? WHERE goal_id = ? AND owner_id = ?""",
            [stale.session_id, "v0650-owner", clock(), scope.goal_id, owner_id],
        )

    runner.registry.update_session_activity(
        stale.session_id,
        awaiting="input",
        last_activity=clock() - timedelta(seconds=11),
    )
    old_lease = store.lease(run_id, owner_id=owner_id)
    old_epoch = old_lease["owner_epoch"]
    old_action = store.consume(run_id, owner_id=owner_id, owner_epoch=old_epoch)
    assert old_action is not None  # v0.6.50 delivered it without a turn claim.
    clock.advance(5)

    assert runner.process_once() == 1
    assert len(collector.launches) == len(collector.turn_attempts) == 1
    new_token = collector.launches[0][1]["continuity_owner_bearer"]
    assert new_token != old_token
    assert runner.credentials.find_active(old_token) is None
    assert runner.credentials.find_active(new_token) is not None
    assert not request_authorized(
        AuthSettings(enabled=True, api_token="", credentials=runner.credentials),
        {"Authorization": f"Bearer {old_token}"},
        method="POST",
        path="/harness/factory-observer/continuity",
    )
    with control_plane_connection(runner.path) as con:
        generation, session_id = con.execute(
            """SELECT generation, session_id FROM continuity_owner_sessions
            WHERE goal_id = ? AND owner_id = ?""",
            [scope.goal_id, owner_id],
        ).fetchone()
    assert generation == link["generation"] + 1
    assert session_id != stale.session_id
    with pytest.raises(ContinuityConflict):
        store.consume(run_id, owner_id=owner_id, owner_epoch=old_epoch)

    status = store.status(run_id)
    fence = {"owner_id": owner_id, "owner_epoch": status["owner"]["epoch"]}
    action = status["next_action"]
    receipt = store.action_intent(
        run_id,
        **fence,
        wake_id=action["event_id"],
        action_fingerprint=action["action_id"],
        artifact="issue-comment:recovered",
    )
    assert receipt["state"] == "complete"
    store.acknowledge(run_id, **fence, event_id=action["event_id"], checkpoint="acked")
    assert store.status(run_id)["inbox_counts"] == {"acknowledged": 1}


def test_terminal_failed_owner_turn_is_replaced_before_redelivery(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    session_id = collector.launches[0][1]["client_session_id"]
    owner_session = runner.registry.session_by_client_id(session_id)
    runner.registry.update_session_activity(owner_session.session_id, awaiting="input")
    runner.registry.append_event(
        session_id=owner_session.session_id,
        event_type="status",
        seq=2,
        payload={"payload": {"exited": 1, "awaiting": "input"}},
    )
    clock.advance(5)

    assert runner.process_once() == 1
    assert len(collector.launches) == len(collector.turn_attempts) == 2
    assert collector.launches[1][1]["client_session_id"] != session_id


def test_expired_sent_claim_missing_session_recovers_same_wake_once(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    old_token = collector.launches[0][1]["continuity_owner_bearer"]
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)

    assert runner.process_once() == 1
    assert len(collector.launches) == 2
    assert len(collector.turn_attempts) == 2
    assert (
        collector.turn_attempts[0][1]["client_turn_id"]
        == collector.turn_attempts[1][1]["client_turn_id"]
    )
    assert (
        collector.launches[0][1]["client_session_id"]
        != collector.launches[1][1]["client_session_id"]
    )
    assert runner.credentials.find_active(old_token) is None
    assert (
        runner.credentials.find_active(
            collector.launches[1][1]["continuity_owner_bearer"]
        )
        is not None
    )
    with control_plane_connection(runner.path) as con:
        assert (
            con.execute("SELECT state FROM continuity_owner_recoveries").fetchone()[0]
            == "recovered"
        )


def test_abandoned_claim_fence_failure_leaves_blocking_claim(tmp_path, monkeypatch):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    monkeypatch.setattr(
        runner.continuity,
        "fence_owner",
        lambda *_a, **_k: (_ for _ in ()).throw(ContinuityConflict("fence failed")),
    )

    with pytest.raises(ContinuityConflict, match="fence failed"):
        runner.process_once()
    with control_plane_connection(runner.path) as con:
        assert (
            con.execute("SELECT state FROM continuity_owner_turn_claims").fetchone()[0]
            == "sent"
        )
    assert len(collector.turn_attempts) == 1


def test_fence_failure_journal_retries_without_second_epoch_fence(
    tmp_path, monkeypatch
):
    runner, collector, store, run_id, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    original = runner.continuity.fence_owner
    calls = []

    def fail_once(*args, **kwargs):
        calls.append((args, kwargs))
        raise ContinuityConflict("fence failed")

    monkeypatch.setattr(runner.continuity, "fence_owner", fail_once)
    with pytest.raises(ContinuityConflict, match="fence failed"):
        runner.process_once()
    monkeypatch.setattr(runner.continuity, "fence_owner", original)

    assert runner.process_once() == 1
    assert len(calls) == 1
    assert store.status(run_id)["owner"]["epoch"] == 3
    assert len(collector.turn_attempts) == 2


def test_stale_recovery_snapshot_cannot_revoke_successor(tmp_path):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        row = con.execute(
            "SELECT wake_id, client_turn_id, session_id, run_id, claim_until FROM continuity_owner_turn_claims"
        ).fetchone()
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    assert runner._recover_abandoned_claim(runner.config.goals[0], row)
    assert runner.process_once() == 1
    successor = collector.launches[-1][1]["continuity_owner_bearer"]
    epoch = runner.continuity.status(row[3])["owner"]["epoch"]

    assert not runner._recover_abandoned_claim(runner.config.goals[0], row)
    assert runner.credentials.find_active(successor) is not None
    assert runner.continuity.status(row[3])["owner"]["epoch"] == epoch


def test_terminal_session_cannot_bypass_fencing_journal(tmp_path, monkeypatch):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    session_id = collector.turn_attempts[0][0]
    runner.registry.update_session_status(session_id, "failed")
    # This is the receiptless abandoned path: an existing terminal session
    # must not replace the link before the missing submitted turn is fenced.
    with control_plane_connection(runner.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [session_id])
    clock.advance(11)
    monkeypatch.setattr(
        runner.continuity,
        "fence_owner",
        lambda *_a, **_k: (_ for _ in ()).throw(ContinuityConflict("fence failed")),
    )
    with pytest.raises(ContinuityConflict):
        runner.process_once()
    link = runner._link(runner.config.goals[0])
    assert runner._session(runner.config.goals[0], launch=False) is None
    assert runner._link(runner.config.goals[0])["generation"] == link["generation"]


def test_status_pause_cannot_revoke_successor_from_other_runner(tmp_path, monkeypatch):
    runner_a, collector, _, _, clock = _runner(tmp_path)
    assert runner_a.process_once() == 1
    session_id = collector.turn_attempts[0][0]
    with control_plane_connection(runner_a.path) as con:
        claim = con.execute(
            "SELECT wake_id, client_turn_id, session_id, run_id, claim_until FROM continuity_owner_turn_claims"
        ).fetchone()
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [session_id])
    clock.advance(11)
    runner_b = NativeOwnerRunner(
        collector,
        runner_a.config,
        credential_store=runner_a.credentials,
        clock=clock,
        claim_seconds=10,
    )
    original_status = runner_a.continuity.status
    paused = False

    def status_then_other_runner(run_id):
        nonlocal paused
        if not paused:
            paused = True
            assert runner_b.process_once() == 1
        return original_status(run_id)

    monkeypatch.setattr(runner_a.continuity, "status", status_then_other_runner)
    assert not runner_a._recover_abandoned_claim(runner_a.config.goals[0], claim)
    successor = collector.launches[-1][1]["continuity_owner_bearer"]
    assert runner_a.credentials.find_active(successor) is not None
    assert len(collector.turn_attempts) == 2


def test_expired_recovery_executor_cannot_revoke_or_fence_successor(
    tmp_path, monkeypatch
):
    """An executor lease fences the Python frame, not merely its reservation."""
    runner_a, collector, store, run_id, clock = _runner(tmp_path)
    assert runner_a.process_once() == 1
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner_a.path) as con:
        claim = con.execute(
            "SELECT wake_id, client_turn_id, session_id, run_id, claim_until "
            "FROM continuity_owner_turn_claims"
        ).fetchone()
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    runner_b = NativeOwnerRunner(
        collector,
        runner_a.config,
        credential_store=runner_a.credentials,
        clock=clock,
        claim_seconds=10,
    )
    original_revoke = runner_a.credentials.revoke_owner_credential
    paused = False
    successor_epoch = None

    def expire_then_reclaim(owner_id, credential_id):
        nonlocal paused, successor_epoch
        if not paused:
            paused = True
            # A has reserved the recovery.  Its executor lease now expires
            # before the irreversible credential/epoch side effects.
            clock.advance(11)
            assert runner_b.process_once() == 1
            successor_epoch = store.status(run_id)["owner"]["epoch"]
        return original_revoke(owner_id, credential_id)

    monkeypatch.setattr(
        runner_a.credentials, "revoke_owner_credential", expire_then_reclaim
    )
    assert not runner_a._recover_abandoned_claim(runner_a.config.goals[0], claim)

    successor = collector.launches[-1][1]["continuity_owner_bearer"]
    assert runner_a.credentials.find_active(successor) is not None
    assert store.status(run_id)["owner"]["epoch"] == successor_epoch
    assert len(collector.turn_attempts) == 2
    with control_plane_connection(runner_a.path) as con:
        state, executor_id = con.execute(
            "SELECT state, executor_id FROM continuity_owner_recoveries"
        ).fetchone()
    assert state == "recovered"
    assert executor_id == runner_b._recovery_executor_id


def test_recovery_revocation_failure_keeps_claim_blocked_until_retry(
    tmp_path, monkeypatch
):
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    original_revoke = runner.credentials.revoke_owner_credential
    monkeypatch.setattr(
        runner.credentials, "revoke_owner_credential", lambda *_args: False
    )

    assert runner.process_once() == 0
    with control_plane_connection(runner.path) as con:
        assert con.execute(
            "SELECT state FROM continuity_owner_turn_claims"
        ).fetchone() == ("sent",)
        assert con.execute(
            "SELECT state FROM continuity_owner_recoveries"
        ).fetchone() == ("fencing",)

    monkeypatch.setattr(runner.credentials, "revoke_owner_credential", original_revoke)
    assert runner.process_once() == 1
    assert len(collector.turn_attempts) == 2


def test_crashed_post_fence_executor_reclaims_one_same_wake(tmp_path, monkeypatch):
    runner_a, collector, _, _, clock = _runner(tmp_path)
    assert runner_a.process_once() == 1
    first_turn = collector.turn_attempts[0][1]["client_turn_id"]
    old_session = collector.turn_attempts[0][0]
    with control_plane_connection(runner_a.path) as con:
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    clock.advance(11)
    original_fence = runner_a.continuity.fence_owner

    def fence_then_crash(*args, **kwargs):
        original_fence(*args, **kwargs)
        raise RuntimeError("simulated executor crash after fence")

    monkeypatch.setattr(runner_a.continuity, "fence_owner", fence_then_crash)
    with pytest.raises(RuntimeError, match="simulated executor crash"):
        runner_a.process_once()
    with control_plane_connection(runner_a.path) as con:
        assert con.execute(
            "SELECT state, fenced_at IS NOT NULL FROM continuity_owner_recoveries"
        ).fetchone() == ("fencing", True)

    clock.advance(11)
    runner_b = NativeOwnerRunner(
        collector,
        runner_a.config,
        credential_store=runner_a.credentials,
        clock=clock,
        claim_seconds=10,
    )
    assert runner_b.process_once() == 1
    assert len(collector.turn_attempts) == 2
    assert collector.turn_attempts[-1][1]["client_turn_id"] == first_turn
    with control_plane_connection(runner_a.path) as con:
        assert con.execute(
            "SELECT state FROM continuity_owner_recoveries"
        ).fetchone() == ("recovered",)


def test_populated_v0656_native_owner_upgrade_rehearsal_is_additive(
    tmp_path, monkeypatch
):
    """Rehearse the v0.6.56 native tables through journal use and rollback read."""
    runner, collector, _, _, clock = _runner(tmp_path)
    assert runner.process_once() == 1
    session_id = collector.turn_attempts[0][0]
    with control_plane_connection(runner.path) as con:
        # This is the exact v0.6.56 session shape, populated with a sent claim.
        con.execute("DROP TABLE continuity_owner_recoveries")
        con.execute("ALTER TABLE continuity_owner_sessions RENAME TO v0656_sessions")
        con.execute("""CREATE TABLE continuity_owner_sessions (
            goal_id TEXT NOT NULL, owner_id TEXT NOT NULL, session_id TEXT,
            client_session_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (goal_id, owner_id), UNIQUE (client_session_id))""")
        con.execute("""INSERT INTO continuity_owner_sessions
            (goal_id, owner_id, session_id, client_session_id, generation, updated_at)
            SELECT goal_id, owner_id, session_id, client_session_id, generation, updated_at
            FROM v0656_sessions""")
        con.execute("DROP TABLE v0656_sessions")
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [session_id])

    # Upgrade, then rerun the additive DDL as a process restart would.
    upgraded = NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )
    NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )
    with control_plane_connection(runner.path) as con:
        session_columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info('continuity_owner_sessions')"
            ).fetchall()
        }
        recovery_columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info('continuity_owner_recoveries')"
            ).fetchall()
        }
    assert "credential_id" in session_columns
    assert "old_credential_id" in recovery_columns

    clock.advance(11)
    monkeypatch.setattr(
        upgraded.continuity,
        "fence_owner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ContinuityConflict("hold")),
    )
    with pytest.raises(ContinuityConflict, match="hold"):
        upgraded.process_once()

    with control_plane_connection(runner.path) as con:
        # A v0.6.56 binary never reads the additive journal. Its old claim
        # query still sees the pending sent row during a failed new recovery.
        assert con.execute(
            "SELECT state FROM continuity_owner_turn_claims"
        ).fetchone() == ("sent",)
        assert con.execute(
            "SELECT state FROM continuity_owner_recoveries"
        ).fetchone() == ("fencing",)

    # The failed upgraded process has left its bounded executor lease behind.
    clock.advance(11)
    resumed = NativeOwnerRunner(
        collector,
        runner.config,
        credential_store=runner.credentials,
        clock=clock,
        claim_seconds=10,
    )
    assert resumed.process_once() == 1
    with control_plane_connection(runner.path) as con:
        assert con.execute(
            "SELECT state FROM continuity_owner_recoveries"
        ).fetchone() == ("recovered",)
