"""Truthful, local evidence ledger for snapshot and release preflight.

This is deliberately an adapter, not a release mechanism.  It reads the
receipt published by ``scripts/backup/studio_backup.py`` and the checks
produced by ``run_release_preflight``.  It never contacts the NAS, publishes a
release, changes a runtime, or starts a rollout.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cutover import Check, CutoverPlan


class WorkflowError(ValueError):
    """The workflow ledger or one of its evidence documents is invalid."""


_PHASES = (
    "nas_snapshot_execution",
    "nas_snapshot_publication",
    "release_preflight",
    "release_publication",
    "rollout",
    "acceptance",
)
_STATES = {"succeeded", "failed", "skipped", "blocked"}


def _action(owner: str, action: str) -> dict[str, str]:
    return {"owner": owner, "action": action}


def _phase(
    state: str, evidence: dict[str, Any], *next_actions: dict[str, str]
) -> dict[str, Any]:
    return {
        "state": state,
        "evidence": evidence,
        "next_actions": list(next_actions),
    }


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise WorkflowError("snapshot_receipt_completed_at_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowError("snapshot_receipt_completed_at_invalid") from exc
    if parsed.tzinfo is None:
        raise WorkflowError("snapshot_receipt_completed_at_invalid")
    return parsed.astimezone(timezone.utc)


def snapshot_receipt_check(
    receipt: object,
    *,
    max_age_hours: float = 36,
    now: datetime | None = None,
) -> Check:
    """Validate the existing NAS success receipt without touching the NAS.

    The NAS helper writes ``SUCCESS.json`` only after verification and Studio
    archive readback; the Studio job writes this receipt locally only after that
    publish succeeds.  This check records what the receipt attests, rather than
    claiming a new snapshot has been run.
    """
    if max_age_hours <= 0:
        raise WorkflowError("snapshot_max_age_invalid")
    if not isinstance(receipt, dict):
        return Check("nas_snapshot", False, {"reason": "snapshot_receipt_invalid"})
    try:
        completed = _timestamp(receipt.get("completed_at"))
    except WorkflowError as exc:
        return Check("nas_snapshot", False, {"reason": str(exc)})
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (clock - completed).total_seconds()
    evidence = {
        "date": receipt.get("date"),
        "completed_at": completed.isoformat(),
        "age_seconds": round(age_seconds),
        "verified_files": receipt.get("verified_files"),
        "verified_bytes": receipt.get("verified_bytes"),
        "databases": receipt.get("databases"),
    }
    if age_seconds < 0 or age_seconds > max_age_hours * 3600:
        return Check(
            "nas_snapshot", False, evidence | {"reason": "snapshot_receipt_stale"}
        )
    required = {
        "date": receipt.get("date"),
        "host": receipt.get("host"),
        "nas_path": receipt.get("nas_path"),
    }
    if not all(isinstance(value, str) and value for value in required.values()):
        return Check(
            "nas_snapshot", False, evidence | {"reason": "snapshot_receipt_incomplete"}
        )
    # ``verify_generation`` consumes this flag before writing
    # ``last-success.json``. Its absence is therefore the current producer's
    # successful-receipt shape, not missing verification evidence.
    if receipt.get("verification_ok") is False:
        return Check(
            "nas_snapshot", False, evidence | {"reason": "snapshot_verification_failed"}
        )
    if receipt.get("verification_ok") not in (None, True):
        return Check(
            "nas_snapshot", False, evidence | {"reason": "snapshot_receipt_invalid"}
        )
    if receipt.get("nas_pg_restore_list") is not True:
        return Check(
            "nas_snapshot",
            False,
            evidence | {"reason": "snapshot_archive_readback_missing"},
        )
    if not all(
        isinstance(receipt.get(name), int) and receipt[name] >= minimum
        for name, minimum in (
            ("verified_files", 1),
            ("verified_bytes", 1),
            ("databases", 1),
        )
    ):
        return Check(
            "nas_snapshot", False, evidence | {"reason": "snapshot_receipt_incomplete"}
        )
    return Check("nas_snapshot", True, evidence)


def _preflight_phase(checks: Iterable[Check] | None) -> dict[str, Any]:
    if checks is None:
        return _phase(
            "blocked",
            {"reason": "nas_snapshot_not_verified"},
            _action("backup_operator", "run_or_repair_nas_snapshot"),
        )
    values = list(checks)
    failures = [check.id for check in values if not check.passed]
    evidence = {
        "checks": [check.as_dict() for check in values],
        "failed": failures,
    }
    if not values:
        return _phase(
            "failed",
            evidence | {"reason": "release_preflight_empty"},
            _action("release_operator", "rerun_release_preflight"),
        )
    if failures:
        return _phase(
            "failed",
            evidence,
            _action("release_operator", "resolve_preflight_failures"),
        )
    return _phase(
        "succeeded",
        evidence,
        _action("release_operator", "record_release_publication"),
    )


def build_release_workflow_ledger(
    plan: CutoverPlan,
    *,
    release: str,
    snapshot: Check | None,
    preflight: Iterable[Check] | None,
    snapshot_skip_reason: str | None = None,
) -> dict[str, Any]:
    """Make one machine-verifiable ledger with no implied rollout success."""
    if not isinstance(release, str) or not release:
        raise WorkflowError("release_invalid")
    if snapshot is None and not snapshot_skip_reason:
        raise WorkflowError("snapshot_or_skip_reason_required")
    if snapshot is not None and snapshot.id != "nas_snapshot":
        raise WorkflowError("snapshot_check_invalid")

    if snapshot is None:
        snapshot_execution = _phase(
            "skipped",
            {"reason": snapshot_skip_reason},
            _action("backup_operator", "run_or_record_nas_snapshot"),
        )
        snapshot_publication = _phase(
            "blocked",
            {"reason": "nas_snapshot_not_executed"},
            _action("backup_operator", "publish_verified_nas_snapshot"),
        )
        preflight_phase = _preflight_phase(None)
    elif not snapshot.passed:
        snapshot_execution = _phase(
            "failed",
            snapshot.evidence,
            _action("backup_operator", "repair_nas_snapshot_evidence"),
        )
        snapshot_publication = _phase(
            "blocked",
            {"reason": "nas_snapshot_not_verified"},
            _action("backup_operator", "publish_verified_nas_snapshot"),
        )
        preflight_phase = _preflight_phase(None)
    else:
        snapshot_execution = _phase("succeeded", snapshot.evidence)
        snapshot_publication = _phase(
            "succeeded",
            {
                "attested_by": "studio_backup_success_receipt",
                "verification_ok": True,
                "nas_pg_restore_list": True,
            },
        )
        preflight_phase = _preflight_phase(preflight)

    phases = {
        "nas_snapshot_execution": snapshot_execution,
        "nas_snapshot_publication": snapshot_publication,
        "release_preflight": preflight_phase,
        "release_publication": _phase(
            "skipped",
            {"reason": "not_executed_or_verified_by_this_workflow"},
            _action("release_operator", "record_release_publication"),
        ),
        "rollout": _phase(
            "blocked",
            {"reason": "requires_explicit_human_approval_and_separate_command"},
            _action("human_release_operator", "approve_separate_rollout"),
        ),
        "acceptance": _phase(
            "blocked",
            {"reason": "rollout_not_executed"},
            _action("acceptance_owner", "record_post_rollout_acceptance"),
        ),
    }
    failed = any(phases[name]["state"] == "failed" for name in _PHASES)
    document = {
        "schema_version": 1,
        "kind": "nas_snapshot_release_preflight",
        "status": "failed" if failed else "blocked",
        "release": release,
        "lake": plan.name,
        "phases": phases,
    }
    validate_release_workflow_ledger(document)
    return document


def validate_release_workflow_ledger(document: object) -> None:
    """Reject malformed documents and claims this workflow cannot establish."""
    if not isinstance(document, dict):
        raise WorkflowError("workflow_ledger_invalid")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "nas_snapshot_release_preflight"
    ):
        raise WorkflowError("workflow_ledger_version_invalid")
    if not all(
        isinstance(document.get(name), str) and document[name]
        for name in ("release", "lake")
    ):
        raise WorkflowError("workflow_ledger_identity_invalid")
    phases = document.get("phases")
    if not isinstance(phases, dict) or set(phases) != set(_PHASES):
        raise WorkflowError("workflow_ledger_phases_invalid")
    for name in _PHASES:
        phase = phases[name]
        if not isinstance(phase, dict) or phase.get("state") not in _STATES:
            raise WorkflowError("workflow_ledger_phase_invalid")
        if not isinstance(phase.get("evidence"), dict) or not isinstance(
            phase.get("next_actions"), list
        ):
            raise WorkflowError("workflow_ledger_phase_invalid")
        if not all(
            isinstance(action, dict)
            and isinstance(action.get("owner"), str)
            and isinstance(action.get("action"), str)
            for action in phase["next_actions"]
        ):
            raise WorkflowError("workflow_ledger_actions_invalid")
        if phase["state"] in {"blocked", "skipped"} and not isinstance(
            phase["evidence"].get("reason"), str
        ):
            raise WorkflowError("workflow_ledger_reason_invalid")
        if phase["state"] == "failed" and not (
            isinstance(phase["evidence"].get("reason"), str)
            or isinstance(phase["evidence"].get("failed"), list)
        ):
            raise WorkflowError("workflow_ledger_failure_evidence_invalid")
    snapshot_evidence = phases["nas_snapshot_execution"]["evidence"]
    if phases["nas_snapshot_execution"]["state"] == "succeeded" and not (
        isinstance(snapshot_evidence.get("date"), str)
        and isinstance(snapshot_evidence.get("completed_at"), str)
        and all(
            isinstance(snapshot_evidence.get(name), int)
            and snapshot_evidence[name] >= 1
            for name in ("verified_files", "verified_bytes", "databases")
        )
    ):
        raise WorkflowError("workflow_ledger_snapshot_evidence_invalid")
    publication_evidence = phases["nas_snapshot_publication"]["evidence"]
    if phases["nas_snapshot_publication"]["state"] == "succeeded" and (
        publication_evidence.get("attested_by") != "studio_backup_success_receipt"
        or publication_evidence.get("verification_ok") is not True
        or publication_evidence.get("nas_pg_restore_list") is not True
    ):
        raise WorkflowError("workflow_ledger_snapshot_publication_invalid")
    preflight_evidence = phases["release_preflight"]["evidence"]
    if phases["release_preflight"]["state"] == "succeeded":
        checks = preflight_evidence.get("checks")
        if not (
            isinstance(checks, list)
            and checks
            and preflight_evidence.get("failed") == []
            and all(
                isinstance(check, dict)
                and isinstance(check.get("id"), str)
                and check["id"]
                and check.get("passed") is True
                and isinstance(check.get("evidence"), dict)
                for check in checks
            )
        ):
            raise WorkflowError("workflow_ledger_preflight_evidence_invalid")
    if (
        phases["nas_snapshot_publication"]["state"] == "succeeded"
        and phases["nas_snapshot_execution"]["state"] != "succeeded"
    ):
        raise WorkflowError("snapshot publication requires snapshot execution")
    if (
        phases["release_preflight"]["state"] == "succeeded"
        and phases["nas_snapshot_publication"]["state"] != "succeeded"
    ):
        raise WorkflowError("preflight requires published NAS snapshot")
    if phases["release_publication"]["state"] == "succeeded":
        raise WorkflowError("release publication cannot succeed in this workflow")
    if phases["rollout"]["state"] == "succeeded":
        raise WorkflowError("rollout cannot succeed in this workflow")
    if phases["acceptance"]["state"] == "succeeded":
        raise WorkflowError("acceptance cannot succeed in this workflow")
    failed = any(phases[name]["state"] == "failed" for name in _PHASES)
    if document.get("status") != ("failed" if failed else "blocked"):
        raise WorkflowError("workflow_ledger_status_invalid")


def read_release_workflow_ledger(path: Path) -> dict[str, Any]:
    """Read and validate a previously created ledger without repairing it."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("workflow_ledger_unreadable") from exc
    validate_release_workflow_ledger(document)
    return document


def write_release_workflow_ledger(path: Path, document: dict[str, Any]) -> None:
    """Atomically create a private immutable ledger document.

    Rewriting history would let a later invocation turn a failed or skipped
    phase into a claimed success.  A new run must use a new evidence pathname.
    """
    validate_release_workflow_ledger(document)
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            raise WorkflowError("workflow_ledger_exists")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise WorkflowError("workflow_ledger_exists") from exc
    except OSError as exc:
        raise WorkflowError("workflow_ledger_write_failed") from exc
    finally:
        if "temporary" in locals():
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
