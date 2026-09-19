"""`drover-server run` can be asked for every thread's stack without root."""

from __future__ import annotations

import faulthandler
import signal

from drover.server.__main__ import _register_stack_dump


def test_register_stack_dump_installs_a_sigusr1_handler() -> None:
    faulthandler.unregister(signal.SIGUSR1)
    try:
        _register_stack_dump()
        # unregister() reports whether a handler was registered.
        assert faulthandler.unregister(signal.SIGUSR1) is True
    finally:
        faulthandler.unregister(signal.SIGUSR1)
