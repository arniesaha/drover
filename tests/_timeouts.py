"""One place that decides how patient a test wait should be.

Every bounded wait in this suite used to encode a deadline tuned for an idle
developer machine. That held while CI ran the suite serially. It stopped
holding when CI moved to xdist (drover#241): a GitHub runner has 4 vCPUs, the
tests spawn their own subprocesses and HTTP servers, and several workers'
children then compete with each other's tests. Three different tests began
failing CI on expired waits, all of them passing locally (drover#250).

The deadline is the wrong thing to tune per test. Scale it once, from the
environment, so a loaded runner gets more patience and an idle machine pays
nothing: a wait that succeeds returns as soon as its predicate is true, so a
larger ceiling costs time only in the failure case.
"""

from __future__ import annotations

import os

#: Env var CI sets to buy headroom. Unset locally, so local runs are unchanged.
TIMEOUT_SCALE_ENV = "DROVER_TEST_TIMEOUT_SCALE"


def timeout_scale() -> float:
    """Read the scale, defaulting to 1.0 for anything unset or unusable.

    Deliberately total: a malformed value must not turn every timing test in
    the suite into an error, because the failure would look like the flakiness
    this exists to remove.
    """
    raw = os.environ.get(TIMEOUT_SCALE_ENV)
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def scale_timeout(seconds: float) -> float:
    """Stretch a wait's ceiling to suit the machine it is running on."""
    return seconds * timeout_scale()
