"""Truthful local ledger for the supported snapshot + preflight workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drover.server.lake.cutover import Check, CutoverPlan
from drover.server.lake.release_workflow import (
    WorkflowError,
    build_release_workflow_ledger,
    read_release_workflow_ledger,
    snapshot_receipt_check,
    validate_release_workflow_ledger,
    write_release_workflow_ledger,
)


def receipt(*, completed_at: datetime | None = None, **changes):
    value = {
        "date": "2026-10-22",
        "host": "studio",
        "nas_path": "/approved/drover/studio/2026-10-22",
        "completed_at": (completed_at or datetime.now(timezone.utc)).isoformat(),
        "completed_epoch": 1.0,
        "duration_s": 10.0,
        "databases": 2,
        "nas_pg_restore_list": True,
        "verified_files": 4,
        "verified_bytes": 1024,
    }
    return value | changes


def preflight(*, passed: bool = True):
    return [
        Check("backup", passed, {"verified": passed}),
        Check("host_proxy", passed, {"hosts_status": 200}),
    ]


def test_successful_snapshot_and_preflight_stay_blocked_before_release_actions(
    tmp_path,
):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    snapshot = snapshot_receipt_check(receipt())

    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=snapshot, preflight=preflight()
    )

    validate_release_workflow_ledger(document)
    assert document["status"] == "blocked"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "succeeded"
    assert document["phases"]["nas_snapshot_publication"]["state"] == "succeeded"
    assert document["phases"]["release_preflight"]["state"] == "succeeded"
    assert document["phases"]["release_publication"]["state"] == "skipped"
    assert document["phases"]["rollout"]["state"] == "blocked"
    assert document["phases"]["acceptance"]["state"] == "blocked"
    assert document["phases"]["rollout"]["next_actions"] == [
        {"owner": "human_release_operator", "action": "approve_separate_rollout"}
    ]


def test_invalid_snapshot_blocks_preflight_without_claiming_it_ran(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    snapshot = snapshot_receipt_check(receipt(verification_ok=False))

    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=snapshot, preflight=None
    )

    assert document["status"] == "failed"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "failed"
    assert document["phases"]["nas_snapshot_publication"]["state"] == "blocked"
    assert document["phases"]["release_preflight"]["state"] == "blocked"
    assert document["phases"]["release_preflight"]["evidence"] == {
        "reason": "nas_snapshot_not_verified"
    }


def test_skipped_snapshot_is_explicit_and_cannot_bypass_preflight(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)

    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=None,
        preflight=None,
        snapshot_skip_reason="snapshot_not_requested",
    )

    assert document["status"] == "blocked"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "skipped"
    assert document["phases"]["release_preflight"]["state"] == "blocked"


def test_preflight_failure_is_preserved_with_its_failed_check_ids(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)

    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=snapshot_receipt_check(receipt()),
        preflight=preflight(passed=False),
    )

    assert document["status"] == "failed"
    phase = document["phases"]["release_preflight"]
    assert phase["state"] == "failed"
    assert phase["evidence"]["failed"] == ["backup", "host_proxy"]
    assert phase["next_actions"] == [
        {"owner": "release_operator", "action": "resolve_preflight_failures"}
    ]


def test_snapshot_receipt_rejects_stale_or_incomplete_evidence():
    stale = datetime.now(timezone.utc) - timedelta(hours=37)
    check = snapshot_receipt_check(receipt(completed_at=stale), max_age_hours=36)
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_receipt_stale"

    check = snapshot_receipt_check(receipt(nas_pg_restore_list=False))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_archive_readback_missing"

    check = snapshot_receipt_check(receipt(verification_ok=False))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_verification_failed"


def test_ledger_validator_rejects_a_false_success_shape(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=snapshot_receipt_check(receipt()),
        preflight=preflight(),
    )
    document["phases"]["rollout"]["state"] = "succeeded"

    with pytest.raises(WorkflowError, match="rollout cannot succeed"):
        validate_release_workflow_ledger(document)


def test_ledger_validator_requires_identity_and_evidence_for_success(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=snapshot_receipt_check(receipt()),
        preflight=preflight(),
    )

    document["lake"] = ""
    with pytest.raises(WorkflowError, match="workflow_ledger_identity_invalid"):
        validate_release_workflow_ledger(document)

    document["lake"] = "blue"
    document["phases"]["release_preflight"]["evidence"] = {
        "checks": [],
        "failed": [],
    }
    with pytest.raises(
        WorkflowError, match="workflow_ledger_preflight_evidence_invalid"
    ):
        validate_release_workflow_ledger(document)


def test_ledger_write_is_private_atomic_and_never_rewrites_history(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=snapshot_receipt_check(receipt()),
        preflight=preflight(),
    )
    path = tmp_path / "evidence" / "release-ledger.json"

    write_release_workflow_ledger(path, document)

    assert path.stat().st_mode & 0o777 == 0o600
    assert read_release_workflow_ledger(path) == document
    with pytest.raises(WorkflowError, match="workflow_ledger_exists"):
        write_release_workflow_ledger(path, document)


def test_ledger_write_sanitizes_filesystem_failures(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=snapshot_receipt_check(receipt()),
        preflight=preflight(),
    )
    parent = tmp_path / "not-a-directory"
    parent.write_text("blocker")

    with pytest.raises(WorkflowError, match="workflow_ledger_write_failed"):
        write_release_workflow_ledger(parent / "release-ledger.json", document)
