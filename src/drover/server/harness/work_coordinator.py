"""Strict continuity opt-in for normal Work-coordinator structured launches.

The Work coordinator supplies this only after it has committed its execution
intent.  It reuses the durable observer ledger but never fabricates a Factory
run or grants authority to an unbound session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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


def parse_work_coordinator_continuity(
    body: Mapping[str, Any],
) -> tuple[WorkCoordinatorContinuity | None, dict[str, Any]]:
    """Remove and validate the private coordinator-only launch extension."""
    raw = body.get("work_coordinator_continuity")
    normalized = dict(body)
    if raw is None:
        return None, normalized
    normalized.pop("work_coordinator_continuity")
    if not isinstance(raw, Mapping) or set(raw) != {
        "run_id",
        "owner_id",
        "authority_scope",
        "objective",
    }:
        raise FactoryObserverRequestError("invalid work coordinator continuity binding")
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
    )
