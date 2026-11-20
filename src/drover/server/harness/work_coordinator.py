"""Strict continuity opt-in for normal Work-coordinator structured launches.

The Work coordinator supplies this only after it has committed its execution
intent.  It reuses the durable observer ledger but never fabricates a Factory
run or grants authority to an unbound session.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from drover.server.control_outbox import canonical_payload
from drover.server.harness.factory_observer import _RUN_ID, FactoryObserverRequestError


def work_coordinator_turn_id(run_id: str) -> str:
    """Return the immutable initial-turn id for one signed worker run."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("work continuity run_id is invalid")
    return str(uuid5(NAMESPACE_URL, "drover-work-coordinator-turn:" + run_id))


@dataclass(frozen=True)
class WorkCoordinatorContinuity:
    run_id: str
    owner_id: str
    authority_scope: str
    objective: str
    delivery_role: str = "standard"
    repository: str | None = None
    serialization_key: str | None = None
    # Opaque, signed reference to the single candidate effect the source daemon
    # must attest before a native owner can settle its wake.
    expected_effect_artifact: str | None = None
    # The hub signs the exact WorkService item that a coordinator worker may read
    # or update through its per-session stdio MCP bridge.
    goal_id: str | None = None
    item_number: int | None = None
    # Only the hub-signed work binding can grant this exact local candidate
    # permission to commit provenance for a separate verifier.
    committed_provenance: bool = False

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
    base_fields = {
        "run_id",
        "owner_id",
        "authority_scope",
        "objective",
        "binding_signature",
    }
    serialization_fields = {"delivery_role", "repository", "serialization_key"}
    expected_effect_fields = {"expected_effect_artifact"}
    work_item_fields = {"goal_id", "item_number"}
    committed_provenance_fields = {"committed_provenance"}
    if not isinstance(raw, Mapping) or not (
        base_fields
        <= set(raw)
        <= base_fields
        | serialization_fields
        | expected_effect_fields
        | work_item_fields
        | committed_provenance_fields
    ):
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
        or not (owner_id.startswith("agent:") or owner_id.startswith("drover:owner/"))
        or len(owner_id) > 191
    ):
        raise FactoryObserverRequestError(
            "work continuity owner_id must be a canonical agent or native owner key"
        )
    if scope not in {"implementation", "integration", "deployment"}:
        raise FactoryObserverRequestError("work continuity scope is invalid")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise FactoryObserverRequestError("work continuity objective is invalid")
    delivery_role = raw.get("delivery_role", "standard")
    repository = raw.get("repository")
    serialization_key = raw.get("serialization_key")
    expected_effect_artifact = raw.get("expected_effect_artifact")
    goal_id = raw.get("goal_id")
    item_number = raw.get("item_number")
    committed_provenance = raw.get("committed_provenance", False)
    if not isinstance(committed_provenance, bool):
        raise FactoryObserverRequestError(
            "work continuity committed provenance is invalid"
        )
    if committed_provenance and (goal_id is None or item_number is None):
        raise FactoryObserverRequestError(
            "work continuity committed provenance requires item binding"
        )
    if committed_provenance and scope != "implementation":
        raise FactoryObserverRequestError(
            "work continuity committed provenance requires implementation authority"
        )
    if committed_provenance and expected_effect_artifact is None:
        raise FactoryObserverRequestError(
            "work continuity committed provenance requires expected effect"
        )
    if (goal_id is None) != (item_number is None):
        raise FactoryObserverRequestError("work continuity item binding is invalid")
    if goal_id is not None and (
        not isinstance(goal_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}", goal_id)
        or isinstance(item_number, bool)
        or not isinstance(item_number, int)
        or item_number <= 0
    ):
        raise FactoryObserverRequestError("work continuity item binding is invalid")
    if expected_effect_artifact is not None and (
        not isinstance(expected_effect_artifact, str)
        or not expected_effect_artifact.strip()
        or len(expected_effect_artifact) > 2000
    ):
        raise FactoryObserverRequestError("work continuity expected effect is invalid")
    if delivery_role == "standard":
        if repository is not None or serialization_key is not None:
            raise FactoryObserverRequestError(
                "standard work continuity cannot serialize"
            )
    elif delivery_role == "integration_serialized":
        if scope != "implementation":
            raise FactoryObserverRequestError(
                "integration serialization requires implementation authority"
            )
        if (
            not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(serialization_key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}", serialization_key)
        ):
            raise FactoryObserverRequestError(
                "integration serialization binding is invalid"
            )
    else:
        raise FactoryObserverRequestError("work continuity delivery role is invalid")
    return (
        WorkCoordinatorContinuity(
            run_id,
            owner_id,
            scope,
            objective.strip(),
            delivery_role,
            repository,
            serialization_key,
            expected_effect_artifact,
            goal_id,
            item_number,
            committed_provenance,
        ),
        normalized,
        unsigned,
        signature,
    )
