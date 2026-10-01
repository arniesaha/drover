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
from drover.server.harness.updater import is_quiescent

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
    """Agy cannot recover a session after harnessd restarts: the session
    would come back errored and its conversation lost. Parked or not, it
    holds the update back until its user closes it."""
    release = tmp_path / "release"
    manager = _manager(tmp_path, AgyAdapter(), "sess-1", release)
    try:
        assert AgyAdapter.recover_after_restart is False
        assert manager.awaiting("sess-1") == "input"
        assert manager._require_entry("sess-1").driver.has_turn_in_flight() is False
        assert manager.is_busy("sess-1") is True
        assert is_quiescent(_state(manager)) is False
    finally:
        release.touch()
        manager.close("sess-1")
