"""Live structured-turn state is distinct from restart quiescence.

``StructuredSessionManager.is_busy`` answers "would restarting harnessd cut
work off?", so it reports a persistent driver as busy for as long as it lives.
Hub continuity settlement asks a narrower question: is a turn in flight right
now? Reusing ``is_busy`` there (via the ``turn_active`` session field) fenced
every reused persistent owner session after its first turn, which turned the
relay e2e ``test_native_owner_claim_settles_via_real_structured_relay`` red on
main after #977.
"""

from __future__ import annotations

from pathlib import Path

from test_structured_quiesce_busy import _manager, _wait_until

from drover.server.harness.structured.adapters import CodexAdapter


class _GuardedUnrecoverable(CodexAdapter):
    """A persistent driver: restart-unsafe while alive, but turn-guarded."""

    recover_after_restart = False
    persistent_turn_guard = True


def test_parked_persistent_session_is_busy_for_restart_but_has_no_turn(tmp_path):
    release: Path = tmp_path / "release"
    manager = _manager(tmp_path, _GuardedUnrecoverable(), "sess-1", release)
    try:
        assert manager.awaiting("sess-1") == "input"
        # Restart safety and live turn state disagree here by design.
        assert manager.is_busy("sess-1") is True
        assert manager.turn_in_flight("sess-1") is False

        manager.send_turn("sess-1", "do the thing")
        assert manager.turn_in_flight("sess-1") is True

        release.touch()
        _wait_until(lambda: manager.turn_in_flight("sess-1") is False)
    finally:
        release.touch()
        manager.close("sess-1")


def test_driver_turn_signal_is_used_when_there_is_no_turn_guard(tmp_path):
    release: Path = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        assert manager.turn_in_flight("sess-1") is False
        manager.send_turn("sess-1", "do the thing")
        assert manager.turn_in_flight("sess-1") is True
        release.touch()
        _wait_until(lambda: manager.turn_in_flight("sess-1") is False)
    finally:
        release.touch()
        manager.close("sess-1")


def test_unknown_session_is_unknown_not_idle(tmp_path):
    release: Path = tmp_path / "release"
    manager = _manager(tmp_path, CodexAdapter(), "sess-1", release)
    try:
        assert manager.turn_in_flight("never-existed") is None
    finally:
        release.touch()
        manager.close("sess-1")
