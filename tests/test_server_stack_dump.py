"""`drover-server run` can be asked for every thread's stack without root."""

from __future__ import annotations

import faulthandler
import io
import signal
import sys

from drover.server.__main__ import _register_stack_dump


def test_register_stack_dump_installs_a_sigusr1_handler() -> None:
    faulthandler.unregister(signal.SIGUSR1)
    try:
        _register_stack_dump()
        # unregister() reports whether a handler was registered.
        assert faulthandler.unregister(signal.SIGUSR1) is True
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_register_stack_dump_ignores_a_replaced_stderr(monkeypatch) -> None:
    """A stream with no file descriptor (click's CliRunner, a test harness)
    must not stop the server. The dump goes to the process's own stderr,
    which is where the launchd log is, not to whatever replaced it.
    """
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    faulthandler.unregister(signal.SIGUSR1)
    try:
        _register_stack_dump()
        assert faulthandler.unregister(signal.SIGUSR1) is True
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_register_stack_dump_never_raises_without_a_usable_stderr(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "__stderr__", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    faulthandler.unregister(signal.SIGUSR1)
    try:
        _register_stack_dump()  # must not raise
    finally:
        faulthandler.unregister(signal.SIGUSR1)
