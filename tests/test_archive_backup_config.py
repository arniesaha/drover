"""Strict, private configuration for manual Pond-to-R2 backups."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from drover.server.archive.backup_config import (
    BackupConfig,
    generation_storage_url,
    load_backup_config,
)

GENERATION_ID = "33333333-3333-4333-8333-333333333333"
STORE_SCOPE_ID = "11111111-1111-4111-8111-111111111111"
EXPECTED_CONFIG_FIELDS = {
    "schema_version",
    "pond_binary",
    "local_pond_config",
    "local_store",
    "remote_pond_config",
    "backup_root_url",
    "store_scope_id",
    "receipt_directory",
    "copy_timeout_seconds",
    "max_rss_bytes",
    "max_physical_bytes",
    "max_swap_growth_bytes",
}


def _private_file(path: Path, body: str = "") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _valid_config_values(tmp_path: Path) -> dict[str, object]:
    pond_binary = _private_file(tmp_path / "pond", "#!/bin/sh\n")
    pond_binary.chmod(0o700)
    local_pond_config = _private_file(tmp_path / "local-pond.toml")
    remote_pond_config = _private_file(tmp_path / "remote-pond.toml")
    local_store = tmp_path / "pond-store"
    local_store.mkdir(mode=0o700)
    receipt_directory = tmp_path / "receipts"
    receipt_directory.mkdir(mode=0o700)
    return {
        "schema_version": 1,
        "pond_binary": str(pond_binary),
        "local_pond_config": str(local_pond_config),
        "local_store": str(local_store),
        "remote_pond_config": str(remote_pond_config),
        "backup_root_url": (
            "s3+https://0123456789abcdef.r2.cloudflarestorage.com/"
            "private-bucket/drover"
        ),
        "store_scope_id": STORE_SCOPE_ID,
        "receipt_directory": str(receipt_directory),
        "copy_timeout_seconds": 1800,
        "max_rss_bytes": 3 * 1024**3,
        "max_physical_bytes": 4 * 1024**3,
        "max_swap_growth_bytes": 512 * 1024**2,
    }


def _toml_scalar(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_config(path: Path, values: dict[str, object], *, mode: int = 0o600) -> Path:
    body = "\n".join(f"{key} = {_toml_scalar(value)}" for key, value in values.items())
    path.write_text(body + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _valid_config_path(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    values = _valid_config_values(tmp_path)
    return _write_config(tmp_path / "backup.toml", values), values


def test_backup_config_loads_the_exact_private_contract(tmp_path):
    config_path, values = _valid_config_path(tmp_path)

    config = load_backup_config(config_path)

    assert isinstance(config, BackupConfig)
    assert not hasattr(config, "__dict__")
    assert set(config.__dataclass_fields__) == EXPECTED_CONFIG_FIELDS
    assert config.schema_version == 1
    assert config.pond_binary == Path(values["pond_binary"])
    assert config.max_rss_bytes == 3 * 1024**3
    assert config.max_physical_bytes == 4 * 1024**3
    assert config.max_swap_growth_bytes == 512 * 1024**2
    assert generation_storage_url(config, UUID(GENERATION_ID)) == (
        f"{values['backup_root_url']}/generations/{GENERATION_ID}"
    )


@pytest.mark.parametrize("timeout", [5, 1800])
def test_backup_config_accepts_inclusive_copy_timeout_boundaries(tmp_path, timeout):
    values = _valid_config_values(tmp_path)
    values["copy_timeout_seconds"] = timeout
    config_path = _write_config(tmp_path / "backup.toml", values)

    assert load_backup_config(config_path).copy_timeout_seconds == timeout


@pytest.mark.parametrize(
    ("mutation", "secret_value"),
    [
        ("config_mode", "mode-secret"),
        ("unknown_key", "unknown-secret"),
        ("missing_key", "missing-secret"),
        ("credential_in_url", "credential-secret"),
        ("query_in_url", "query-secret"),
        ("fragment_in_url", "fragment-secret"),
        ("non_r2_authority", "not-r2.example"),
        ("embedded_generation", GENERATION_ID),
        ("relative_local_store", "relative-store-secret"),
        ("receipt_directory_mode", "directory-mode-secret"),
        ("rss_above_ceiling", str(3 * 1024**3 + 1)),
        ("physical_above_ceiling", str(4 * 1024**3 + 1)),
        ("swap_above_ceiling", str(512 * 1024**2 + 1)),
        ("timeout_below_minimum", "4"),
        ("timeout_above_maximum", "1801"),
    ],
)
def test_backup_config_fails_closed_without_echoing_values(
    tmp_path, mutation, secret_value
):
    values = _valid_config_values(tmp_path)
    config_path = tmp_path / "backup.toml"
    mode = 0o600
    if mutation == "config_mode":
        mode = 0o640
    elif mutation == "unknown_key":
        values["unknown-secret"] = secret_value
    elif mutation == "missing_key":
        del values["pond_binary"]
    elif mutation == "credential_in_url":
        values["backup_root_url"] = (
            "s3+https://user:credential-secret@"
            "0123456789abcdef.r2.cloudflarestorage.com/private-bucket/drover"
        )
    elif mutation == "query_in_url":
        values["backup_root_url"] = (
            "s3+https://0123456789abcdef.r2.cloudflarestorage.com/"
            "private-bucket/drover?query-secret"
        )
    elif mutation == "fragment_in_url":
        values["backup_root_url"] = (
            "s3+https://0123456789abcdef.r2.cloudflarestorage.com/"
            "private-bucket/drover#fragment-secret"
        )
    elif mutation == "non_r2_authority":
        values["backup_root_url"] = "s3+https://not-r2.example/private-bucket"
    elif mutation == "embedded_generation":
        values["backup_root_url"] = (
            "s3+https://0123456789abcdef.r2.cloudflarestorage.com/"
            f"private-bucket/generations/{GENERATION_ID}"
        )
    elif mutation == "relative_local_store":
        values["local_store"] = secret_value
    elif mutation == "receipt_directory_mode":
        Path(values["receipt_directory"]).chmod(0o750)
    elif mutation == "rss_above_ceiling":
        values["max_rss_bytes"] = int(secret_value)
    elif mutation == "physical_above_ceiling":
        values["max_physical_bytes"] = int(secret_value)
    elif mutation == "swap_above_ceiling":
        values["max_swap_growth_bytes"] = int(secret_value)
    elif mutation == "timeout_below_minimum":
        values["copy_timeout_seconds"] = 4
    elif mutation == "timeout_above_maximum":
        values["copy_timeout_seconds"] = 1801
    _write_config(config_path, values, mode=mode)

    with pytest.raises(ValueError, match=r"^archive backup config failed$") as raised:
        load_backup_config(config_path)

    assert secret_value not in str(raised.value)


@pytest.mark.parametrize(
    "target",
    ["pond_binary", "local_pond_config", "local_store", "remote_pond_config"],
)
def test_backup_config_rejects_symlinked_local_inputs(tmp_path, target):
    values = _valid_config_values(tmp_path)
    original = Path(values[target])
    moved = original.with_name(f"real-{original.name}")
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=moved.is_dir())
    config_path = _write_config(tmp_path / "backup.toml", values)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


@pytest.mark.parametrize("target", ["local_pond_config", "remote_pond_config"])
def test_backup_config_rejects_group_readable_pond_configs(tmp_path, target):
    values = _valid_config_values(tmp_path)
    Path(values[target]).chmod(0o640)
    config_path = _write_config(tmp_path / "backup.toml", values)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


def test_backup_config_rejects_symlinked_config_file(tmp_path):
    values = _valid_config_values(tmp_path)
    target = _write_config(tmp_path / "real-backup.toml", values)
    config_path = tmp_path / "backup.toml"
    config_path.symlink_to(target)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


def test_backup_config_rejects_config_below_a_symlinked_ancestor(tmp_path):
    values = _valid_config_values(tmp_path)
    real_parent = tmp_path / "real-private-parent"
    real_parent.mkdir(mode=0o700)
    config_path = _write_config(real_parent / "backup.toml", values)
    symlinked_parent = tmp_path / "private-secret-ancestor"
    symlinked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match=r"^archive backup config failed$") as raised:
        load_backup_config(symlinked_parent / config_path.name)

    assert symlinked_parent.name not in str(raised.value)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        (" ", ""),
        ("", " "),
        ("\t", ""),
        ("", "\t"),
        ("\n", ""),
        ("", "\n"),
        ("\r", ""),
        ("", "\r"),
        ("\x01", ""),
        ("", "\x1f"),
    ],
)
def test_backup_config_rejects_noncanonical_url_text(
    tmp_path, prefix: str, suffix: str
):
    values = _valid_config_values(tmp_path)
    private_value = prefix + str(values["backup_root_url"]) + suffix
    values["backup_root_url"] = private_value
    config_path = _write_config(tmp_path / "backup.toml", values)

    with pytest.raises(ValueError, match=r"^archive backup config failed$") as raised:
        load_backup_config(config_path)

    assert private_value not in str(raised.value)


def test_backup_config_rejects_noncanonical_executable_path(tmp_path):
    values = _valid_config_values(tmp_path)
    values["pond_binary"] = str(tmp_path / "pond-store" / ".." / "pond")
    config_path = _write_config(tmp_path / "backup.toml", values)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


def test_backup_config_rejects_nonexecutable_binary(tmp_path):
    values = _valid_config_values(tmp_path)
    Path(values["pond_binary"]).chmod(0o600)
    config_path = _write_config(tmp_path / "backup.toml", values)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


def test_backup_config_rejects_toml_larger_than_32_mib_without_reading_it(tmp_path):
    config_path = tmp_path / "backup.toml"
    with config_path.open("wb") as output:
        output.seek(32 * 1024 * 1024)
        output.write(b"x")
    config_path.chmod(0o600)

    with pytest.raises(ValueError, match=r"^archive backup config failed$"):
        load_backup_config(config_path)


def test_generation_url_rejects_non_uuid_identity_without_leaking_it(tmp_path):
    config_path, _ = _valid_config_path(tmp_path)
    config = load_backup_config(config_path)
    secret = "generation-secret"

    with pytest.raises(ValueError, match=r"^archive backup config failed$") as raised:
        generation_storage_url(config, secret)

    assert secret not in str(raised.value)
