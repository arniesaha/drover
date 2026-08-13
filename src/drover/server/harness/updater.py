"""Decide when a host may activate a newly installed version.

The whole update path hangs off one question: is this machine doing work? If
the answer is unavailable for any reason, the answer is treated as yes. An
update deferred costs a few hours; an update that kills a running agent
mid-turn costs someone's work, and they will not get it back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("drover.harnessd.update")


@dataclass(frozen=True)
class QuiesceReport:
    structured_alive: int
    terminals: int

    @property
    def is_idle(self) -> bool:
        return self.structured_alive == 0 and self.terminals == 0


def quiesce_report(state) -> QuiesceReport:
    """Count live work. Raises if either manager cannot answer."""
    alive = 0
    for session_id in state.structured.session_ids():
        if state.structured.is_alive(session_id):
            alive += 1
    terminals = len(state.pty.list_sessions())
    return QuiesceReport(structured_alive=alive, terminals=terminals)


def is_quiescent(state) -> bool:
    """True only when this host is provably doing nothing.

    Any failure to determine that is reported as busy rather than idle. A
    wedged session manager is exactly the situation where guessing idle would
    flip the symlink out from under a running agent.
    """
    try:
        return quiesce_report(state).is_idle
    except Exception:  # noqa: BLE001 - unknown means busy, never means idle
        log.warning("could not determine session state; treating host as busy")
        return False
