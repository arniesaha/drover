"""Bounded, local-only Pond inventory export contract."""

from __future__ import annotations

import dataclasses
import json
import stat
import textwrap
from pathlib import Path

import pytest

from drover.server.archive.inventory import load_pond_inventory
from drover.server.archive.pond_inventory import (
    POND_INVENTORY_SQL,
    export_pond_inventory,
    pond_inventory_summary,
)

_COLUMNS = {
    "session_id",
    "source_agent",
    "created_at",
    "message_count",
    "first_message_at",
    "last_message_at",
}

_DEFAULT_ROWS = (
    {
        "session_id": "pond-codex-1",
        "source_agent": "codex-cli",
        "created_at": "2026-08-28T12:00:00+02:00",
        "message_count": 2,
        "first_message_at": "2026-08-28T12:01:00+02:00",
        "last_message_at": "2026-08-28T12:02:00+02:00",
    },
    {
        "session_id": "pond-claude-1",
        "source_agent": "claude-code",
        "created_at": "2026-08-28T09:00:00Z",
        "message_count": 0,
        "first_message_at": None,
        "last_message_at": None,
    },
)

_FAKE_POND = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

record_path = Path(os.environ["FAKE_POND_RECORD"])
try:
    calls = json.loads(record_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    calls = []
calls.append({"argv": sys.argv, "storage_env": os.environ.get("POND_STORAGE_PATH")})
record_path.write_text(json.dumps(calls), encoding="utf-8")

if sys.argv[1:] == ["--version"]:
    if os.environ.get("FAKE_VERSION_MODE") == "oversize_stdout":
        sys.stdout.buffer.write(b"v" * (32 * 1024 * 1024 + 1))
    elif os.environ.get("FAKE_VERSION_MODE") == "invalid_utf8":
        sys.stdout.buffer.write(b"pond \xff\n")
    else:
        sys.stdout.write(os.environ.get("FAKE_POND_VERSION", "pond 0.16.3\n"))
    raise SystemExit(int(os.environ.get("FAKE_VERSION_EXIT", "0")))

mode = os.environ.get("FAKE_SQL_MODE", "success")
output = Path(sys.argv[sys.argv.index("--output-file") + 1])
if mode == "timeout":
    time.sleep(30)
elif mode == "nonzero":
    sys.stdout.write("SENSITIVE STDOUT pond-session-secret")
    sys.stderr.write("SENSITIVE STDERR storage-secret")
    raise SystemExit(17)
elif mode == "missing_export":
    pass
elif mode == "oversize_export":
    with output.open("wb") as stream:
        for _ in range(513):
            stream.write(b"x" * 65536)
            stream.flush()
    time.sleep(30)
elif mode == "oversize_stdout":
    sys.stdout.buffer.write(b"x" * (32 * 1024 * 1024 + 1))
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "oversize_stderr":
    sys.stderr.buffer.write(b"x" * (32 * 1024 * 1024 + 1))
    sys.stderr.buffer.flush()
    time.sleep(30)
elif mode == "invalid_utf8":
    output.write_bytes(b"{\"session_id\":\xff}\n")
elif mode == "too_many_rows":
    with output.open("w", encoding="utf-8") as stream:
        for index in range(100001):
            stream.write(json.dumps({
                "session_id": f"session-{index}",
                "source_agent": "codex-cli",
                "created_at": "2026-08-28T10:00:00Z",
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
            }, separators=(",", ":")) + "\n")
elif mode == "unsafe_export_mode":
    output.write_text(os.environ["FAKE_NDJSON"], encoding="utf-8")
    output.chmod(0o644)
else:
    output.write_text(os.environ["FAKE_NDJSON"], encoding="utf-8")
"""


def _ndjson(rows=_DEFAULT_ROWS) -> str:
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


@pytest.fixture
def fake_pond(tmp_path):
    binary = tmp_path / "pond"
    binary.write_text(textwrap.dedent(_FAKE_POND), encoding="utf-8")
    binary.chmod(0o700)
    local_store = tmp_path / "local-store"
    local_store.mkdir()
    record = tmp_path / "calls.json"
    return binary, local_store, record


def _environment(record: Path, **values: str) -> dict[str, str]:
    return {
        "FAKE_POND_RECORD": str(record),
        "FAKE_NDJSON": _ndjson(),
        **values,
    }


def _export(fake_pond, output: Path, **env_values: str):
    binary, local_store, record = fake_pond
    inventory = export_pond_inventory(
        binary,
        output,
        storage_path=local_store,
        env=_environment(record, **env_values),
    )
    calls = json.loads(record.read_text(encoding="utf-8"))
    return inventory, calls


def test_export_uses_exact_pinned_cli_contract_and_private_staging_target(
    fake_pond, tmp_path
):
    binary, local_store, _ = fake_pond
    output = tmp_path / "pond-inventory.json"

    inventory, calls = _export(fake_pond, output)

    export_path = Path(calls[1]["argv"][calls[1]["argv"].index("--output-file") + 1])
    assert calls[0]["argv"] == [str(binary), "--version"]
    assert calls[1]["argv"] == [
        str(binary),
        "--storage-path",
        str(local_store),
        "sql",
        POND_INVENTORY_SQL,
        "--format",
        "ndjson",
        "--output-file",
        str(export_path),
        "--timeout",
        "60",
    ]
    assert export_path != output
    assert not export_path.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert load_pond_inventory(output) == inventory


def test_export_normalizes_datafusion_timestamps_and_returns_safe_summary(
    fake_pond, tmp_path
):
    inventory, _ = _export(fake_pond, tmp_path / "pond.json")

    by_session = {row.session_id: row for row in inventory.records}
    assert by_session["pond-codex-1"].created_at == "2026-08-28T10:00:00Z"
    assert by_session["pond-codex-1"].first_message_at == "2026-08-28T10:01:00Z"
    assert by_session["pond-codex-1"].last_message_at == "2026-08-28T10:02:00Z"
    assert pond_inventory_summary(inventory) == {
        "schema_version": 1,
        "pond_version": "0.16.3",
        "archive_sessions": 2,
        "empty_sessions": 1,
        "by_harness": {"claude-code": 1, "codex-cli": 1},
    }


@pytest.mark.parametrize(
    "version",
    [
        "pond 0.16.2\n",
        "pond 0.16.30\n",
        "pond 0.16.3-dev\n",
        "0.16.3\n",
        "pond version 0.16.3\n",
        "pond 0.16.3 extra\n",
        "",
    ],
)
def test_export_requires_the_exact_pinned_version_tokens(fake_pond, tmp_path, version):
    _, _, record = fake_pond

    with pytest.raises(ValueError, match="version"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_POND_VERSION=version)

    assert len(json.loads(record.read_text(encoding="utf-8"))) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"FAKE_VERSION_EXIT": "3"},
        {"FAKE_VERSION_MODE": "invalid_utf8"},
        {"FAKE_VERSION_MODE": "oversize_stdout"},
    ],
)
def test_export_rejects_failed_malformed_or_oversized_version_output(
    fake_pond, tmp_path, values
):
    with pytest.raises(ValueError, match="version|size"):
        _export(fake_pond, tmp_path / "pond.json", **values)


@pytest.mark.parametrize("kind", ["missing", "non_executable"])
def test_export_rejects_missing_or_non_executable_binary_before_start(
    fake_pond, tmp_path, kind
):
    binary, local_store, record = fake_pond
    if kind == "missing":
        binary = tmp_path / "missing-pond"
    else:
        binary.chmod(0o600)

    with pytest.raises(ValueError, match="binary") as raised:
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=local_store,
            env=_environment(record),
        )

    assert not record.exists()
    assert str(binary) not in str(raised.value)


@pytest.mark.parametrize(
    "storage_kind",
    [
        "relative",
        "missing",
        "file",
        "s3://bucket/prefix",
        "file:///private/tmp/pond",
        "https://storage.example/pond",
        "custom+store://bucket",
    ],
)
def test_export_rejects_nonlocal_or_non_directory_storage_before_start(
    fake_pond, tmp_path, storage_kind
):
    binary, _, record = fake_pond
    if storage_kind == "relative":
        storage_path = Path("relative-store")
    elif storage_kind == "missing":
        storage_path = tmp_path / "missing-store"
    elif storage_kind == "file":
        storage_path = tmp_path / "store-file"
        storage_path.write_text("not a directory", encoding="utf-8")
    else:
        storage_path = Path(storage_kind)

    with pytest.raises(ValueError, match="storage") as raised:
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=storage_path,
            env=_environment(record),
        )

    assert not record.exists()
    assert str(storage_path) not in str(raised.value)


def test_explicit_storage_path_removes_inherited_remote_selector(fake_pond, tmp_path):
    _, calls = _export(
        fake_pond,
        tmp_path / "pond.json",
        POND_STORAGE_PATH="s3://sensitive-bucket/private-prefix",
    )

    assert calls[0]["storage_env"] is None
    assert calls[1]["storage_env"] is None


@pytest.mark.parametrize(
    "timeout",
    [4.999, 600.001, 5.5, float("nan"), float("inf"), True, "60"],
)
def test_export_rejects_timeout_that_is_out_of_range_or_not_an_exact_integer(
    fake_pond, tmp_path, timeout
):
    binary, local_store, record = fake_pond

    with pytest.raises(ValueError, match="timeout"):
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=local_store,
            timeout_seconds=timeout,
            env=_environment(record),
        )

    assert not record.exists()


def test_export_renders_an_exact_integral_float_timeout(fake_pond, tmp_path):
    binary, local_store, record = fake_pond

    export_pond_inventory(
        binary,
        tmp_path / "pond.json",
        storage_path=local_store,
        timeout_seconds=5.0,
        env=_environment(record),
    )

    calls = json.loads(record.read_text(encoding="utf-8"))
    assert calls[1]["argv"][-2:] == ["--timeout", "5"]


def test_export_terminates_timed_out_sql_without_writing_manifest(fake_pond, tmp_path):
    binary, local_store, record = fake_pond
    output = tmp_path / "pond.json"

    with pytest.raises(ValueError, match="timeout"):
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            timeout_seconds=5,
            env=_environment(record, FAKE_SQL_MODE="timeout"),
        )

    assert not output.exists()
    assert len(json.loads(record.read_text(encoding="utf-8"))) == 2


def test_export_does_not_retry_a_nonzero_sql_exit_or_leak_child_output(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    output = tmp_path / "pond.json"

    with pytest.raises(ValueError, match="subprocess") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(record, FAKE_SQL_MODE="nonzero"),
        )

    assert len(json.loads(record.read_text(encoding="utf-8"))) == 2
    assert "SENSITIVE" not in str(raised.value)
    assert "pond-session-secret" not in str(raised.value)
    assert "storage-secret" not in str(raised.value)
    assert not output.exists()


def test_export_rejects_missing_ndjson_artifact(fake_pond, tmp_path):
    with pytest.raises(ValueError, match="export"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_SQL_MODE="missing_export")


@pytest.mark.parametrize(
    "mode",
    ["oversize_export", "oversize_stdout", "oversize_stderr"],
)
def test_export_stops_a_child_when_any_output_crosses_32_mib(fake_pond, tmp_path, mode):
    output = tmp_path / "pond.json"

    with pytest.raises(ValueError, match="size"):
        _export(fake_pond, output, FAKE_SQL_MODE=mode)

    assert not output.exists()


@pytest.mark.parametrize(
    "mode",
    ["invalid_utf8", "unsafe_export_mode"],
)
def test_export_requires_a_private_utf8_ndjson_artifact(fake_pond, tmp_path, mode):
    with pytest.raises(ValueError, match="export|row"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_SQL_MODE=mode)


@pytest.mark.parametrize("body", ["not-json\n", "[]\n", "\n"])
def test_export_rejects_invalid_ndjson_rows(fake_pond, tmp_path, body):
    with pytest.raises(ValueError, match="row"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=body)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_export_requires_exact_six_columns(fake_pond, tmp_path, mutation):
    row = dict(_DEFAULT_ROWS[0])
    if mutation == "missing":
        del row["last_message_at"]
    else:
        row["project"] = "/sensitive/project"

    with pytest.raises(ValueError, match="columns"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))


def test_export_rejects_duplicate_source_session_identity(fake_pond, tmp_path):
    rows = (_DEFAULT_ROWS[0], dict(_DEFAULT_ROWS[0]))

    with pytest.raises(ValueError, match="duplicate") as raised:
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson(rows))

    assert "pond-codex-1" not in str(raised.value)


def test_export_rejects_more_than_one_hundred_thousand_rows(fake_pond, tmp_path):
    with pytest.raises(ValueError, match="rows"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_SQL_MODE="too_many_rows")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", ""),
        ("session_id", 4),
        ("source_agent", "codex"),
        ("source_agent", "claude-code-subagent"),
        ("created_at", "not-a-timestamp"),
        ("created_at", "2026-08-28T10:00:00"),
        ("created_at", "20260828T100000Z"),
        ("message_count", -1),
        ("message_count", 1.5),
        ("message_count", True),
        ("first_message_at", "not-a-timestamp"),
    ],
)
def test_export_rejects_invalid_identity_timestamp_or_count(
    fake_pond, tmp_path, field, value
):
    row = dict(_DEFAULT_ROWS[0])
    row[field] = value

    with pytest.raises(ValueError, match="row") as raised:
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))

    assert repr(value) not in str(raised.value)


@pytest.mark.parametrize(
    ("count", "first", "last"),
    [
        (0, "2026-08-28T10:01:00Z", "2026-08-28T10:02:00Z"),
        (1, None, None),
        (1, "2026-08-28T10:03:00Z", "2026-08-28T10:02:00Z"),
    ],
)
def test_export_enforces_message_count_timestamp_relationships(
    fake_pond, tmp_path, count, first, last
):
    row = dict(_DEFAULT_ROWS[0])
    row.update(
        message_count=count,
        first_message_at=first,
        last_message_at=last,
    )

    with pytest.raises(ValueError, match="row"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))


def test_export_orders_canonical_timestamps_by_instant_not_rendered_text(
    fake_pond, tmp_path
):
    row = dict(_DEFAULT_ROWS[0])
    row.update(
        first_message_at="2026-08-28T10:00:00Z",
        last_message_at="2026-08-28T10:00:00.1Z",
    )

    inventory, _ = _export(
        fake_pond,
        tmp_path / "pond.json",
        FAKE_NDJSON=_ndjson((row,)),
    )

    assert inventory.records[0].last_message_at == "2026-08-28T10:00:00.100000Z"


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("pond_version", "SENSITIVE-unpinned-version")],
)
def test_pond_summary_refuses_an_invalid_or_unpinned_inventory(
    fake_pond, tmp_path, field, value
):
    inventory, _ = _export(fake_pond, tmp_path / "pond.json")
    invalid = dataclasses.replace(inventory, **{field: value})

    with pytest.raises(ValueError) as raised:
        pond_inventory_summary(invalid)

    assert repr(value) not in str(raised.value)


def test_export_invokes_binary_directly_without_a_shell(tmp_path):
    binary = tmp_path / "pond; touch SHELL_INJECTION_WORKED"
    binary.write_text(textwrap.dedent(_FAKE_POND), encoding="utf-8")
    binary.chmod(0o700)
    local_store = tmp_path / "local store; still argv"
    local_store.mkdir()
    record = tmp_path / "calls.json"

    export_pond_inventory(
        binary,
        tmp_path / "pond.json",
        storage_path=local_store,
        env=_environment(record),
    )

    assert not (tmp_path / "SHELL_INJECTION_WORKED").exists()


def test_export_errors_do_not_disclose_paths_sql_rows_ids_or_storage(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    secret_id = "SENSITIVE-POND-SESSION-ID"
    secret_row = dict(_DEFAULT_ROWS[0], session_id=secret_id)
    output = tmp_path / "SENSITIVE-FINAL-PATH.json"

    with pytest.raises(ValueError) as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(
                record,
                FAKE_NDJSON=_ndjson((secret_row, secret_row)),
            ),
        )

    message = str(raised.value)
    for secret in (str(binary), str(local_store), str(output), secret_id, "SELECT"):
        assert secret not in message


def test_pond_inventory_sql_selects_only_the_bounded_metadata_projection():
    assert POND_INVENTORY_SQL == """SELECT s.session_id, s.source_agent, s.created_at,
       count(m.message_id) AS message_count,
       min(m.timestamp) AS first_message_at,
       max(m.timestamp) AS last_message_at
FROM sessions s
LEFT JOIN messages m ON m.session_id = s.session_id
WHERE s.source_agent IN ('claude-code', 'codex-cli')
GROUP BY s.session_id, s.source_agent, s.created_at
ORDER BY s.source_agent, s.session_id
LIMIT 100001"""
    assert _COLUMNS == set(_DEFAULT_ROWS[0])
