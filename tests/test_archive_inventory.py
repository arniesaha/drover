"""Private, bounded manifest artifacts for Pond inventory operations."""

import dataclasses
import json
import os
import stat

import pytest

from drover.server.archive import inventory as inventory_module
from drover.server.archive.inventory import (
    NativeInventory,
    NativeInventoryRecord,
    PondInventory,
    PondInventoryRecord,
    load_native_inventory,
    load_pond_inventory,
    read_private_json,
    write_private_json,
)


def _native_inventory() -> NativeInventory:
    return NativeInventory(
        schema_version=1,
        captured_at="2026-08-28T12:00:00Z",
        host_id="host-test",
        records=(
            NativeInventoryRecord(
                source_agent="codex-cli",
                session_id="native-1",
                updated_at="2026-08-28T11:00:00Z",
                size_bytes=123,
                source_copies=1,
            ),
        ),
    )


def _pond_inventory() -> PondInventory:
    return PondInventory(
        schema_version=1,
        captured_at="2026-08-28T12:00:00Z",
        pond_version="0.16.3",
        records=(
            PondInventoryRecord(
                session_id="pond-1",
                source_agent="codex-cli",
                created_at="2026-08-28T10:00:00Z",
                message_count=1,
                first_message_at="2026-08-28T10:01:00Z",
                last_message_at="2026-08-28T10:02:00Z",
            ),
        ),
    )


def _write_input(path, payload, *, mode=0o600):
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def test_native_inventory_round_trips_through_owner_only_file(tmp_path):
    path = tmp_path / "native.json"
    inventory = _native_inventory()

    write_private_json(path, inventory.to_wire())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_native_inventory(path) == inventory


def test_pond_inventory_round_trips_and_sorts_records(tmp_path):
    path = tmp_path / "pond.json"
    inventory = PondInventory(
        schema_version=1,
        captured_at="2026-08-28T12:00:00Z",
        pond_version="0.16.3",
        records=(
            PondInventoryRecord(
                "pond-2", "claude-code", "2026-08-28T10:00:00Z", 0, None, None
            ),
            _pond_inventory().records[0],
        ),
    )

    write_private_json(path, inventory.to_wire())

    assert load_pond_inventory(path) == PondInventory(
        schema_version=1,
        captured_at="2026-08-28T12:00:00Z",
        pond_version="0.16.3",
        records=(
            PondInventoryRecord(
                "pond-2", "claude-code", "2026-08-28T10:00:00Z", 0, None, None
            ),
            _pond_inventory().records[0],
        ),
    )


@pytest.mark.parametrize("factory", [_native_inventory, _pond_inventory])
def test_inventory_values_are_frozen_and_slotted(factory):
    inventory = factory()

    assert not hasattr(inventory, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        inventory.schema_version = 2


@pytest.mark.parametrize(
    ("kind", "loader", "payload"),
    [
        ("native", load_native_inventory, _native_inventory().to_wire()),
        ("pond", load_pond_inventory, _pond_inventory().to_wire()),
    ],
)
def test_typed_loaders_require_kind_to_match_manifest_type(
    tmp_path, kind, loader, payload
):
    payload["kind"] = (
        "pond_session_inventory" if kind == "native" else "native_source_inventory"
    )
    path = tmp_path / f"{kind}.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="kind"):
        loader(path)


@pytest.mark.parametrize("name", ["existing.json", "symlink.json"])
def test_private_writer_refuses_existing_output_including_symlink(tmp_path, name):
    path = tmp_path / name
    if name == "symlink.json":
        path.symlink_to(tmp_path / "not-created")
    else:
        path.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="output") as raised:
        write_private_json(path, _native_inventory().to_wire())

    assert str(path) not in str(raised.value)
    assert path.is_symlink() or path.read_text(encoding="utf-8") == "keep"


