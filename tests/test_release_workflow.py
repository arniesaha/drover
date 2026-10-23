"""Truthful local ledger for the supported snapshot + preflight workflow."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from drover.server.lake.cutover import Check, CutoverPlan
from drover.server.lake.release_workflow import (
    WorkflowError,
    build_release_workflow_ledger,
    read_release_workflow_ledger,
    sign_snapshot_receipt,
    snapshot_receipt_check,
    validate_release_workflow_ledger,
    write_release_workflow_ledger,
)

RECEIPT_KEY = "test-receipt-key"


def receipt(*, completed_at: datetime | None = None, **changes):
    value = {
        "date": "2026-10-22",
        "host": "studio",
        "nas_path": "/approved/drover/studio/2026-10-22",
        "completed_at": (completed_at or datetime.now(timezone.utc)).isoformat(),
        "completed_epoch": 1.0,
        "duration_s": 10.0,
        "databases": 2,
        "verification_ok": True,
        "nas_pg_restore_list": True,
        "verified_files": 4,
        "verified_bytes": 1024,
        "artifact_manifest_sha256": "a" * 64,
        "local_backup_manifest_sha256": "a" * 64,
        "producer": {"name": "studio_backup", "schema_version": 2},
    }
    return sign_snapshot_receipt(value | changes, RECEIPT_KEY)


def checked(value, **kwargs):
    return snapshot_receipt_check(
        value,
        receipt_key=RECEIPT_KEY,
        expected_manifest_sha256="a" * 64,
        **kwargs,
    )


def preflight(*, passed: bool = True):
    return [
        Check("backup", passed, {"verified": passed}),
        Check("host_proxy", passed, {"hosts_status": 200}),
    ]


def test_successful_snapshot_and_preflight_stay_blocked_before_release_actions(
    tmp_path,
):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    snapshot = checked(receipt())

    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=snapshot, preflight=preflight()
    )

    validate_release_workflow_ledger(document)
    assert document["status"] == "blocked"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "succeeded"
    assert document["phases"]["nas_snapshot_publication"]["state"] == "succeeded"
    assert document["phases"]["nas_snapshot_publication"]["evidence"] == {
        "attested_by": "studio_backup_success_receipt",
        "verification_ok": True,
        "nas_pg_restore_list": True,
        "date": "2026-10-22",
        "host": "studio",
        "nas_path": "/approved/drover/studio/2026-10-22",
        "artifact_manifest_sha256": "a" * 64,
        "local_backup_manifest_sha256": "a" * 64,
        "producer": document["phases"]["nas_snapshot_execution"]["evidence"][
            "producer"
        ],
    }
    assert document["phases"]["release_preflight"]["state"] == "succeeded"
    assert document["phases"]["release_publication"]["state"] == "skipped"
    assert document["phases"]["rollout"]["state"] == "blocked"
    assert document["phases"]["acceptance"]["state"] == "blocked"
    assert document["phases"]["rollout"]["next_actions"] == [
        {"owner": "human_release_operator", "action": "approve_separate_rollout"}
    ]


def test_invalid_snapshot_blocks_preflight_without_claiming_it_ran(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    snapshot = checked(receipt(verification_ok=False))

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
        snapshot=checked(receipt()),
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
    check = checked(receipt(completed_at=stale), max_age_hours=36)
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_receipt_stale"

    check = checked(receipt(nas_pg_restore_list=False))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_archive_readback_missing"

    check = checked(receipt(verification_ok=False))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_verification_failed"

    check = checked(receipt(verification_ok=0))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_verification_failed"

    missing_success = receipt()
    missing_success.pop("verification_ok")
    check = checked(sign_snapshot_receipt(missing_success, RECEIPT_KEY))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_verification_failed"

    unsigned = receipt()
    unsigned.pop("producer")
    check = checked(unsigned)
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_provenance_missing"

    forged = receipt()
    forged["host"] = "other-studio"
    check = checked(forged)
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_provenance_invalid"

    check = snapshot_receipt_check(
        receipt(), receipt_key=RECEIPT_KEY, expected_manifest_sha256="b" * 64
    )
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_manifest_mismatch"

    check = checked(receipt(local_backup_manifest_sha256="b" * 64))
    assert not check.passed
    assert check.evidence["reason"] == "snapshot_local_manifest_mismatch"


@pytest.mark.parametrize("malformed", ["g" * 64, "é" * 64])
def test_malformed_expected_manifest_creates_failed_blocked_ledger(tmp_path, malformed):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    check = snapshot_receipt_check(
        receipt(), receipt_key=RECEIPT_KEY, expected_manifest_sha256=malformed
    )

    assert not check.passed
    assert check.evidence["reason"] == "snapshot_manifest_mismatch"
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=check, preflight=None
    )
    assert document["status"] == "failed"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "failed"
    assert document["phases"]["nas_snapshot_publication"]["state"] == "blocked"
    assert document["phases"]["release_preflight"]["state"] == "blocked"
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    ledger = evidence_root / "malformed-manifest.json"
    write_release_workflow_ledger(ledger, document, evidence_root=evidence_root)
    assert read_release_workflow_ledger(ledger) == document


@pytest.mark.parametrize("malformed", ["g" * 64, "é" * 64])
def test_malformed_producer_signature_creates_failed_blocked_ledger(
    tmp_path, malformed
):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    value = receipt()
    value["producer"]["signature"] = malformed
    check = checked(value)

    assert not check.passed
    assert check.evidence["reason"] == "snapshot_provenance_invalid"
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=check, preflight=None
    )
    assert document["status"] == "failed"
    assert document["phases"]["nas_snapshot_execution"]["state"] == "failed"
    assert document["phases"]["nas_snapshot_publication"]["state"] == "blocked"
    assert document["phases"]["release_preflight"]["state"] == "blocked"
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    ledger = evidence_root / "malformed-provenance.json"
    write_release_workflow_ledger(ledger, document, evidence_root=evidence_root)
    assert read_release_workflow_ledger(ledger) == document


def test_ledger_validator_rejects_a_false_success_shape(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=checked(receipt()),
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
        snapshot=checked(receipt()),
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


def test_ledger_validator_rejects_publication_with_mismatched_receipt_correlation(
    tmp_path,
):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=checked(receipt()),
        preflight=preflight(),
    )
    document["phases"]["nas_snapshot_publication"]["evidence"][
        "nas_path"
    ] = "/forged/generation"

    with pytest.raises(
        WorkflowError, match="workflow_ledger_snapshot_publication_invalid"
    ):
        validate_release_workflow_ledger(document)


def test_ledger_write_is_private_atomic_and_never_rewrites_history(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=checked(receipt()),
        preflight=preflight(),
    )
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    path = root / "release-ledger.json"

    write_release_workflow_ledger(path, document, evidence_root=root)

    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(root.glob(".release-ledger.json.*.tmp"))
    assert read_release_workflow_ledger(path) == document
    with pytest.raises(WorkflowError, match="workflow_ledger_exists"):
        write_release_workflow_ledger(path, document, evidence_root=root)


def test_ledger_write_sanitizes_filesystem_failures(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan,
        release="0.6.28",
        snapshot=checked(receipt()),
        preflight=preflight(),
    )
    parent = tmp_path / "not-a-directory"
    parent.write_text("blocker")

    with pytest.raises(WorkflowError, match="workflow_ledger_path_unsafe"):
        write_release_workflow_ledger(
            parent / "release-ledger.json", document, evidence_root=parent
        )


def test_ledger_write_rejects_symlink_or_writable_evidence_root(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=checked(receipt()), preflight=preflight()
    )
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(safe, target_is_directory=True)
    with pytest.raises(WorkflowError, match="workflow_ledger_path_unsafe"):
        write_release_workflow_ledger(
            linked / "ledger.json", document, evidence_root=linked
        )
    safe.chmod(0o777)
    with pytest.raises(WorkflowError, match="workflow_ledger_path_unsafe"):
        write_release_workflow_ledger(
            safe / "ledger.json", document, evidence_root=safe
        )


def test_ledger_write_rejects_symlinked_parent_traversal(tmp_path):
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=checked(receipt()), preflight=preflight()
    )
    backing = tmp_path / "backing"
    backing.mkdir(mode=0o700)
    root = backing / "evidence"
    root.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(backing, target_is_directory=True)

    with pytest.raises(WorkflowError, match="workflow_ledger_path_unsafe"):
        write_release_workflow_ledger(
            linked_parent / "evidence" / "ledger.json",
            document,
            evidence_root=linked_parent / "evidence",
        )


def test_ledger_write_fails_closed_when_ancestor_is_swapped_during_creation(
    tmp_path, monkeypatch
):
    import drover.server.lake.release_workflow as workflow

    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=checked(receipt()), preflight=preflight()
    )
    parent = tmp_path / "parent"
    evidence_root = parent / "evidence"
    evidence_root.mkdir(parents=True, mode=0o700)
    attacker = tmp_path / "attacker"
    (attacker / "evidence").mkdir(parents=True, mode=0o700)
    displaced = tmp_path / "displaced-parent"
    real_open = workflow.os.open
    swapped = False

    def swap_ancestor(*args, **kwargs):
        nonlocal swapped
        descriptor = real_open(*args, **kwargs)
        if not swapped and args[0] == "evidence" and kwargs.get("dir_fd") is not None:
            parent.rename(displaced)
            parent.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(workflow.os, "open", swap_ancestor)

    with pytest.raises(WorkflowError, match="workflow_ledger_path_unsafe"):
        write_release_workflow_ledger(
            evidence_root / "ledger.json", document, evidence_root=evidence_root
        )

    assert not (attacker / "evidence" / "ledger.json").exists()
    assert not (displaced / "evidence" / "ledger.json").exists()


def test_ledger_write_fsyncs_the_file_and_evidence_directory(tmp_path, monkeypatch):
    import drover.server.lake.release_workflow as workflow

    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    document = build_release_workflow_ledger(
        plan, release="0.6.28", snapshot=checked(receipt()), preflight=preflight()
    )
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    modes = []
    real_fsync = workflow.os.fsync

    def record_fsync(descriptor):
        modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(workflow.os, "fsync", record_fsync)
    write_release_workflow_ledger(root / "ledger.json", document, evidence_root=root)

    assert len(modes) == 2
    assert stat.S_ISREG(modes[0])
    assert stat.S_ISDIR(modes[1])
