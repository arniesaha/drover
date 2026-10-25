import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap_control_plane_store
from drover.server.harness.continuity import (
    IDLE_SOURCE,
    FactoryObserverContinuity,
)
from drover.server.harness.registry import HarnessRegistry

T0 = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
OWNER = "agent:coder:subagent:owner"


def _event(con, session_id, seq, event_type, text, at):
    con.execute(
        """INSERT INTO harness_events
           (event_id, session_id, event_type, payload_json, created_at, seq)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            f"{session_id}-e{seq}",
            session_id,
            event_type,
            json.dumps({"text": text}),
            at,
            seq,
        ],
    )


@pytest.fixture
def run(tmp_path):
    path = tmp_path / "control.duckdb"
    bootstrap_control_plane_store(path)
    registry = HarnessRegistry(path)
    registry.create_session(
        session_id="harness-w",
        host_id="studio",
        harness="agy",
        command="agy",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id="work/work-idle@0",
    )
    now = [T0 + timedelta(minutes=6)]
    store = FactoryObserverContinuity(path, clock=lambda: now[0])
    run_id = store.initialize(
        session_id="harness-w",
        objective="review",
        checkpoint="launched",
        authorized_owner_id=OWNER,
    )
    return SimpleNamespace(store=store, run_id=run_id, now=now, path=path)


def _state(run, awaiting="input", activity=T0, status="running"):
    with run.store._transaction() as (con, _):
        con.execute(
            "UPDATE harness_sessions SET awaiting = ?, last_activity = ?, status = ? "
            "WHERE session_id = 'harness-w'",
            [awaiting, activity, status],
        )


def _turn(run, *texts, start=1):
    with run.store._transaction() as (con, _):
        _event(con, "harness-w", start, "user_input", "review PR 788", T0)
        for i, text in enumerate(texts, start + 1):
            _event(con, "harness-w", i, "assistant_output", text, T0)


def _kinds(run):
    return [e["report"] for e in run.store.status(run.run_id)["events"]]


def test_idle_worker_with_output_wakes_owner_once(run):
    _turn(run, "Review: no blocking findings in PR 788.")
    _state(run)
    assert len(run.store.sweep_idle_undeclared(300)) == 1
    [report] = _kinds(run)
    assert report["kind"] == "worker_brief_completed"
    assert report["source"] == IDLE_SOURCE
    assert report["subject"] == "harness-w"
    assert "idle_undeclared" in report["summary"]
    assert "no blocking findings" in report["summary"]
    # Replays and later sweeps are no-ops, so the owner is woken once.
    run.store.sweep_idle_undeclared(300)
    assert len(_kinds(run)) == 1


def test_inside_deadline_no_output_declared_or_working_are_skipped(run):
    _turn(run, "partial")
    _state(run, activity=T0 + timedelta(minutes=3))
    assert run.store.sweep_idle_undeclared(300) == []  # only 3 minutes idle

    _state(run, awaiting="approval")
    assert run.store.sweep_idle_undeclared(300) == []  # not awaiting input

    _state(run, status="terminated")
    assert run.store.sweep_idle_undeclared(300) == []  # ended

    _state(run)
    with run.store._transaction() as (con, _):
        con.execute("DELETE FROM harness_events")
        _event(con, "harness-w", 1, "user_input", "go", T0)
    assert run.store.sweep_idle_undeclared(300) == []  # produced no output


def test_declared_terminal_state_is_left_to_the_explicit_projection(run):
    _turn(run, "result", "Drover-State: done sha=" + "a" * 40)
    _state(run)
    assert run.store.sweep_idle_undeclared(300) == []


def test_new_idle_turn_is_a_new_fact_and_unauthorized_run_is_ignored(run):
    _turn(run, "first answer")
    _state(run)
    run.store.sweep_idle_undeclared(300)
    _turn(run, "second answer", start=10)
    run.store.sweep_idle_undeclared(300)
    assert len(_kinds(run)) == 2

    with run.store._transaction() as (con, _):
        con.execute("UPDATE factory_observer_runs SET authorized_owner_id = NULL")
        con.execute("DELETE FROM factory_observer_inbox")
    _turn(run, "third answer", start=20)
    assert run.store.sweep_idle_undeclared(300) == []


def test_idle_worker_process_once_and_config_gate(run):
    from drover.server.work_queue.runtime import (
        IdleUndeclaredWorker,
        start_idle_undeclared_worker,
    )

    _turn(run, "done reviewing")
    _state(run)
    worker = IdleUndeclaredWorker(run.path, 300, clock=lambda: run.now[0])
    assert worker.process_once() == 1
    assert worker.process_once() == 0
    no_owner = SimpleNamespace(goals=[SimpleNamespace(continuity_owner_id=None)])
    assert (
        start_idle_undeclared_worker(
            SimpleNamespace(work_control=object()), no_owner, None
        )
        is None
    )


def test_brief_tells_workers_how_to_declare_and_what_happens_otherwise():
    from drover.server.harness.worker_state import STATE_CONTRACT

    assert "Drover-State: done sha=<40-hex>" in STATE_CONTRACT
    assert "idle_undeclared" in STATE_CONTRACT


def test_scan_stops_at_the_newest_user_turn_with_mixed_null_sequences(run):
    with run.store._transaction() as (con, _):
        _event(con, "harness-w", 1, "assistant_output", "old turn answer", T0)
        con.execute(
            "INSERT INTO harness_events (event_id, session_id, event_type, "
            "payload_json, created_at, seq) VALUES ('null-user', 'harness-w', "
            "'user_input', '{\"text\": \"next\"}', ?, NULL)",
            [T0 + timedelta(seconds=5)],
        )
        # Many outputs from the new turn must not hide its boundary.
        for seq in range(2, 260):
            _event(
                con,
                "harness-w",
                seq,
                "assistant_output",
                f"chunk {seq}",
                T0 + timedelta(seconds=10 + seq),
            )
    _state(run)
    [event_id] = run.store.sweep_idle_undeclared(300)
    [report] = _kinds(run)
    assert "chunk 259" in report["summary"] and "old turn" not in report["summary"]
