"""Deterministic identity for a harness event.

`harness_events` fenced on `event_id`, which is minted per insert. Two rows
describing the same event at the same sequence number with the same body are
the same event, whatever identifier each insert happened to carry -- so that
fence could not see a replay arriving under a fresh id, and every harnessd
restart re-inserted the entire history (drover#280).

`drover.dedup.make_dedup_key` already does this for `agent_events` in the
lakehouse. This is the same idea over the fields that identify a harness event.

The body is normalised the way the duplicate-event migration normalises it, and
for the same reason: `HarnessEvent.wire_payload` strips `aggregated_output` and
stamps the row's own `event_id`, so an original and its replay differ by those
two things and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

#: Stripped before hashing: `wire_payload` removes it on the way out, so a
#: replay carries a slimmer body than the original push did.
_VOLATILE_PAYLOAD_KEYS = ("aggregated_output",)

#: Stamped by whichever insert produced the row, so it cannot be part of an
#: identity meant to survive a re-delivery under a new one.
_IDENTITY_EXCLUDED_KEYS = ("event_id",)


def _strip(value: Any) -> Any:
    """Drop the keys that differ between an event and its replay."""
    if isinstance(value, Mapping):
        return {
            key: _strip(inner)
            for key, inner in sorted(value.items())
            if key not in _VOLATILE_PAYLOAD_KEYS and key not in _IDENTITY_EXCLUDED_KEYS
        }
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def harness_event_identity(
    *,
    session_id: Optional[str],
    seq: Optional[int],
    event_type: Optional[str],
    created_at: Optional[Any],
    payload: Optional[Mapping[str, Any]],
) -> str:
    """SHA-256 over the fields that make one harness event distinct.

    `seq` alone is not enough. Two genuinely different events can share a
    sequence number when a session's counter starts from zero twice, which on
    the live hub is 30 sessions at `seq = 1` (drover#277) -- `session.started`
    beside `terminal.output`. Type, timestamp and body keep those apart.
    """
    body = json.dumps(_strip(payload or {}), sort_keys=True, default=str)
    fingerprint = "|".join(
        [
            session_id or "",
            "" if seq is None else str(int(seq)),
            event_type or "",
            "" if created_at is None else str(created_at),
            body,
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
