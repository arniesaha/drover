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
    assert "DROVER_CONTINUITY_OWNER_TOKEN" in prompt
    payload = collector.launches[0][1]
    token = payload["continuity_owner_token"]
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


def test_finished_turn_completes_claim_and_allows_next_wake_turn(tmp_path):
    runner, collector, store, run_id, clock = _runner(tmp_path)

    assert runner.process_once() == 1
    session_id = collector.launches[0][1]["client_session_id"]
    owner_session = runner.registry.session_by_client_id(session_id)
    runner.registry.update_session_activity(owner_session.session_id, awaiting="input")
    runner.registry.append_event(
        session_id=owner_session.session_id,
        event_type="status",
        seq=2,
        payload={"payload": {"turn_complete": True, "awaiting": "input"}},
    )
    runner.registry.append_event(
        session_id=owner_session.session_id,
        event_type="status",
        seq=3,
        payload={"payload": {"exited": 0}},
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
    new_token = collector.launches[0][1]["continuity_owner_token"]
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
