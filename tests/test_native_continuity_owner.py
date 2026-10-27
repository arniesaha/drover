import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
)
from drover.server.harness.registry import HarnessRegistry
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

    def proxy_create_harness_session(self, host_id, payload):
        self.launches.append((host_id, payload))
        session = self.registry.create_session(
            session_id="owner-session",
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
        self.accepted_turns.add(payload["client_turn_id"])
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
    runner = NativeOwnerRunner(
        collector, SimpleNamespace(goals=[_scope()]), clock=clock, claim_seconds=10
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
        collector, runner.config, clock=clock, claim_seconds=10
    )

    assert restarted.process_once() == 0
    assert len(collector.turn_attempts) == 1
    assert collector.accepted_turns == {first_turn}