def test_private_descriptor_writer_never_follows_a_replaced_parent_path(tmp_path):
    original_parent = tmp_path / "original-parent"
    original_parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    hostile_parent = tmp_path / "hostile-parent"
    hostile_parent.mkdir()
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(original_parent, flags)
    try:
        original_parent.rename(moved_parent)
        original_parent.symlink_to(hostile_parent, target_is_directory=True)

        inventory_module._write_private_json_at(
            descriptor,
            "inventory.json",
            _native_inventory().to_wire(),
        )
    finally:
        os.close(descriptor)

    pinned_output = moved_parent / "inventory.json"
    assert load_native_inventory(pinned_output) == _native_inventory()
    assert stat.S_IMODE(pinned_output.stat().st_mode) == 0o600
    assert not (hostile_parent / "inventory.json").exists()


def test_private_writer_refuses_payload_larger_than_32_mib(tmp_path):
    path = tmp_path / "large.json"
    payload = {"payload": "x" * (32 * 1024 * 1024)}

    with pytest.raises(ValueError, match="size"):
        write_private_json(path, payload)

    assert not path.exists()


@pytest.mark.parametrize("kind", ["directory", "group-readable"])
def test_private_reader_refuses_non_private_regular_input(tmp_path, kind):
    path = tmp_path / "input.json"
    if kind == "directory":
        path.mkdir()
    else:
        _write_input(path, _native_inventory().to_wire(), mode=0o640)

    with pytest.raises(ValueError, match="input") as raised:
        read_private_json(path)

    assert str(path) not in str(raised.value)


def test_private_reader_refuses_symlink_input(tmp_path):
    target = tmp_path / "target.json"
    path = tmp_path / "input.json"
    _write_input(target, _native_inventory().to_wire())
    path.symlink_to(target)

    with pytest.raises(ValueError, match="input") as raised:
        read_private_json(path)

    assert str(path) not in str(raised.value)


def test_private_reader_rejects_file_larger_than_limit_before_decoding(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * 17)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="size"):
        read_private_json(path, max_bytes=16)


def test_private_reader_closes_descriptor_when_rejecting_group_readable_input(tmp_path):
    path = tmp_path / "group-readable.json"
    _write_input(path, _native_inventory().to_wire(), mode=0o640)
    baseline = len(os.listdir("/dev/fd"))

    for _ in range(64):
        with pytest.raises(ValueError, match="input"):
            read_private_json(path)

    assert len(os.listdir("/dev/fd")) <= baseline + 1


@pytest.mark.parametrize("body", [b"\xff", b"not json"])
def test_private_reader_rejects_invalid_utf8_or_json(tmp_path, body):
    path = tmp_path / "invalid.json"
    path.write_bytes(body)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="content"):
        read_private_json(path)


@pytest.mark.parametrize(
    ("remove", "add"),
    [
        ("host_id", None),
        (None, ("unexpected", True)),
    ],
)
def test_native_loader_requires_exact_root_fields(tmp_path, remove, add):
    payload = _native_inventory().to_wire()
    if remove:
        del payload[remove]
    if add:
        payload[add[0]] = add[1]
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="root"):
        load_native_inventory(path)


@pytest.mark.parametrize(
    ("remove", "add"),
    [
        ("size_bytes", None),
        (None, ("unexpected", True)),
    ],
)
def test_native_loader_requires_exact_record_fields(tmp_path, remove, add):
    payload = _native_inventory().to_wire()
    if remove:
        del payload["records"][0][remove]
    if add:
        payload["records"][0][add[0]] = add[1]
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="record"):
        load_native_inventory(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_id", ""),
        ("host_id", 1),
        ("source_agent", ""),
        ("source_agent", 1),
        ("session_id", ""),
        ("session_id", 1),
        ("updated_at", "not-a-timestamp"),
        ("size_bytes", -1),
        ("source_copies", -1),
    ],
)
def test_native_loader_rejects_invalid_field_values(tmp_path, field, value):
    payload = _native_inventory().to_wire()
    destination = payload if field == "host_id" else payload["records"][0]
    destination[field] = value
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match=field) as raised:
        load_native_inventory(path)

    assert repr(value) not in str(raised.value)


