"""Outbound push delivery for the drover-server API.

Only APNs today. The package exposes a module-level dispatch hook rather than
constructor injection because ``HarnessRegistry`` is rebuilt per request from
a bare path (``app.py``'s ``_harness_registry()``), so there is no long-lived
object to thread a sender through.
"""

from drover.server.push.apns import (
    APNsConfig,
    APNsSender,
    AwaitingTransition,
    configure,
    dispatch_awaiting_transition,
    set_sender,
)

__all__ = [
    "APNsConfig",
    "APNsSender",
    "AwaitingTransition",
    "configure",
    "dispatch_awaiting_transition",
    "set_sender",
]
