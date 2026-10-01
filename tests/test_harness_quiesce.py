"""A host must not activate a new version while it is doing work.

This is the single predicate standing between an update and interrupting
someone's agent mid-turn, so it gets its own module and its own tests. The
governing rule: if the answer is unavailable for any reason, the answer is
busy. An update deferred costs a few hours; an update that kills a running
agent costs someone's work.
"""

from __future__ import annotations

from types import SimpleNamespace

from drover.server.harness.updater import is_quiescent, quiesce_report


class _Structured:
    def __init__(self, alive_ids=(), dead_ids=(), parked_ids=()):
        self._alive = set(alive_ids)
        self._all = list(alive_ids) + list(dead_ids) + list(parked_ids)
        self._parked = set(parked_ids)

    def session_ids(self):
        return list(self._all)

    def is_alive(self, session_id):
        return session_id in self._alive or session_id in self._parked

    def is_busy(self, session_id):
        return session_id in self._alive


def _state(structured=None, terminals=()):
    return SimpleNamespace(
        structured=structured or _Structured(),
        pty=SimpleNamespace(list_sessions=lambda: list(terminals)),
    )


def test_an_idle_host_is_quiescent():
    assert is_quiescent(_state()) is True


def test_a_live_structured_session_blocks():
    assert is_quiescent(_state(structured=_Structured(alive_ids=["s1"]))) is False


def test_a_finished_structured_session_does_not_block():
    """A session row that exists but is not alive is history, not work."""
    assert is_quiescent(_state(structured=_Structured(dead_ids=["s1"]))) is True


def test_an_attached_terminal_blocks():
    assert is_quiescent(_state(terminals=["t1"])) is False


def test_a_terminal_blocks_even_with_no_structured_sessions():
    state = _state(structured=_Structured(dead_ids=["s1"]), terminals=["t1"])
    assert is_quiescent(state) is False


def test_report_counts_both_kinds():
    report = quiesce_report(
        _state(structured=_Structured(alive_ids=["s1", "s2"]), terminals=["t1"])
    )
    assert report.structured_alive == 2
    assert report.terminals == 1
    assert report.is_idle is False


def test_report_on_an_idle_host():
    report = quiesce_report(_state())
    assert (report.structured_alive, report.terminals) == (0, 0)
    assert report.is_idle is True


def test_a_broken_session_manager_blocks_rather_than_assuming_idle():
    """If we cannot tell, we must not update. Guessing idle kills live work."""

    class _Broken:
        def session_ids(self):
            raise RuntimeError("manager is wedged")

    assert is_quiescent(_state(structured=_Broken())) is False


def test_a_broken_pty_manager_blocks_too():
    def boom():
        raise RuntimeError("pty manager is wedged")

    state = SimpleNamespace(
        structured=_Structured(),
        pty=SimpleNamespace(list_sessions=boom),
    )
    assert is_quiescent(state) is False


def test_is_busy_raising_blocks():
    """A per-session failure is still an unknown answer."""

    class _PartiallyBroken:
        def session_ids(self):
            return ["s1"]

        def is_busy(self, session_id):
            raise RuntimeError("driver gone")

    assert is_quiescent(_state(structured=_PartiallyBroken())) is False


def test_a_session_parked_between_turns_does_not_block():
    """Open but idle is not work (drover#236): a host with a session waiting
    on its user must still be able to take an update."""
    state = _state(structured=_Structured(parked_ids=["s1", "s2"]))
    assert is_quiescent(state) is True
    assert quiesce_report(state).structured_alive == 0


def test_a_parked_session_does_not_hide_a_busy_one():
    state = _state(structured=_Structured(alive_ids=["s1"], parked_ids=["s2"]))
    assert quiesce_report(state).structured_alive == 1
    assert is_quiescent(state) is False


def test_a_manager_that_cannot_say_busy_blocks():
    """Liveness alone is no longer enough to call a host idle. A manager that
    only answers is_alive is an unknown answer, which means busy."""

    class _LivenessOnly:
        def session_ids(self):
            return ["s1"]

        def is_alive(self, session_id):
            return False

    assert is_quiescent(_state(structured=_LivenessOnly())) is False
