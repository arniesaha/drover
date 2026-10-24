"""Strict continuity opt-in for normal Work-coordinator structured launches.

The Work coordinator supplies this only after it has committed its execution
intent.  It reuses the durable observer ledger but never fabricates a Factory
run or grants authority to an unbound session.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Mapping

from drover.server.control_outbox import canonical_payload
from drover.server.harness.factory_observer import _RUN_ID, FactoryObserverRequestError


@dataclass(frozen=True)
class WorkCoordinatorContinuity:
    run_id: str
    owner_id: str
    authority_scope: str
    objective: str

    @property
    def source_session_id(self) -> str:
        return f"work/{self.run_id}@0"


def sign_work_coordinator_continuity(binding: Mapping[str, Any], secret: str) -> str:
    """Authenticate the hub-issued immutable launch binding for one host."""
    if not isinstance(secret, str) or not secret:
        raise FactoryObserverRequestError("hub continuity authentication is required")
    return hmac.new(
        secret.encode(), canonical_payload(dict(binding)).encode(), hashlib.sha256
    ).hexdigest()


def parse_work_coordinator_continuity(
    body: Mapping[str, Any], *, hub_secret: str | None = None
) -> tuple[WorkCoordinatorContinuity | None, dict[str, Any]]:
    """Remove and validate the hub-only coordinator launch extension."""
    continuity, normalized, unsigned, signature = _parse_work_coordinator_continuity(
        body
    )
    if continuity is None:
        return None, normalized
    if (
        signature is None
        or hub_secret is None
        or not hmac.compare_digest(
            signature, sign_work_coordinator_continuity(unsigned, hub_secret)
        )
    ):
        raise FactoryObserverRequestError(
            "work continuity binding is not hub authenticated"
        )
    return continuity, normalized


def parse_unverified_work_coordinator_continuity(
    body: Mapping[str, Any],
) -> tuple[WorkCoordinatorContinuity | None, dict[str, Any]]:
    """Parse a hub-forwarded binding without exposing its HMAC secret to a host.

    Harnessd authenticates the request with its bearer token. The hub remains
    the only component that verifies the continuity HMAC before recording the
    authoritative ledger entry.
    """
    continuity, normalized, _, _ = _parse_work_coordinator_continuity(body)
    return continuity, normalized


def _parse_work_coordinator_continuity(
    body: Mapping[str, Any],
) -> tuple[
    WorkCoordinatorContinuity | None, dict[str, Any], dict[str, Any], str | None
]:
    raw = body.get("work_coordinator_continuity")
    normalized = dict(body)
    if raw is None:
        return None, normalized, {}, None
    normalized.pop("work_coordinator_continuity")
    if not isinstance(raw, Mapping) or set(raw) != {
        "run_id",
        "owner_id",
        "authority_scope",
        "objective",
        "binding_signature",
    }:
        raise FactoryObserverRequestError("invalid work coordinator continuity binding")
    signature = raw["binding_signature"]
    unsigned = {key: value for key, value in raw.items() if key != "binding_signature"}
    if not isinstance(signature, str) or not signature:
        raise FactoryObserverRequestError("work continuity binding is invalid")
    run_id, owner_id, scope, objective = (
        raw["run_id"],
        raw["owner_id"],
        raw["authority_scope"],
        raw["objective"],
    )
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise FactoryObserverRequestError("work continuity run_id is invalid")
    if (
        not isinstance(owner_id, str)
        or not owner_id.startswith("agent:")
        or len(owner_id) > 191
    ):
        raise FactoryObserverRequestError(
            "work continuity owner_id must be a canonical agent key"
        )
    if scope not in {"implementation", "integration"}:
        raise FactoryObserverRequestError("work continuity scope is invalid")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise FactoryObserverRequestError("work continuity objective is invalid")
    return (
        WorkCoordinatorContinuity(run_id, owner_id, scope, objective.strip()),
        normalized,
        unsigned,
        signature,
    )
