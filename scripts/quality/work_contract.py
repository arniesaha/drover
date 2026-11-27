"""Typed client-facing contract for ``/work/update`` responses (#1024 Slice B).

Uses pydantic, the project's existing validation dependency, in strict mode
with unknown fields forbidden. It validates what ``work_queue.runtime.dispatch``
and the web handler actually emit: the status-to-error envelope and the
outcome semantics of each supported action. It is a test-side contract, not a
replacement for server validation. Contract: docs/operations/structural-quality.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Fixed strings emitted by runtime.dispatch and web/app.py for work routes.
ERRORS_BY_STATUS = {
    400: frozenset({"invalid work request", "JSON object required"}),
    401: frozenset({"authentication required"}),
    403: frozenset({"work operation forbidden"}),
    409: frozenset(
        {
            "work operation denied",
            "work operation paused; inspect provider or execution intent",
        }
    ),
    503: frozenset({"work control unavailable"}),
}
REASON_REQUIRED = frozenset({403, 409})
ACTIONS = ("complete", "verified", "failed", "progress")


class ContractViolation(ValueError):
    pass


class WorkError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    error: str
    reason: str | None = Field(default=None, min_length=1, max_length=200)


class UpdateOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    outcome: Literal["verified", "failed", "progress", "deferred", "marker_only"]


class VerificationLink(BaseModel):
    """``complete`` launches a separate verifier and returns its link row."""

    model_config = ConfigDict(extra="allow", strict=True, frozen=True)
    run_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_role: Literal["verify"]
    goal_id: str
    item_number: int
    session_id: str | None


def validate_update_response(status: object, body: object, *, action: object):
    """Return the parsed model or raise ContractViolation with the reason."""
    if action not in ACTIONS:
        raise ContractViolation(f"unsupported action {action!r}")
    if isinstance(status, bool) or not isinstance(status, int):
        raise ContractViolation("status must be an int")
    try:
        if status == 200:
            if action == "complete" and isinstance(body, dict) and "run_role" in body:
                return VerificationLink.model_validate(body)
            result = UpdateOutcome.model_validate(body)
            allowed = {action, "deferred"}
            if action == "complete":
                allowed = {"deferred", "marker_only"}
            if result.outcome not in allowed:
                raise ContractViolation(
                    f"outcome {result.outcome!r} is not valid for action {action!r}"
                )
            return result
        if status not in ERRORS_BY_STATUS:
            raise ContractViolation(f"status {status} is outside the work contract")
        error = WorkError.model_validate(body)
    except ValidationError as exc:
        raise ContractViolation(f"shape: {exc.errors()[0]['msg']}") from exc
    if error.error not in ERRORS_BY_STATUS[status]:
        raise ContractViolation(f"error {error.error!r} is not a {status} error")
    if (error.reason is not None) != (status in REASON_REQUIRED):
        raise ContractViolation(f"reason presence is wrong for status {status}")
    return error
