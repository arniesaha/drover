"""A session parked on user input must not hold a host back from updating.

drover#236: Codex and Agy own a process only while a turn runs, but their
`is_alive()` stays true until the session is closed, so a host with any open
session never activated an update. The quiescence question is now "would a
restart cut work off", answered per session by `StructuredSessionManager.is_busy`.

These tests drive the real Codex/Agy drivers against a stand-in child process
whose lifetime the test controls, so "turn in flight" is a real process and not
a flag set by hand.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from drover.schema import bootstrap
from drover.server.harness.adapters import HarnessAdapterRegistry
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured.adapters import AgyAdapter, CodexAdapter
from drover.server.harness.structured.agy import AgyDriver
from drover.server.harness.structured.codex import CodexDriver
from drover.server.harness.structured.deepseek import DeepSeekDriver
from drover.server.harness.structured.driver import StructuredMessage
from drover.server.harness.structured.manager import StructuredSessionManager
from drover.server.harness.updater import drain_for_restart, end_drain, is_quiescent

_DEADLINE_S = 10.0


def _turn_command(release: Path) -> list[str]:
    # Stands in for the CLI: stays running until the test releases it, and
    # ignores the arguments each driver appends.
    script = (
        "import pathlib, sys, time\n"
        f"release = pathlib.Path({str(release)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )
    return [sys.executable, "-c", script]


def _wait_until(predicate) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


def _state(manager: StructuredSessionManager) -> SimpleNamespace:
    return SimpleNamespace(
        structured=manager,
        pty=SimpleNamespace(list_sessions=lambda: []),
    )


@pytest.mark.parametrize("driver_type", [AgyDriver, CodexDriver])
def test_driver_reports_a_turn_only_while_its_process_runs(driver_type, tmp_path):
    release = tmp_path / "release"
    emitted: list[StructuredMessage] = []
    driver = driver_type(_turn_command(release), str(tmp_path), emitted.append)
    driver.start()
    assert driver.is_alive() is True
    assert driver.has_turn_in_flight() is False

    driver.send_turn("hello", "turn-1")
    assert driver.has_turn_in_flight() is True

    release.touch()
    _wait_until(lambda: not driver.has_turn_in_flight())
    # Still open between turns: liveness is unchanged, only the work is gone.
    assert driver.is_alive() is True
    # The worker's last word is recorded before the driver calls itself idle
    # (agy closes a clean turn with turn_complete, codex with exited).
    last = emitted[-1]
    assert last.type == "status" and last.turn_id == "turn-1"
    assert last.payload.get("turn_complete") or "exited" in last.payload
    driver.close()


@pytest.mark.parametrize("driver_type", [AgyDriver, CodexDriver])
def test_worker_still_emitting_counts_as_in_flight(driver_type, tmp_path):
    """The worker clears the turn flag before it emits the turn's final
    events; a restart in that gap would lose them."""
    driver = driver_type(["unused"], str(tmp_path), lambda message: None)
    finishing = threading.Event()
    worker = threading.Thread(target=finishing.wait, args=(5,), daemon=True)
    worker.start()
    driver._turn_thread = worker
    try:
        assert driver._turn_active is False
        assert driver.has_turn_in_flight() is True
    finally:
        finishing.set()
        worker.join(timeout=5)
    assert driver.has_turn_in_flight() is False


def test_deepseek_counts_its_poll_thread_as_the_turn():
    """DeepSeek's turn runs in the Web RPC service; what a restart cuts off
    is the poll thread recording it."""
    driver = DeepSeekDriver(["unused"], None, lambda message: None, api=object())
    assert driver.has_turn_in_flight() is False
    driver._turn_active = True
    assert driver.has_turn_in_flight() is True
    driver._turn_active = False
    polling = threading.Event()
    worker = threading.Thread(target=polling.wait, args=(5,), daemon=True)
    worker.start()
    driver._turn_thread = worker
    try:
        assert driver.has_turn_in_flight() is True
    finally:
        polling.set()
        worker.join(timeout=5)
    assert driver.has_turn_in_flight() is False


def _manager(tmp_path, adapter, session_id: str, release: Path):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.create_session(
        host_id="test-host",
        harness=adapter.id,
        command="stand-in",
        session_id=session_id,
        status="starting",
        mode="structured",
    )
    manager = StructuredSessionManager(HarnessAdapterRegistry([adapter]))
    manager.start(
        session_id,
        harness=adapter.id,
        cwd=str(tmp_path),
        command=_turn_command(release),
        registry=registry,
        on_message=lambda sid, event: None,
        finalize=lambda sid, code: None,
    )
    return manager


def test_parked_session_lets_the_host_update_but_a_turn_does_not(tmp_path):
    """The observed failure, end to end: open session at awaiting=input with
    no child process. It must be quiescent; the same session mid-turn must
    not be."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        assert manager.awaiting("sess-1") == "input"
        assert manager.is_alive("sess-1") is True
        assert manager.is_busy("sess-1") is False
        assert is_quiescent(_state(manager)) is True

        manager.send_turn("sess-1", "do the thing")
        assert manager.is_busy("sess-1") is True
        assert is_quiescent(_state(manager)) is False

        release.touch()
        _wait_until(lambda: not manager.is_busy("sess-1"))
        assert manager.is_alive("sess-1") is True
        assert is_quiescent(_state(manager)) is True
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_dispatch_in_progress_is_busy_before_the_driver_knows(tmp_path):
    """send_turn holds the entry lock across dispatch; an update must not
    slip in between a turn being accepted and its process existing."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        entry = manager._require_entry("sess-1")
        held = threading.Event()
        done = threading.Event()

        def hold() -> None:
            with entry.lock:
                held.set()
                done.wait(timeout=5)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(timeout=5)
        try:
            assert entry.driver.has_turn_in_flight() is False
            assert manager.is_busy("sess-1") is True
        finally:
            done.set()
            holder.join(timeout=5)
        assert manager.is_busy("sess-1") is False
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_pending_approval_is_busy(tmp_path):
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        manager._require_entry("sess-1").awaiting = "approval"
        assert manager.is_busy("sess-1") is True
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_persistent_driver_stays_busy_while_alive(tmp_path):
    """Claude Code keeps one process for the whole session. That process is
    real, so its liveness still blocks -- this fix is for per-turn drivers."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:

        class _Persistent:
            def is_alive(self) -> bool:
                return True

        manager._require_entry("sess-1").driver = _Persistent()
        assert manager.is_busy("sess-1") is True
    finally:
        release.touch()


