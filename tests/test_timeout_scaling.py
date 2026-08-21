"""Cover the one knob that decides how patient every bounded wait is."""

from __future__ import annotations

from _timeouts import TIMEOUT_SCALE_ENV, scale_timeout, timeout_scale


def test_unset_scale_leaves_local_runs_untouched(monkeypatch):
    monkeypatch.delenv(TIMEOUT_SCALE_ENV, raising=False)

    assert timeout_scale() == 1.0
    assert scale_timeout(15) == 15


def test_ci_can_buy_headroom(monkeypatch):
    monkeypatch.setenv(TIMEOUT_SCALE_ENV, "4")

    assert scale_timeout(15) == 60


def test_a_malformed_scale_falls_back_instead_of_erroring(monkeypatch):
    """A bad value must not turn every timing test into an error.

    That failure would look exactly like the flakiness this exists to remove,
    which is the worst possible disguise for a typo in a workflow file.
    """
    for bad in ("", "soon", "0", "-2", "NaN0"):
        monkeypatch.setenv(TIMEOUT_SCALE_ENV, bad)
        assert scale_timeout(15) == 15, bad
