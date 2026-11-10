"""Isolated fixture evidence for #924; these are not live fleet acceptance.

Use the real ingest handler, native runner, inbox/claim/receipt transactions,
and queue cap coordinator. Only the harness/effect boundary is a fixture.
"""

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_continuity_owner import _ingest_owner_events, _runner, _scope

from drover.server.db import control_plane_connection
from drover.server.harness.continuity import ContinuityConflict
from drover.server.work_queue.config import WorkQueueConfig
from drover.server.work_queue.native_owner import NativeOwnerRunner
from drover.server.work_queue.reconciler import QueueExecutionCoordinator
from drover.server.work_queue.runtime import HarnessBridge


def _rows(path, sql, params=()):
    with control_plane_connection(path) as con:
        result = con.execute(sql, list(params))
        return [
            dict(zip((c[0] for c in result.description), row))
            for row in result.fetchall()
        ]


def _save(tmp_path, scenario, evidence):
    # Retained by the foreground acceptance command's tmp_path retention option.
    receipt = {"evidence": "fixture-only", "scenario": scenario, **evidence}
    text = (
        json.dumps(
            receipt,
            default=lambda stamp: stamp.astimezone(timezone.utc).isoformat(),
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    (tmp_path / "receipt.json").write_text(text)
    print(text)
    return receipt


def _receipts(path):
    return _rows(
        path,
        """SELECT i.event_id AS wake_id, i.run_id, i.payload_json AS source_report,
        i.received_at AS event_at, i.state AS inbox_state, i.attempts,
        i.action_json AS delivery_action,
        i.acknowledged_at, c.client_turn_id, c.session_id,
        c.created_at AS turn_at, c.updated_at AS claim_updated_at,
        c.state AS claim_state, a.action_fingerprint, a.state AS receipt_state,
        a.created_at AS intent_at, a.completed_at AS effect_verified_at, a.artifact
        FROM factory_observer_inbox i
        LEFT JOIN continuity_owner_turn_claims c ON c.wake_id = i.event_id
        LEFT JOIN continuity_owner_action_intents a ON a.wake_id = i.event_id
        ORDER BY i.received_at, i.event_id""",
    )


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("DROVER_HOST_STALE_AFTER_SECONDS", "7200")
    monkeypatch.setenv("DROVER_HOST_OFFLINE_AFTER_SECONDS", "7200")
    runner, collector, store, run_id, clock = _runner(tmp_path)
    # Registry timestamps and event producer timestamps share a declared fixture
    # clock. Host registration above stays on wall time for the liveness check.
    monkeypatch.setattr("drover.server.harness.registry._now", clock)
    collector.idle_undeclared_worker = SimpleNamespace(owner_runner=runner)
    original = collector.proxy_harness_session_action
    effects = tmp_path / "effects"
    effects.mkdir()
    state = SimpleNamespace(interrupt=False, gate_denials=[])

    def execute(session_id, action, payload):
        response = original(session_id, action, payload)
        fields = dict(
            line.split(": ", 1) for line in payload["text"].splitlines() if ": " in line
        )
        run = fields["run_id"]
        status = store.status(run)
        delivery = status["next_action"]
        fence = {
            "owner_id": fields["owner_id"],
            "owner_epoch": int(fields["owner_epoch"]),
        }
        assert delivery["authorized"], "fixture effects cannot cross a human gate"
        wake = delivery["event_id"]
        store.action_intent(
            run, **fence, wake_id=wake, action_fingerprint=delivery["action_id"]
        )
        target = effects / (wake + ".json")
        # The effect target carries the deterministic wake marker. Recovery
        # reconciles it before completing the existing receipt and acking.
        if not target.exists():
            with target.open("x") as stream:
                json.dump(
                    {
                        "wake_id": wake,
                        "effect": "fixture-local-write",
                        "at": clock().isoformat(),
                    },
                    stream,
                )
        assert json.loads(target.read_text())["wake_id"] == wake
        if state.interrupt:
            state.interrupt = False
            raise RuntimeError("fixture interruption after effect, before receipt/ack")
        store.action_intent(
            run,
            **fence,
            wake_id=wake,
            action_fingerprint=delivery["action_id"],
            artifact="fixture-effect:" + wake,
        )
        store.acknowledge(
            run, **fence, event_id=wake, checkpoint="fixture effect verified"
        )
        return response

    collector.proxy_harness_session_action = execute

    def event(event_id, session_id="observer-session", *, complete=False, seq=1):
        return {
            "event_id": event_id,
            "session_id": session_id,
            "seq": seq,
            "type": "status",
            "ts": clock().isoformat(),
            "payload": {"turn_complete": True} if complete else {"fixture": "kick"},
        }

    def ingest(*events):
        return _ingest_owner_events(
            collector.idle_undeclared_worker.owner_runner, collector, list(events)
        )

    return SimpleNamespace(
        runner=runner,
        collector=collector,
        store=store,
        run_id=run_id,
        clock=clock,
        state=state,
        event=event,
        ingest=ingest,
        effects=effects,
    )


def test_unattended_three_stage_event_to_effect_receipts(acceptance, tmp_path):
    f = acceptance
    assert f.ingest(f.event("fixture-924-bootstrap")) == [(200, {"ingested": 1})]
    for stage in (2, 3):
        f.clock.advance(1)
        session_id = f.collector.turn_attempts[-1][0]
        assert f.ingest(
            f.event(
                f"fixture-924-owner-{stage - 1}-done",
                session_id,
                complete=True,
                seq=stage,
            ),
            f.event(f"fixture-924-stage-{stage}", complete=True, seq=stage),
        ) == [(200, {"ingested": 2})]
    f.clock.advance(1)
    assert f.ingest(
        f.event(
            "fixture-924-owner-3-done",
            f.collector.turn_attempts[-1][0],
            complete=True,
            seq=4,
        )
    ) == [(200, {"ingested": 1})]
    rows = _receipts(f.store.path)
    assert len(rows) == len(list(f.effects.iterdir())) == 3
    assert all(
        r["receipt_state"] == "complete"
        and r["inbox_state"] == "acknowledged"
        and r["claim_state"] == "completed"
        for r in rows
    )
    assert len({r["client_turn_id"] for r in rows}) == 3
    projections = _rows(
        f.store.path,
        "SELECT harness_event_id, state, continuity_event_id, recorded_at FROM factory_observer_completion_receipts ORDER BY recorded_at",
    )
    assert [r["state"] for r in projections] == ["projected", "projected"]
    assert f.store.owner_runs(f.runner.config.goals[0].continuity_owner_id) == []
    _save(
        tmp_path,
        "three-stage",
        {
            "clock": "synthetic UTC; no cron, chat, polling thread, or direct process_once dispatch",
            "receipts": rows,
            "completion_projections": projections,
            "effects": [json.loads(p.read_text()) for p in sorted(f.effects.iterdir())],
        },
    )


def test_redelivery_and_interrupted_owner_reconcile_one_effect_and_keep_ready_work(
    acceptance, tmp_path
):
    f = acceptance
    f.state.interrupt = True
    first = f.event("fixture-924-interrupted-bootstrap")
    assert f.ingest(first) == [(200, {"ingested": 1})]
    before = _receipts(f.store.path)
    assert before[0]["receipt_state"] == "intent"
    assert before[0]["inbox_state"] == "delivered"
    interrupted_effect = next(f.effects.iterdir()).read_text()
    f.clock.advance(1)
    ready = f.event("fixture-924-ready-during-interruption", complete=True, seq=2)
    assert f.ingest(ready) == [(200, {"ingested": 1})]
    assert f.ingest(ready) == [(200, {"ingested": 0})]
    assert f.store.status(f.run_id)["inbox_counts"] == {"delivered": 1, "pending": 1}
    old_session = f.collector.turn_attempts[0][0]
    with control_plane_connection(f.store.path) as con:
        # Supported missing-owner recovery path, as in the native runner tests.
        con.execute("DELETE FROM harness_events WHERE session_id = ?", [old_session])
        con.execute("DELETE FROM harness_sessions WHERE session_id = ?", [old_session])
    f.clock.advance(10)
    restarted = NativeOwnerRunner(
        f.collector,
        f.runner.config,
        credential_store=f.runner.credentials,
        clock=f.clock,
        claim_seconds=10,
    )
    f.collector.idle_undeclared_worker.owner_runner = restarted
    assert f.ingest(ready) == [(200, {"ingested": 0})]
    assert len(list(f.effects.iterdir())) == 1
    assert next(f.effects.iterdir()).read_text() == interrupted_effect
    assert f.store.status(f.run_id)["inbox_counts"] == {"acknowledged": 1, "pending": 1}
    ready_preserved_at = f.clock().isoformat()
    ready_snapshot = _receipts(f.store.path)
    f.clock.advance(1)
    assert f.ingest(
        f.event(
            "fixture-924-recovered-owner-done",
            f.collector.turn_attempts[-1][0],
            complete=True,
            seq=3,
        )
    ) == [(200, {"ingested": 1})]
    f.clock.advance(1)
    f.ingest(
        f.event(
            "fixture-924-ready-owner-done",
            f.collector.turn_attempts[-1][0],
            complete=True,
            seq=4,
        )
    )
    rows = _receipts(f.store.path)
    assert sorted(r["attempts"] for r in rows) == [1, 2]
    assert all(
        r["receipt_state"] == "complete"
        and r["inbox_state"] == "acknowledged"
        and r["claim_state"] == "completed"
        for r in rows
    )
    assert rows[0]["client_turn_id"] == before[0]["client_turn_id"]
    assert len(list(f.effects.iterdir())) == 2
    assert len(f.collector.turn_attempts) == 3
    recovery = _rows(
        f.store.path,
        "SELECT wake_id, state, old_owner_epoch, created_at, fenced_at, recovered_at FROM continuity_owner_recoveries",
    )
    assert len(recovery) == 1 and recovery[0]["state"] == "recovered"
    _save(
        tmp_path,
        "interrupted-redelivery",
        {
            "clock": "synthetic UTC",
            "before_interruption": before,
            "ready_preserved_at": ready_preserved_at,
            "ready_snapshot": ready_snapshot,
            "recovery": recovery,
            "receipts": rows,
            "effects": [json.loads(p.read_text()) for p in sorted(f.effects.iterdir())],
        },
    )


def test_human_gate_blocks_only_its_branch(acceptance, tmp_path):
    f = acceptance
    scope = _scope().model_copy(
        update={
            "goal_id": "goal-gated",
            "continuity_owner_id": "drover:owner/goal-gated",
        }
    )
    f.runner.config.goals.insert(0, scope)
    f.runner.registry.create_session(
        session_id="gated-worker",
        host_id="studio",
        harness="codex",
        command="fixture",
        status="running",
        handoff_mode="factory_observer",
        source_session_id="factory/run-gated@1",
    )
    gated = f.store.initialize(
        session_id="gated-worker",
        objective="Fixture deployment gate",
        checkpoint="human approval required",
        authority_scope="deployment",
        authorized_owner_id=scope.continuity_owner_id,
    )
    gate_event = f.event("fixture-924-human-gate", "gated-worker", complete=True)
    assert f.ingest(gate_event, f.event("fixture-924-unrelated-ready")) == [
        (200, {"ingested": 2})
    ]
    assert f.store.status(gated)["inbox_counts"] == {"pending": 1}
    assert f.store.owner_runs(scope.continuity_owner_id) == []
    # A separate negative boundary probe confirms that even a direct consumed
    # delivery cannot acknowledge deployment without human approval. The
    # automatic runner above never dispatched this excluded branch.
    lease = f.store.lease(gated, owner_id=scope.continuity_owner_id)
    fence = {"owner_id": scope.continuity_owner_id, "owner_epoch": lease["owner_epoch"]}
    delivery = f.store.consume(gated, **fence)
    with pytest.raises(ContinuityConflict, match="explicit deployment approval") as exc:
        f.store.acknowledge(
            gated, **fence, event_id=delivery["event_id"], checkpoint="forbidden"
        )
    f.state.gate_denials.append(str(exc.value))
    f.clock.advance(1)
    f.ingest(
        gate_event,
        f.event(
            "fixture-924-unrelated-owner-done",
            f.collector.turn_attempts[-1][0],
            complete=True,
            seq=2,
        ),
    )
    rows = _receipts(f.store.path)
    gated_rows = [r for r in rows if r["run_id"] == gated]
    ready_rows = [r for r in rows if r["run_id"] == f.run_id]
    assert len(gated_rows) == len(ready_rows) == 1
    assert (
        gated_rows[0]["inbox_state"] == "delivered"
        and gated_rows[0]["receipt_state"] is None
    )
    assert gated_rows[0]["client_turn_id"] is None
    assert (
        ready_rows[0]["inbox_state"] == "acknowledged"
        and ready_rows[0]["receipt_state"] == "complete"
        and ready_rows[0]["claim_state"] == "completed"
    )
    assert len(list(f.effects.iterdir())) == 1
    assert len(f.state.gate_denials) == 1
    assert f.store.status(gated)["next_action"]["authorized"] is False
    _save(
        tmp_path,
        "human-gated-branch",
        {
            "clock": "synthetic UTC",
            "automatic_gate_dispatches": 0,
            "negative_probe": "explicit lease/consume/ack refused; automatic gate remained pending before probe",
            "gate_denials": f.state.gate_denials,
            "receipts": rows,
            "effects": [json.loads(p.read_text()) for p in f.effects.iterdir()],
        },
    )


def test_two_ready_tasks_overlap_in_separate_fixture_worktrees_under_caps(
    acceptance, tmp_path
):
    f = acceptance
    repo = tmp_path / "fixture-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    (repo / "fixture.txt").write_text("#924 isolated fixture repository\n")
    git("add", "fixture.txt")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture #924",
    )
    scopes = []
    for label in ("a", "b"):
        worktree = tmp_path / ("worktree-" + label)
        git("worktree", "add", "--detach", str(worktree), "HEAD")
        scopes.append(
            _scope().model_copy(
                update={
                    "goal_id": "goal-" + label,
                    "continuity_owner_id": "drover:owner/goal-" + label,
                    "execution_cwds": {"studio": str(worktree)},
                    "execution_max_workers": 1,
                }
            )
        )
    config = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="fixture-924",
        goals=scopes,
        execution={"global_max_workers": 2},
    )
    bridge = HarnessBridge(f.collector, config)

    def links(**filters):
        rows = _rows(
            f.store.path,
            "SELECT session_id, client_session_id AS run_key, source_session_id AS goal_id FROM harness_sessions WHERE handoff_mode = 'fixture_924_worker'",
        )
        for row in rows:
            row.update(
                item_number=int(row["session_id"].rsplit("-", 1)[1]), run_role="work"
            )
        return [
            r
            for r in rows
            if all(
                r["item_number" if k == "number" else k] == v
                for k, v in filters.items()
            )
        ]

    def launch(goal_id, number, request_key, cost, *, actor):
        scope = next(s for s in scopes if s.goal_id == goal_id)
        session = f.runner.registry.create_session(
            session_id=f"fixture-task-{goal_id}-{number}",
            host_id="studio",
            harness="codex",
            command="fixture-local-effect",
            status="running",
            mode="structured",
            cwd=scope.execution_cwds["studio"],
            client_session_id=request_key,
            handoff_mode="fixture_924_worker",
            source_session_id=goal_id,
        )
        return next(r for r in links() if r["session_id"] == session.session_id)

    # The provider/launch boundary is a fixture. Admission uses the real
    # coordinator and HarnessBridge.active over durable registry session links.
    service = SimpleNamespace(
        config=config,
        lock=threading.RLock(),
        execution_kill_switch=False,
        harness=bridge,
        store=SimpleNamespace(links=links),
        launch=launch,
    )
    coordinator = QueueExecutionCoordinator(service)
    for scope in scopes:
        assert coordinator.launch_candidate(scope.goal_id, 1)["launched"]
    assert coordinator.launch_candidate("goal-a", 2) == {"denied": "global_cap"}
    assert len(bridge.active(links())) == 2
    barrier = threading.Barrier(2)

    def task(scope):
        cwd = scope.execution_cwds["studio"]
        started = datetime.now(timezone.utc).isoformat()
        start = time.monotonic_ns()
        # Each foreground fixture task remains executing until its independent
        # peer has entered its own worktree. No sleeps or timing thresholds.
        barrier.wait(timeout=10)
        target = Path(cwd) / "effect.json"
        target.write_text(
            json.dumps({"task": scope.goal_id, "effect": "fixture-task-write"})
        )
        end = time.monotonic_ns()
        finished = datetime.now(timezone.utc).isoformat()
        return {
            "task": scope.goal_id,
            "session_id": f"fixture-task-{scope.goal_id}-1",
            "worktree": cwd,
            "git_dir": (target.parent / ".git").read_text().strip(),
            "started_at": started,
            "finished_at": finished,
            "start_ns": start,
            "end_ns": end,
            "effect": json.loads(target.read_text()),
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        intervals = list(pool.map(task, scopes))
    assert max(r["start_ns"] for r in intervals) < min(r["end_ns"] for r in intervals)
    assert len({r["git_dir"] for r in intervals}) == 2
    # Releasing B's slot leaves A's per-goal cap binding. A denied ready task
    # is admitted once A's capacity frees as well.
    f.runner.registry.update_session_status("fixture-task-goal-b-1", "completed")
    assert coordinator.launch_candidate("goal-a", 2) == {"denied": "global_cap"}
    f.runner.registry.update_session_status("fixture-task-goal-a-1", "completed")
    assert coordinator.launch_candidate("goal-a", 2)["launched"]
    _save(
        tmp_path,
        "overlapping-ready-tasks",
        {
            "clock": "wall UTC and monotonic ns",
            "global_cap": 2,
            "per_goal_cap": 1,
            "overlap_ns": min(r["end_ns"] for r in intervals)
            - max(r["start_ns"] for r in intervals),
            "cap_denials": coordinator.health["cap_denials"],
            "ready_task_preserved": "fixture-task-goal-a-2 admitted after capacity release",
            "intervals": intervals,
            "sessions": links(),
        },
    )