def test_a_driver_that_cannot_answer_blocks_the_update(tmp_path):
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        entry = manager._require_entry("sess-1")

        def broken() -> bool:
            raise RuntimeError("turn lock wedged")

        entry.driver.has_turn_in_flight = broken
        assert is_quiescent(_state(manager)) is False
    finally:
        release.touch()
        manager.close("sess-1")


def test_closed_and_unknown_sessions_are_not_busy(tmp_path):
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    manager.close("sess-1")
    assert manager.is_busy("sess-1") is False
    assert manager.is_busy("never-existed") is False


def test_a_parked_session_that_cannot_survive_a_restart_stays_busy(tmp_path):
    """A session whose adapter cannot recover it after harnessd restarts would
    come back errored with its conversation lost. Parked or not, it holds the
    update back until its user closes it."""

    class _Unrecoverable(CodexAdapter):
        recover_after_restart = False

    release = tmp_path / "release"
    manager = _manager(tmp_path, _Unrecoverable(), "sess-1", release)
    try:
        assert manager.awaiting("sess-1") == "input"
        assert manager._require_entry("sess-1").driver.has_turn_in_flight() is False
        assert manager.is_busy("sess-1") is True
        assert is_quiescent(_state(manager)) is False
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_parked_agy_session_lets_the_host_update(tmp_path):
    """The live case in drover#236: Agy recovers after a restart, so a session
    parked on input no longer holds the host back. Mid-turn it still does."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, AgyAdapter(), "sess-1", release)
    try:
        assert AgyAdapter.recover_after_restart is True
        assert manager.awaiting("sess-1") == "input"
        assert manager.is_busy("sess-1") is False
        assert is_quiescent(_state(manager)) is True

        manager.send_turn("sess-1", "do the thing")
        assert manager.is_busy("sess-1") is True
        assert is_quiescent(_state(manager)) is False
    finally:
        release.touch()
        manager.close("sess-1")


# Stands in for agy: records each invocation beside itself and answers like the
# real CLI -- the conversation it reports is the one it was resumed onto, or a
# new one when it was not.
_RECORDING_AGY = """\
import json, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(sys.argv[0]).with_name("argv.jsonl")
with log.open("a") as fh:
    fh.write(json.dumps(args) + "\\n")
