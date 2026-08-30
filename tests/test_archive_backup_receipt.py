"""Exact private receipts and linear-chain validation for R2 backups."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from drover.server.archive import backup_receipt as receipt_module
from drover.server.archive.backup_receipt import (
    BackupReceipt,
    CollisionCounts,
    backup_receipt_summary,
    latest_backup_receipt,
    load_backup_receipt,
    load_backup_receipt_chain,
    write_backup_receipt,
)
from drover.server.archive.inventory import (
    canonical_private_json_bytes,
    private_json_sha256,
)

GENERATION_ID = "33333333-3333-4333-8333-333333333333"
SECOND_GENERATION_ID = "44444444-4444-4444-8444-444444444444"
THIRD_GENERATION_ID = "55555555-5555-4555-8555-555555555555"
STORE_SCOPE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_STORE_SCOPE_ID = "22222222-2222-4222-8222-222222222222"
EXPECTED_RECEIPT_FIELDS = {
    "kind",
    "schema_version",
    "created_at",
    "pond_version",
    "store_scope_id",
    "generation_id",
    "previous_receipt_sha256",
    "source_inventory_sha256",
    "local_pond_inventory_sha256",
    "remote_pond_inventory_sha256",
    "coverage_report_sha256",
    "sessions",
    "messages",
    "parts",
    "source_not_archive_eligible",
    "collision_counts",
    "copy_duration_ms",
    "verify_duration_ms",
    "health_samples",
    "health_p95_ms",
    "peak_rss_bytes",
    "peak_physical_bytes",
    "swap_delta_bytes",
    "result",
}
EXPECTED_COLLISION_FIELDS = {
    "duplicate_source_groups",
    "cross_harness_native_id_groups",
    "archive_logical_duplicate_candidate_groups",
    "archive_signature_unverifiable",
}
EXPECTED_SUMMARY_FIELDS = {
    "schema_version",
    "pond_version",
    "sessions",
    "messages",
    "parts",
    "source_not_archive_eligible",
    "collision_counts",
    "copy_duration_ms",
    "verify_duration_ms",
    "health_samples",
    "health_p95_ms",
    "peak_rss_bytes",
    "peak_physical_bytes",
    "swap_delta_bytes",
    "result",
}


def _receipt_directory(tmp_path: Path) -> Path:
    path = tmp_path / "receipts"
    path.mkdir(mode=0o700)
    return path


def _verified_receipt(
    *,
    generation_id: str = GENERATION_ID,
    store_scope_id: str = STORE_SCOPE_ID,
    previous_receipt_sha256: str | None = None,
) -> BackupReceipt:
    return BackupReceipt(
        schema_version=1,
        created_at="2026-08-29T12:34:56Z",
        pond_version="0.16.3",
        store_scope_id=store_scope_id,
        generation_id=generation_id,
        previous_receipt_sha256=previous_receipt_sha256,
        source_inventory_sha256="a" * 64,
        local_pond_inventory_sha256="b" * 64,
        remote_pond_inventory_sha256="c" * 64,
        coverage_report_sha256="d" * 64,
        sessions=7,
        messages=11,
        parts=13,
        source_not_archive_eligible=2,
        collision_counts=CollisionCounts(0, 0, 0, 0),
        copy_duration_ms=1200,
        verify_duration_ms=300,
        health_samples=31,
        health_p95_ms=12.5,
        peak_rss_bytes=1024,
        peak_physical_bytes=2048,
        swap_delta_bytes=0,
    )


def _write_payload(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def test_receipt_round_trip_preserves_the_exact_v1_contract(tmp_path):
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()

    path = write_backup_receipt(directory, receipt)

    assert path == directory / f"backup-{GENERATION_ID}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_backup_receipt(path) == receipt
    assert set(receipt.to_wire()) == EXPECTED_RECEIPT_FIELDS
    assert set(receipt.to_wire()["collision_counts"]) == EXPECTED_COLLISION_FIELDS
    assert (
        private_json_sha256(receipt.to_wire())
        == hashlib.sha256(canonical_private_json_bytes(receipt.to_wire())).hexdigest()
    )


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (_verified_receipt, "schema_version"),
        (lambda: CollisionCounts(0, 0, 0, 0), "duplicate_source_groups"),
    ],
)
def test_receipt_values_are_frozen_and_slotted(factory, field):
    value = factory()

    assert not hasattr(value, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, 2)


def test_canonical_private_json_has_one_stable_compact_encoding():
    first = {"z": [3, 2, 1], "a": {"value": True}}
    second = {"a": {"value": True}, "z": [3, 2, 1]}
    expected = b'{"a":{"value":true},"z":[3,2,1]}\n'

    assert canonical_private_json_bytes(first) == expected
    assert canonical_private_json_bytes(second) == expected
    assert private_json_sha256(first) == hashlib.sha256(expected).hexdigest()


def test_canonical_private_json_rejects_non_json_numbers():
    with pytest.raises(ValueError, match="output"):
        canonical_private_json_bytes({"health_p95_ms": float("nan")})


def test_receipt_summary_is_exact_and_never_exposes_private_identity():
    receipt = _verified_receipt(previous_receipt_sha256="e" * 64)

    summary = backup_receipt_summary(receipt)
    encoded = json.dumps(summary, sort_keys=True)

    assert set(summary) == EXPECTED_SUMMARY_FIELDS
    assert summary["collision_counts"] == {
        "duplicate_source_groups": 0,
        "cross_harness_native_id_groups": 0,
        "archive_logical_duplicate_candidate_groups": 0,
        "archive_signature_unverifiable": 0,
    }
    for private_value in (
        receipt.created_at,
        receipt.store_scope_id,
        receipt.generation_id,
        receipt.previous_receipt_sha256,
        receipt.source_inventory_sha256,
        receipt.local_pond_inventory_sha256,
        receipt.remote_pond_inventory_sha256,
        receipt.coverage_report_sha256,
    ):
        assert private_value not in encoded


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("kind", "private-secret-kind"),
        ("schema_version", 2),
        ("created_at", "private-secret-time"),
        ("pond_version", "private-secret-version"),
        ("store_scope_id", "private-secret-scope"),
        ("generation_id", "private-secret-generation"),
        ("previous_receipt_sha256", "private-secret-previous"),
        ("source_inventory_sha256", "private-secret-source"),
        ("sessions", -1),
        ("health_p95_ms", 1),
        ("health_p95_ms", float("nan")),
        ("result", "private-secret-result"),
    ],
)
def test_receipt_loader_rejects_invalid_values_with_one_private_error(
    tmp_path, field, invalid
):
    payload = _verified_receipt().to_wire()
    payload[field] = invalid
    path = tmp_path / "receipt.json"
    _write_payload(path, payload)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$") as raised:
        load_backup_receipt(path)

    assert "private-secret" not in str(raised.value)


@pytest.mark.parametrize(
    "change", ["remove", "add", "collision_add", "collision_nonzero"]
)
def test_receipt_loader_requires_exact_fields_and_zero_collisions(tmp_path, change):
    payload = _verified_receipt().to_wire()
    if change == "remove":
        del payload["parts"]
    elif change == "add":
        payload["private-secret"] = True
    elif change == "collision_add":
        payload["collision_counts"]["private-secret"] = 0
    else:
        payload["collision_counts"]["duplicate_source_groups"] = 1
    path = tmp_path / "receipt.json"
    _write_payload(path, payload)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$") as raised:
        load_backup_receipt(path)

    assert "private-secret" not in str(raised.value)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o700])
def test_receipt_loader_rejects_non_private_input(tmp_path, mode):
    path = tmp_path / "receipt.json"
    _write_payload(path, _verified_receipt().to_wire(), mode=mode)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        load_backup_receipt(path)


def test_receipt_writer_is_exclusive_and_refuses_a_final_symlink(tmp_path):
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    path = directory / f"backup-{GENERATION_ID}.json"
    target = tmp_path / "outside.json"
    path.symlink_to(target)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, receipt)

    assert path.is_symlink()
    assert not target.exists()


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_receipt_writer_requires_a_real_mode_0700_directory(tmp_path, unsafe):
    directory = _receipt_directory(tmp_path)
    if unsafe == "mode":
        directory.chmod(0o750)
    else:
        target = tmp_path / "real-receipts"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, _verified_receipt())


def test_receipt_directory_rejection_does_not_leak_descriptors(tmp_path):
    not_a_directory = tmp_path / "receipts"
    _write_payload(not_a_directory, {})
    baseline = len(list(Path("/dev/fd").iterdir()))

    for _ in range(64):
        with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
            latest_backup_receipt(not_a_directory, STORE_SCOPE_ID)

    assert len(list(Path("/dev/fd").iterdir())) <= baseline + 1


def test_receipt_chain_round_trip_is_root_to_selected_and_latest_is_tail(tmp_path):
    directory = _receipt_directory(tmp_path)
    root = _verified_receipt()
    root_path = write_backup_receipt(directory, root)
    child = _verified_receipt(
        generation_id=SECOND_GENERATION_ID,
        previous_receipt_sha256=private_json_sha256(root.to_wire()),
    )
    child_path = write_backup_receipt(directory, child)

    assert load_backup_receipt_chain(child_path, directory) == (root, child)
    assert load_backup_receipt_chain(root_path, directory) == (root,)
    assert latest_backup_receipt(directory, STORE_SCOPE_ID) == child
    assert latest_backup_receipt(directory, OTHER_STORE_SCOPE_ID) is None


def test_receipt_chain_rejects_cross_scope_link_without_exposing_scope(tmp_path):
    directory = _receipt_directory(tmp_path)
    root = _verified_receipt()
    write_backup_receipt(directory, root)
    child = _verified_receipt(
        generation_id=SECOND_GENERATION_ID,
        store_scope_id=OTHER_STORE_SCOPE_ID,
        previous_receipt_sha256=private_json_sha256(root.to_wire()),
    )
    child_path = write_backup_receipt(directory, child)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$") as raised:
        load_backup_receipt_chain(child_path, directory)

    assert STORE_SCOPE_ID not in str(raised.value)
    assert OTHER_STORE_SCOPE_ID not in str(raised.value)


def test_receipt_chain_rejects_a_fork(tmp_path):
    directory = _receipt_directory(tmp_path)
    root = _verified_receipt()
    write_backup_receipt(directory, root)
    previous = private_json_sha256(root.to_wire())
    first = _verified_receipt(
        generation_id=SECOND_GENERATION_ID,
        previous_receipt_sha256=previous,
    )
    second = _verified_receipt(
        generation_id=THIRD_GENERATION_ID,
        previous_receipt_sha256=previous,
    )
    first_path = write_backup_receipt(directory, first)
    write_backup_receipt(directory, second)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        load_backup_receipt_chain(first_path, directory)
    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        latest_backup_receipt(directory, STORE_SCOPE_ID)


def test_receipt_chain_rejects_a_cycle(tmp_path, monkeypatch):
    directory = _receipt_directory(tmp_path)
    first = _verified_receipt(previous_receipt_sha256="b" * 64)
    second = _verified_receipt(
        generation_id=SECOND_GENERATION_ID,
        previous_receipt_sha256="a" * 64,
    )
    first_path = write_backup_receipt(directory, first)
    write_backup_receipt(directory, second)

    def controlled_digest(payload):
        return "a" * 64 if payload["generation_id"] == GENERATION_ID else "b" * 64

    monkeypatch.setattr(receipt_module, "private_json_sha256", controlled_digest)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        load_backup_receipt_chain(first_path, directory)


def test_receipt_scan_rejects_more_than_1024_candidates_before_decoding(tmp_path):
    directory = _receipt_directory(tmp_path)
    for number in range(1025):
        generation_id = UUID(int=number + 1, version=4)
        _write_payload(directory / f"backup-{generation_id}.json", {})

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        latest_backup_receipt(directory, STORE_SCOPE_ID)


def test_receipt_chain_rejects_filename_generation_mismatch(tmp_path):
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    wrong_path = directory / f"backup-{SECOND_GENERATION_ID}.json"
    _write_payload(wrong_path, receipt.to_wire())

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        load_backup_receipt_chain(wrong_path, directory)


def test_receipt_chain_refuses_selected_path_outside_receipt_directory(tmp_path):
    directory = _receipt_directory(tmp_path)
    outside = tmp_path / f"backup-{GENERATION_ID}.json"
    _write_payload(outside, _verified_receipt().to_wire())

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        load_backup_receipt_chain(outside, directory)


def test_receipt_writer_validates_before_creating_output(tmp_path):
    directory = _receipt_directory(tmp_path)
    invalid = replace(_verified_receipt(), source_inventory_sha256="private-secret")

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, invalid)

    assert list(directory.iterdir()) == []
