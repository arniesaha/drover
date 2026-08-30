"""Exact private receipts and linear-chain validation for R2 backups."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
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


def _write_raw_private_json(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)


def test_receipt_round_trip_preserves_the_exact_v1_contract(tmp_path):
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()

    path = write_backup_receipt(directory, receipt)

    assert path == directory / f"backup-{GENERATION_ID}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert {entry.name for entry in directory.iterdir()} == {path.name}
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


@pytest.mark.parametrize("duplicate_location", ["root", "collision_counts"])
def test_receipt_loader_rejects_duplicate_json_keys(tmp_path, duplicate_location):
    encoded = json.dumps(_verified_receipt().to_wire(), separators=(",", ":"))
    if duplicate_location == "root":
        private_value = "private-secret-generation"
        encoded = encoded.replace("{", f'{{"generation_id":"{private_value}",', 1)
    else:
        private_value = "private-secret-collision"
        encoded = encoded.replace(
            '"collision_counts":{',
            '"collision_counts":{"duplicate_source_groups":1,',
            1,
        )
    path = tmp_path / "receipt.json"
    _write_raw_private_json(path, encoded)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$") as raised:
        load_backup_receipt(path)

    assert private_value not in str(raised.value)


def test_receipt_loader_rejects_receipt_below_a_symlinked_ancestor(tmp_path):
    real_parent = tmp_path / "real-private-parent"
    real_parent.mkdir(mode=0o700)
    receipt_path = real_parent / "receipt.json"
    _write_payload(receipt_path, _verified_receipt().to_wire())
    symlinked_parent = tmp_path / "private-secret-ancestor"
    symlinked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$") as raised:
        load_backup_receipt(symlinked_parent / receipt_path.name)

    assert symlinked_parent.name not in str(raised.value)


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


def test_receipt_writer_rejects_replacement_after_created_file_closes(
    tmp_path, monkeypatch
):
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    real_write = receipt_module._write_private_json_at

    def replace_after_write(descriptor, name, payload):
        created_identity = real_write(descriptor, name, payload)
        os.unlink(name, dir_fd=descriptor)
        real_write(descriptor, name, payload)
        return created_identity

    monkeypatch.setattr(receipt_module, "_write_private_json_at", replace_after_write)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, receipt)


def test_receipt_writer_removes_final_if_publication_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    final = directory / f"backup-{GENERATION_ID}.json"
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(receipt_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, receipt)

    assert calls >= 2
    assert not final.exists()


def test_receipt_writer_performs_no_syscall_or_callback_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    final_name = f"backup-{GENERATION_ID}.json"
    final = directory / final_name
    pinned_directory, descriptor, identity = receipt_module._open_receipt_directory(
        directory
    )
    real_lstat = os.lstat
    real_stat = os.stat
    real_fsync = os.fsync
    real_unlink = os.unlink
    callbacks = 0

    def published() -> bool:
        try:
            real_lstat(final)
        except FileNotFoundError:
            return False
        return True

    def reject_stat(*args, **kwargs):
        if published():
            raise AssertionError("stat after publication")
        return real_stat(*args, **kwargs)

    def reject_fsync(*args, **kwargs):
        if published():
            raise AssertionError("fsync after publication")
        return real_fsync(*args, **kwargs)

    def reject_unlink(*args, **kwargs):
        if published():
            raise AssertionError("unlink after publication")
        return real_unlink(*args, **kwargs)

    def before_publish() -> None:
        nonlocal callbacks
        assert not published()
        callbacks += 1

    try:
        with monkeypatch.context() as patch:
            patch.setattr(receipt_module.os, "stat", reject_stat)
            patch.setattr(receipt_module.os, "fsync", reject_fsync)
            patch.setattr(receipt_module.os, "unlink", reject_unlink)
            path = receipt_module._write_backup_receipt_at(
                pinned_directory,
                descriptor,
                identity,
                receipt,
                before_publish=before_publish,
            )
    finally:
        os.close(descriptor)

    assert path == final
    assert callbacks == 1
    assert final.is_file()


def test_postpublication_permission_loss_cannot_turn_success_into_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    final_name = f"backup-{GENERATION_ID}.json"
    final = directory / final_name
    real_lstat = os.lstat
    real_stat = os.stat
    changed_after_publication = False

    def published() -> bool:
        try:
            real_lstat(final)
        except FileNotFoundError:
            return False
        return True

    def remove_write_permission_on_postpublication_stat(path, *args, **kwargs):
        nonlocal changed_after_publication
        if (
            not changed_after_publication
            and kwargs.get("dir_fd") is not None
            and published()
        ):
            os.fchmod(kwargs["dir_fd"], 0o500)
            changed_after_publication = True
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        receipt_module.os,
        "stat",
        remove_write_permission_on_postpublication_stat,
    )
    failure: Exception | None = None
    path: Path | None = None
    try:
        try:
            path = write_backup_receipt(directory, receipt)
        except Exception as error:
            failure = error
    finally:
        directory.chmod(0o700)

    assert final.is_file()
    assert failure is None
    assert path == final
    assert changed_after_publication is False


@pytest.mark.parametrize("competitor", ["file", "symlink"])
def test_exclusive_publication_refuses_a_concurrent_final_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    competitor: str,
) -> None:
    directory = _receipt_directory(tmp_path)
    receipt = _verified_receipt()
    final = directory / f"backup-{GENERATION_ID}.json"
    target = tmp_path / "outside-private"
    real_fsync = os.fsync
    fsync_calls = 0

    def create_competitor_before_publication(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            try:
                if competitor == "file":
                    competing_descriptor = os.open(
                        final.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=descriptor,
                    )
                    try:
                        os.write(competing_descriptor, b"competing private entry")
                        real_fsync(competing_descriptor)
                    finally:
                        os.close(competing_descriptor)
                else:
                    os.symlink(target, final.name, dir_fd=descriptor)
            except FileExistsError:
                pass
        real_fsync(descriptor)

    monkeypatch.setattr(
        receipt_module.os, "fsync", create_competitor_before_publication
    )

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, receipt)

    if competitor == "file":
        assert final.read_bytes() == b"competing private entry"
    else:
        assert final.is_symlink()
        assert os.readlink(final) == str(target)
        assert not target.exists()


@pytest.mark.parametrize(
    ("platform", "symbol", "flag"),
    [
        ("darwin", "renameatx_np", 0x00000004),
        ("linux", "renameat2", 0x00000001),
    ],
)
def test_exclusive_rename_selects_the_platform_no_replace_primitive(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
    flag: int,
) -> None:
    class Function:
        argtypes = None
        restype = None

    function = Function()
    library = type("Library", (), {symbol: function})()
    monkeypatch.setattr(receipt_module.sys, "platform", platform)
    monkeypatch.setattr(
        receipt_module.ctypes, "CDLL", lambda *_args, **_kwargs: library
    )

    selected, selected_flag = receipt_module._load_exclusive_rename()

    assert selected is function
    assert selected_flag == flag
    assert function.argtypes == (
        receipt_module.ctypes.c_int,
        receipt_module.ctypes.c_char_p,
        receipt_module.ctypes.c_int,
        receipt_module.ctypes.c_char_p,
        receipt_module.ctypes.c_uint,
    )
    assert function.restype is receipt_module.ctypes.c_int


def test_unsupported_exclusive_rename_fails_before_creating_any_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _receipt_directory(tmp_path)
    monkeypatch.setattr(receipt_module, "_EXCLUSIVE_RENAME", None)
    monkeypatch.setattr(receipt_module, "_EXCLUSIVE_RENAME_FLAG", 0)

    with pytest.raises(ValueError, match=r"^archive backup receipt failed$"):
        write_backup_receipt(directory, _verified_receipt())

    assert list(directory.iterdir()) == []


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