@pytest.mark.parametrize("schema_version", [0, 2, "1", 1.0, True])
def test_native_loader_accepts_only_schema_version_one(tmp_path, schema_version):
    payload = _native_inventory().to_wire()
    payload["schema_version"] = schema_version
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="schema_version"):
        load_native_inventory(path)


def test_native_loader_rejects_more_than_one_hundred_thousand_records(tmp_path):
    record = _native_inventory().to_wire()["records"][0]
    payload = _native_inventory().to_wire()
    payload["records"] = [record] * 100_001
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="records"):
        load_native_inventory(path)


def test_native_loader_rejects_duplicate_source_session_pair(tmp_path):
    payload = _native_inventory().to_wire()
    payload["records"].append(payload["records"][0].copy())
    path = tmp_path / "native.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="records"):
        load_native_inventory(path)


@pytest.mark.parametrize(
    ("remove", "add"),
    [
        ("pond_version", None),
        (None, ("unexpected", True)),
    ],
)
def test_pond_loader_requires_exact_root_fields(tmp_path, remove, add):
    payload = _pond_inventory().to_wire()
    if remove:
        del payload[remove]
    if add:
        payload[add[0]] = add[1]
    path = tmp_path / "pond.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="root"):
        load_pond_inventory(path)


@pytest.mark.parametrize(
    ("remove", "add"),
    [
        ("message_count", None),
        (None, ("unexpected", True)),
    ],
)
def test_pond_loader_requires_exact_record_fields(tmp_path, remove, add):
    payload = _pond_inventory().to_wire()
    if remove:
        del payload["records"][0][remove]
    if add:
        payload["records"][0][add[0]] = add[1]
    path = tmp_path / "pond.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="record"):
        load_pond_inventory(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pond_version", ""),
        ("pond_version", 1),
        ("session_id", ""),
        ("source_agent", ""),
        ("created_at", "not-a-timestamp"),
        ("message_count", -1),
    ],
)
def test_pond_loader_rejects_invalid_field_values(tmp_path, field, value):
    payload = _pond_inventory().to_wire()
    destination = payload if field == "pond_version" else payload["records"][0]
    destination[field] = value
    path = tmp_path / "pond.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match=field):
        load_pond_inventory(path)


@pytest.mark.parametrize(
    ("message_count", "first_message_at", "last_message_at"),
    [
        (0, "2026-08-28T10:01:00Z", "2026-08-28T10:02:00Z"),
        (0, None, "2026-08-28T10:02:00Z"),
        (1, None, None),
        (1, "not-a-timestamp", "2026-08-28T10:02:00Z"),
    ],
)
def test_pond_loader_enforces_message_timestamp_pairing(
    tmp_path, message_count, first_message_at, last_message_at
):
    payload = _pond_inventory().to_wire()
    payload["records"][0].update(
        message_count=message_count,
        first_message_at=first_message_at,
        last_message_at=last_message_at,
    )
    path = tmp_path / "pond.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="message"):
        load_pond_inventory(path)


def test_pond_loader_rejects_duplicate_source_session_pair(tmp_path):
    payload = _pond_inventory().to_wire()
    payload["records"].append(payload["records"][0].copy())
    path = tmp_path / "pond.json"
    _write_input(path, payload)

    with pytest.raises(ValueError, match="records"):
        load_pond_inventory(path)


@pytest.mark.parametrize(
    "inventory",
    [
        NativeInventory(
            schema_version=2,
            captured_at="2026-08-28T12:00:00Z",
            host_id="host-test",
            records=(),
        ),
        NativeInventory(
            schema_version=1,
            captured_at="2026-08-28T12:00:00Z",
            host_id="host-test",
            records=(
                NativeInventoryRecord(
                    "codex-cli", "native-1", "2026-08-28T11:00:00Z", 1, 1
                ),
                NativeInventoryRecord(
                    "codex-cli", "native-1", "2026-08-28T11:00:00Z", 1, 1
                ),
            ),
        ),
    ],
)
def test_to_wire_rejects_invalid_typed_manifest(inventory):
    with pytest.raises(ValueError):
        inventory.to_wire()