conv = args[args.index("--conversation") + 1] if "--conversation" in args else "agy-conv-1"
print(json.dumps({"event": "init", "conversation_id": conv}))
print(json.dumps({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS"}}))
"""


def _start_agy(manager, registry, cwd, script, native_session_id=None) -> None:
    manager.start(
        "sess-1",
        harness="agy",
        cwd=str(cwd),
        command=[sys.executable, str(script)],
        registry=registry,
        on_message=lambda sid, event: None,
        finalize=lambda sid, code: None,
        native_session_id=native_session_id,
    )


def test_agy_resumes_its_conversation_after_a_harnessd_restart(tmp_path):
    """Restart past a parked Agy session, then carry on.

    The first manager stands for the harnessd that ran the update; it is
    abandoned rather than closed, as a restart abandons it. The second
    recovers from what the first persisted -- the same inputs `/recover`
    uses -- and its next turn continues the original conversation.
    """
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    script = tmp_path / "agy.py"
    script.write_text(_RECORDING_AGY, encoding="utf-8")
    registry.create_session(
        host_id="test-host",
        harness="agy",
        command="agy",
        session_id="sess-1",
        status="starting",
        mode="structured",
        cwd=str(cwd),
    )
    adapters = HarnessAdapterRegistry([AgyAdapter()])

    before = StructuredSessionManager(adapters)
    _start_agy(before, registry, cwd, script)
    before.send_turn("sess-1", "first")
    _wait_until(lambda: not before.is_busy("sess-1"))
    assert before.awaiting("sess-1") == "input"
    assert is_quiescent(_state(before)) is True
    native_session_id = registry.get_session("sess-1").native_session_id
    assert native_session_id == "agy-conv-1"

    after = StructuredSessionManager(adapters)
    _start_agy(after, registry, cwd, script, native_session_id)
    try:
        after.send_turn("sess-1", "second")
        _wait_until(lambda: not after.is_busy("sess-1"))
    finally:
        after.close("sess-1")
        before.close("sess-1")

    calls = [
        json.loads(line) for line in (tmp_path / "argv.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2
    assert "--conversation" not in calls[0]
    resumed = calls[1]
    assert resumed.count("--conversation") == 1
    assert resumed[resumed.index("--conversation") + 1] == "agy-conv-1"
    assert resumed[resumed.index("--add-dir") + 1] == str(cwd)
    assert resumed[-2:] == ["--print", "second"]
    assert registry.get_session("sess-1").native_session_id == "agy-conv-1"


def test_a_draining_manager_refuses_new_turns(tmp_path):
    """Once the updater commits to a restart, a turn must not start
    underneath it (the turn-vs-restart race on drover#236)."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        manager.begin_drain(60)
        assert manager.is_draining() is True
        with pytest.raises(RuntimeError, match="restarting for an update"):
            manager.send_turn("sess-1", "too late")
        assert manager.is_busy("sess-1") is False

        manager.end_drain()
        manager.send_turn("sess-1", "now")
        assert manager.is_busy("sess-1") is True
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_drain_lapses_if_the_restart_never_comes(tmp_path):
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        manager.begin_drain(0.05)
        _wait_until(lambda: not manager.is_draining())
        manager.send_turn("sess-1", "after the lapse")
        assert manager.is_busy("sess-1") is True
    finally:
        release.touch()
        manager.close("sess-1")


def test_drain_for_restart_holds_only_on_an_idle_host(tmp_path):
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        assert drain_for_restart(_state(manager)) is True
        assert manager.is_draining() is True
        end_drain(_state(manager))

        manager.send_turn("sess-1", "busy now")
        assert drain_for_restart(_state(manager)) is False
        # A busy host keeps taking turns: the gate stays up only for a restart
        # that is actually going to happen.
        assert manager.is_draining() is False
    finally:
        release.touch()
        manager.close("sess-1")


def test_a_turn_dispatching_as_the_drain_starts_blocks_the_restart(tmp_path):
    """The interleaving the gate exists for: a turn holds the dispatch lock
    when the drain goes up. The quiescence check must count it as work."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        entry = manager._require_entry("sess-1")
        held = threading.Event()
        done = threading.Event()

        def dispatching() -> None:
            with entry.lock:
                held.set()
                done.wait(timeout=5)

        holder = threading.Thread(target=dispatching, daemon=True)
        holder.start()
        assert held.wait(timeout=5)
        try:
            assert drain_for_restart(_state(manager)) is False
            assert manager.is_draining() is False
        finally:
            done.set()
            holder.join(timeout=5)
    finally:
        release.touch()
        manager.close("sess-1")
