"""Exact, bounded Pond corpus snapshots for local and remote stores."""

from __future__ import annotations

import importlib
import json
import os
import stat
import textwrap
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from drover.server.archive.backup_runtime import BackupRuntimeError
from drover.server.archive.inventory import PondInventory, PondInventoryRecord
from drover.server.archive.pond_process import (
    PondProcessError,
    PondProcessResult,
    PondResourceEvidence,
    ResourceLimits,
    _pin_pond_executable,
)
from drover.server.archive.pond_snapshot import (
    POND_CORPUS_SNAPSHOT_SQL,
    LocalPondStore,
    PondCorpusCounts,
    PondStoreSnapshot,
    RemotePondGeneration,
    _corpus_snapshot,
    capture_pond_store_snapshot,
    pond_inventory_content_sha256,
)

_PRIVATE_ERROR = "archive backup preflight failed"
_REMOTE_URL = (
    "s3+https://account-private.r2.cloudflarestorage.com/"
    "bucket-private/prefix-private/generations/"
    "536b300b-24ff-4dda-a3e9-52fde1154b59"
)
_ROOT_ROWS = (
    {
        "session_id": "claude-private",
        "source_agent": "claude-code",
        "created_at": "2026-08-29T10:00:00Z",
        "message_count": 2,
        "first_message_at": "2026-08-29T10:01:00Z",
        "last_message_at": "2026-08-29T10:02:00Z",
    },
    {
        "session_id": "codex-private",
        "source_agent": "codex-cli",
        "created_at": "2026-08-29T11:00:00Z",
        "message_count": 3,
        "first_message_at": "2026-08-29T11:01:00Z",
        "last_message_at": "2026-08-29T11:02:00Z",
    },
)
_CORPUS_ROW = {
    "sessions": 3,
    "messages": 5,
    "parts": 8,
    "disallowed_sessions": 0,
}
_DUPLICATE_ROW = {
    "logical_duplicate_groups": 0,
    "sessions_in_logical_duplicate_groups": 0,
}


def _corpus_rows(
    *,
    roots: tuple[dict[str, object], ...] = _ROOT_ROWS,
    corpus: dict[str, int] = _CORPUS_ROW,
    duplicates: dict[str, int] = _DUPLICATE_ROW,
) -> list[dict[str, object]]:
    rows = [{"row_kind": "aggregate", **corpus, **duplicates}]
    rows.extend(
        {
            "row_kind": "root",
            **root,
        }
        for root in roots
    )
    return rows


_FAKE_POND = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

record = Path(os.environ["FAKE_POND_RECORD"])
try:
    calls = json.loads(record.read_text(encoding="utf-8"))
except FileNotFoundError:
    calls = []
calls.append(sys.argv)
record.write_text(json.dumps(calls), encoding="utf-8")

if sys.argv[1:] == ["--version"]:
    sys.stdout.write(
        os.environ.get(
            "FAKE_POND_VERSION",
            "pond 0.16.3 (23c7d0e aarch64-macos)\n",
        )
    )
    raise SystemExit(0)

output = Path(sys.argv[sys.argv.index("--output-file") + 1])
phase = "corpus"
query_calls = sum("sql" in call for call in calls)
if os.environ.get("FAKE_MUTATE_AFTER_FIRST_QUERY") == "1" and query_calls > 1:
    payload = os.environ["FAKE_MUTATED_CORPUS_ROWS"]
else:
    payload = os.environ["FAKE_CORPUS_ROWS"]

if os.environ.get("FAKE_FAIL_PHASE") == phase:
    sys.stdout.write("private-child-stdout")
    sys.stderr.write("private-child-stderr")
    raise SystemExit(17)

mode = os.environ.get("FAKE_INVALID_PHASE")
raw = os.environ.get("FAKE_" + phase.upper() + "_RAW")
if raw is not None:
    output.write_text(raw, encoding="utf-8")
elif mode == phase + "-malformed":
    output.write_text("private-invalid-row\n", encoding="utf-8")
elif mode == phase + "-two-rows":
    row = json.loads(payload)
    if isinstance(row, list):
        row = row[0]
    output.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
else:
    rows = json.loads(payload)
    output.write_text("".join(json.dumps(row) + "\n" for row in rows))
"""


@pytest.fixture
def pond_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    binary = tmp_path / "pond-private"
    binary.write_text(textwrap.dedent(_FAKE_POND), encoding="utf-8")
    binary.chmod(0o700)
    local_store = tmp_path / "local-store-private"
    local_store.mkdir(mode=0o700)
    local_config = tmp_path / "local-config-private.toml"
    local_config.write_text("private-local-config", encoding="utf-8")
    local_config.chmod(0o600)
    remote_config = tmp_path / "remote-config-private.toml"
    remote_config.write_text("private-remote-config", encoding="utf-8")
    remote_config.chmod(0o600)
    record = tmp_path / "calls-private.json"
    monkeypatch.setenv("FAKE_POND_RECORD", str(record))
    monkeypatch.setenv("FAKE_CORPUS_ROWS", json.dumps(_corpus_rows()))
    monkeypatch.setenv(
        "FAKE_MUTATED_CORPUS_ROWS",
        json.dumps(
            _corpus_rows(
                corpus={
                    "sessions": 4,
                    "messages": 6,
                    "parts": 9,
                    "disallowed_sessions": 0,
                }
            )
        ),
    )
    monkeypatch.delenv("POND_STORAGE_PATH", raising=False)
    return binary, local_store, local_config, remote_config, record


def _workspace(tmp_path: Path, name: str = "run-private") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _capture_local(pond_environment, tmp_path: Path) -> PondStoreSnapshot:
    binary, store, config, _, _ = pond_environment
    return capture_pond_store_snapshot(
        binary,
        storage=LocalPondStore(store),
        pond_config=config,
        workspace=_workspace(tmp_path),
        timeout_seconds=60,
    )


def _create_corpus_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        "CREATE TABLE sessions (session_id VARCHAR, source_agent VARCHAR, "
        "created_at TIMESTAMPTZ)"
    )
    connection.execute(
        "CREATE TABLE messages (message_id VARCHAR, session_id VARCHAR, "
        "timestamp TIMESTAMPTZ)"
    )
    connection.execute("CREATE TABLE parts (part_id VARCHAR)")


def _pond_ndjson(cursor: duckdb.DuckDBPyConnection) -> bytes:
    columns = [column[0] for column in cursor.description]

    def encode_timestamp(value: object) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError
        return value.isoformat().replace("+00:00", "Z")

    return "".join(
        json.dumps(
            {
                column: value
                for column, value in zip(columns, row, strict=True)
                if value is not None
            },
            default=encode_timestamp,
            separators=(",", ":"),
        )
        + "\n"
        for row in cursor.fetchall()
    ).encode("utf-8")


def _snapshot_from_corpus_ndjson(data: bytes) -> PondStoreSnapshot:
    records, counts = _corpus_snapshot(data)
    return PondStoreSnapshot(
        PondInventory(
            schema_version=1,
            captured_at="2026-08-29T12:00:00Z",
            pond_version="0.16.3",
            records=records,
        ),
        PondCorpusCounts(**counts),
    )


def test_snapshot_contains_root_inventory_and_full_store_safety_counts(
    pond_environment, tmp_path
):
    snapshot = _capture_local(pond_environment, tmp_path)

    assert snapshot.counts == PondCorpusCounts(
        sessions=3,
        messages=5,
        parts=8,
        disallowed_sessions=0,
        logical_duplicate_groups=0,
        sessions_in_logical_duplicate_groups=0,
    )
    assert {row.source_agent for row in snapshot.root_inventory.records} == {
        "claude-code",
        "codex-cli",
    }
    assert len(snapshot.root_inventory.records) == 2


@pytest.mark.parametrize(
    ("kind", "missing"),
    [
        ("aggregate", "sessions"),
        ("aggregate", "logical_duplicate_groups"),
        ("root", "session_id"),
        ("root", "created_at"),
        ("root", "message_count"),
    ],
)
def test_corpus_parser_rejects_missing_required_kind_fields(
    kind: str, missing: str
) -> None:
    rows = _corpus_rows()
    row = next(value for value in rows if value["row_kind"] == kind)
    del row[missing]

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "aggregate-root-field",
        "root-count-field",
        "unknown-kind",
        "unknown-field",
    ],
)
def test_corpus_parser_rejects_union_mixing_and_unknown_fields(mutation: str) -> None:
    rows = _corpus_rows()
    aggregate = rows[0]
    root = rows[1]
    if mutation == "aggregate-root-field":
        aggregate["session_id"] = None
    elif mutation == "root-count-field":
        root["sessions"] = None
    elif mutation == "unknown-kind":
        root["row_kind"] = "private-unknown"
    else:
        root["private-extra"] = None

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


def test_corpus_parser_normalizes_only_the_omitted_empty_root_timestamp_pair() -> None:
    rows = _corpus_rows(
        roots=(
            {
                "session_id": "claude-empty-private",
                "source_agent": "claude-code",
                "created_at": "2026-08-29T10:00:00Z",
                "message_count": 0,
            },
        )
    )

    records, _ = _corpus_snapshot(
        "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
    )

    assert records[0].first_message_at is None
    assert records[0].last_message_at is None


@pytest.mark.parametrize("missing", ["first_message_at", "last_message_at"])
def test_corpus_parser_rejects_one_omitted_root_timestamp(missing: str) -> None:
    rows = _corpus_rows()
    del rows[1][missing]

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


def test_corpus_parser_rejects_omitted_timestamps_for_a_nonempty_root() -> None:
    rows = _corpus_rows()
    del rows[1]["first_message_at"]
    del rows[1]["last_message_at"]

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


@pytest.mark.parametrize("violation", ["aggregate-last", "roots-unsorted"])
def test_corpus_parser_rejects_rows_outside_query_order(violation: str) -> None:
    rows = _corpus_rows()
    if violation == "aggregate-last":
        rows = [*rows[1:], rows[0]]
    else:
        rows = [rows[0], *reversed(rows[1:])]

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


def test_corpus_parser_rejects_multiple_aggregate_rows() -> None:
    rows = _corpus_rows()
    rows.insert(1, dict(rows[0]))

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in rows).encode("utf-8")
        )


def test_corpus_parser_rejects_more_than_the_root_record_cap(monkeypatch) -> None:
    snapshot_module = importlib.import_module("drover.server.archive.pond_snapshot")
    monkeypatch.setattr(snapshot_module, "MAX_INVENTORY_RECORDS", 1)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _corpus_snapshot(
            "".join(json.dumps(value) + "\n" for value in _corpus_rows()).encode(
                "utf-8"
            )
        )


def test_snapshot_uses_exact_sql_and_argument_order_for_local_store(
    pond_environment, tmp_path
):
    binary, store, config, _, record = pond_environment
    workspace = _workspace(tmp_path)

    _capture_local(pond_environment, tmp_path)

    calls = json.loads(record.read_text(encoding="utf-8"))
    executables = [Path(call[0]) for call in calls]
    artifact_directories = {path.parent for path in executables}
    assert len(artifact_directories) == 2
    assert all(path.parent == workspace for path in artifact_directories)
    assert all(
        path.name.startswith(".drover-pond-tool-")
        and stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in artifact_directories
    )
    assert all(
        path.name.startswith(".drover-pond-executable-")
        and stat.S_IMODE(path.stat().st_mode) == 0o500
        for path in executables
    )
    assert calls[0][1:] == ["--version"]
    assert [call[1:] for call in calls[1:]] == [
        [
            "--config-file",
            str(config),
            "--storage-path",
            str(store),
            "sql",
            POND_CORPUS_SNAPSHOT_SQL,
            "--format",
            "ndjson",
            "--output-file",
            str(workspace / "corpus-snapshot.ndjson"),
            "--timeout",
            "60",
        ]
    ]


def test_snapshot_uses_one_corpus_query_when_live_store_changes(
    pond_environment, monkeypatch, tmp_path
) -> None:
    *_, record = pond_environment
    monkeypatch.setenv("FAKE_MUTATE_AFTER_FIRST_QUERY", "1")

    snapshot = _capture_local(pond_environment, tmp_path)

    calls = json.loads(record.read_text(encoding="utf-8"))
    corpus_calls = [call for call in calls if "sql" in call]
    assert len(corpus_calls) == 1
    assert snapshot.counts.sessions == 3


def test_snapshot_calls_progress_callback_for_version_and_corpus_processes(
    pond_environment, monkeypatch, tmp_path
) -> None:
    snapshot_module = importlib.import_module("drover.server.archive.pond_snapshot")
    real_run = snapshot_module.run_pond_process
    callback_labels: list[str] = []
    callback_calls = 0

    def callback() -> None:
        nonlocal callback_calls
        callback_calls += 1

    def observe_callbacks(*args, **kwargs):
        label = kwargs["label"]
        supplied_callback = kwargs["progress_callback"]

        def observed_callback():
            callback_labels.append(label)
            supplied_callback()

        return real_run(*args, **{**kwargs, "progress_callback": observed_callback})

    monkeypatch.setattr(snapshot_module, "run_pond_process", observe_callbacks)
    binary, store, config, _, _ = pond_environment

    capture_pond_store_snapshot(
        binary,
        storage=LocalPondStore(store),
        pond_config=config,
        workspace=_workspace(tmp_path),
        timeout_seconds=60,
        progress_callback=callback,
    )

    assert set(callback_labels) == {"snapshot-version", "corpus-snapshot"}
    assert callback_calls == len(callback_labels)


def test_snapshot_forwards_limits_and_aggregates_both_process_results(
    pond_environment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot_module = importlib.import_module("drover.server.archive.pond_snapshot")
    binary, store, config, _, _ = pond_environment
    limits = ResourceLimits(1024, 2048, 64)
    received_limits: list[ResourceLimits | None] = []

    def run_process(
        received_binary,
        arguments,
        *,
        run_directory,
        label,
        artifact_path=None,
        resource_limits=None,
        **_kwargs,
    ):
        assert received_binary.path == binary
        received_limits.append(resource_limits)
        stdout = Path(run_directory) / f"{label}.stdout"
        stderr = Path(run_directory) / f"{label}.stderr"
        if label == "snapshot-version":
            stdout.write_text(
                "pond 0.16.3 (23c7d0e aarch64-macos)\n",
                encoding="utf-8",
            )
            evidence = (111, 900, 3)
        else:
            assert artifact_path is not None
            Path(artifact_path).write_text(
                "".join(json.dumps(row) + "\n" for row in _corpus_rows()),
                encoding="utf-8",
            )
            Path(artifact_path).chmod(0o600)
            stdout.write_bytes(b"")
            evidence = (333, 444, 5)
        stdout.chmod(0o600)
        stderr.write_bytes(b"")
        stderr.chmod(0o600)
        return PondProcessResult(0, 1, *evidence, stdout, stderr)

    monkeypatch.setattr(snapshot_module, "run_pond_process", run_process)

    with _pin_pond_executable(binary) as executable:
        release = snapshot_module._capture_pond_release(
            executable,
            workspace=_workspace(tmp_path, "release-private"),
            resource_limits=limits,
        )
        snapshot = snapshot_module._capture_pond_store_snapshot(
            executable,
            storage=LocalPondStore(store),
            pond_config=config,
            workspace=_workspace(tmp_path),
            timeout_seconds=60,
            resource_limits=limits,
            release_evidence=release,
        )

    assert received_limits == [limits, limits]
    assert snapshot.resource_evidence == PondResourceEvidence(333, 900, 5)


@pytest.mark.parametrize("phase", ["snapshot-version", "corpus-snapshot"])
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (
            BackupRuntimeError("archive backup local changed"),
            "archive backup local changed",
        ),
        (PondProcessError("resource"), "resource"),
    ],
)
def test_snapshot_preserves_runtime_and_resource_failures_from_both_processes(
    pond_environment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    failure: Exception,
    category: str,
) -> None:
    snapshot_module = importlib.import_module("drover.server.archive.pond_snapshot")
    real_run = snapshot_module.run_pond_process

    def fail_selected(*args, **kwargs):
        if kwargs["label"] == phase:
            raise failure
        return real_run(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_pond_process", fail_selected)

    with pytest.raises(type(failure), match=rf"^{category}$"):
        _capture_local(pond_environment, tmp_path)


def test_corpus_sql_counts_null_source_agent_as_disallowed() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id VARCHAR, source_agent VARCHAR, "
            "created_at TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE messages (message_id VARCHAR, session_id VARCHAR, "
            "timestamp TIMESTAMP)"
        )
        connection.execute("CREATE TABLE parts (part_id VARCHAR)")
        connection.execute("INSERT INTO sessions VALUES ('private', NULL, now())")

        cursor = connection.execute(POND_CORPUS_SNAPSHOT_SQL)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    aggregate = next(row for row in rows if row["row_kind"] == "aggregate")
    assert aggregate["disallowed_sessions"] == 1


def test_unified_corpus_sql_keeps_timestamps_for_the_production_parser() -> None:
    with duckdb.connect() as connection:
        connection.execute("SET TimeZone = 'America/Vancouver'")
        _create_corpus_schema(connection)
        connection.execute("""INSERT INTO sessions VALUES
               ('claude-private', 'claude-code',
                TIMESTAMPTZ '2026-08-29 03:00:00-07:00'),
               ('codex-empty-private', 'codex-cli',
                TIMESTAMPTZ '2026-08-29 04:00:00-07:00')""")
        connection.execute("""INSERT INTO messages VALUES
               ('message-private', 'claude-private',
                TIMESTAMPTZ '2026-08-29 03:01:00-07:00')""")
        data = _pond_ndjson(connection.execute(POND_CORPUS_SNAPSHOT_SQL))

    snapshot = _snapshot_from_corpus_ndjson(data)

    assert snapshot.root_inventory.records == (
        PondInventoryRecord(
            session_id="claude-private",
            source_agent="claude-code",
            created_at="2026-08-29T10:00:00Z",
            message_count=1,
            first_message_at="2026-08-29T10:01:00Z",
            last_message_at="2026-08-29T10:01:00Z",
        ),
        PondInventoryRecord(
            session_id="codex-empty-private",
            source_agent="codex-cli",
            created_at="2026-08-29T11:00:00Z",
            message_count=0,
            first_message_at=None,
            last_message_at=None,
        ),
    )
    assert snapshot.counts == PondCorpusCounts(2, 1, 0, 0, 0, 0)


def test_unified_corpus_sql_preserves_allowed_claude_prefix_agent_identity() -> None:
    with duckdb.connect() as connection:
        _create_corpus_schema(connection)
        connection.execute("""INSERT INTO sessions VALUES
               ('claude-prefix-private', 'claude-code/1.0.123',
                TIMESTAMPTZ '2026-08-29 10:00:00+00:00')""")
        connection.execute("""INSERT INTO messages VALUES
               ('message-prefix-private', 'claude-prefix-private',
                TIMESTAMPTZ '2026-08-29 10:01:00+00:00')""")
        data = _pond_ndjson(connection.execute(POND_CORPUS_SNAPSHOT_SQL))

    snapshot = _snapshot_from_corpus_ndjson(data)

    assert snapshot.root_inventory.records == (
        PondInventoryRecord(
            session_id="claude-prefix-private",
            source_agent="claude-code/1.0.123",
            created_at="2026-08-29T10:00:00Z",
            message_count=1,
            first_message_at="2026-08-29T10:01:00Z",
            last_message_at="2026-08-29T10:01:00Z",
        ),
    )
    assert snapshot.counts == PondCorpusCounts(1, 1, 0, 0, 0, 0)


def test_unified_corpus_sql_constructs_an_empty_snapshot() -> None:
    with duckdb.connect() as connection:
        _create_corpus_schema(connection)
        data = _pond_ndjson(connection.execute(POND_CORPUS_SNAPSHOT_SQL))

    snapshot = _snapshot_from_corpus_ndjson(data)

    assert snapshot.root_inventory.records == ()
    assert snapshot.counts == PondCorpusCounts(0, 0, 0, 0, 0, 0)


def test_snapshot_uses_remote_wrapper_and_remote_config_without_repr_disclosure(
    pond_environment, tmp_path
):
    binary, _, _, remote_config, record = pond_environment
    workspace = _workspace(tmp_path, "remote-run-private")
    storage = RemotePondGeneration(_REMOTE_URL)

    snapshot = capture_pond_store_snapshot(
        binary,
        storage=storage,
        pond_config=remote_config,
        workspace=workspace,
        timeout_seconds=60,
    )

    calls = json.loads(record.read_text(encoding="utf-8"))
    assert all(
        call[call.index("--storage-path") + 1] == _REMOTE_URL for call in calls[1:]
    )
    assert all(
        call[call.index("--config-file") + 1] == str(remote_config)
        for call in calls[1:]
    )
    assert _REMOTE_URL not in repr(storage)
    assert _REMOTE_URL not in repr(snapshot)


@pytest.mark.parametrize(
    "storage",
    [
        Path("/private/not-wrapped"),
        "s3+https://account-private.r2.cloudflarestorage.com/private",
    ],
)
def test_snapshot_rejects_unwrapped_storage_without_disclosing_it(
    storage, pond_environment, tmp_path
):
    binary, _, config, _, _ = pond_environment

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$") as raised:
        capture_pond_store_snapshot(
            binary,
            storage=storage,
            pond_config=config,
            workspace=_workspace(tmp_path),
            timeout_seconds=60,
        )

    assert str(storage) not in str(raised.value)


@pytest.mark.parametrize(
    "mode",
    ["corpus-malformed", "corpus-two-rows"],
)
def test_snapshot_requires_strict_ndjson_without_row_disclosure(
    mode, pond_environment, monkeypatch, tmp_path
):
    monkeypatch.setenv("FAKE_INVALID_PHASE", mode)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$") as raised:
        _capture_local(pond_environment, tmp_path)

    assert "corpus" not in str(raised.value)
    assert "private-invalid-row" not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sessions", True),
        ("messages", 1.5),
        ("parts", "8"),
        ("disallowed_sessions", -1),
    ],
)
def test_snapshot_rejects_non_exact_nonnegative_count_types(
    field, value, pond_environment, monkeypatch, tmp_path
):
    row = dict(_CORPUS_ROW)
    row[field] = value
    monkeypatch.setenv("FAKE_CORPUS_ROWS", json.dumps(_corpus_rows(corpus=row)))

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _capture_local(pond_environment, tmp_path)


def test_snapshot_rejects_duplicate_ndjson_keys(
    pond_environment, monkeypatch, tmp_path
):
    rows = [json.dumps(row) for row in _corpus_rows()]
    rows[0] = rows[0].replace('"sessions": 3', '"sessions": 3, "sessions": 3', 1)
    monkeypatch.setenv(
        "FAKE_CORPUS_RAW",
        "\n".join(rows) + "\n",
    )

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        _capture_local(pond_environment, tmp_path)


@pytest.mark.parametrize(
    "counts",
    [
        (1, 0, 0, 2, 0, 0),
        (1, 0, 0, 0, 0, 2),
        (2, 0, 0, 0, 2, 2),
    ],
)
def test_snapshot_rejects_relationally_impossible_corpus_counts(counts) -> None:
    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        PondCorpusCounts(*counts)


def test_snapshot_sanitizes_child_failures_and_private_locations(
    pond_environment, monkeypatch, tmp_path
):
    _, store, config, _, _ = pond_environment
    monkeypatch.setenv("FAKE_FAIL_PHASE", "corpus")

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$") as raised:
        _capture_local(pond_environment, tmp_path)

    rendered = str(raised.value)
    for private in (store, config, "private-child-stdout", "private-child-stderr"):
        assert str(private) not in rendered


def test_snapshot_requires_owner_only_workspace_and_config(pond_environment, tmp_path):
    binary, store, config, _, _ = pond_environment
    workspace = _workspace(tmp_path)
    workspace.chmod(0o755)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        capture_pond_store_snapshot(
            binary,
            storage=LocalPondStore(store),
            pond_config=config,
            workspace=workspace,
            timeout_seconds=60,
        )

    workspace.chmod(0o700)
    config.chmod(0o644)
    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        capture_pond_store_snapshot(
            binary,
            storage=LocalPondStore(store),
            pond_config=config,
            workspace=workspace,
            timeout_seconds=60,
        )


@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_snapshot_rejects_non_private_local_store_before_pond_runs(
    mode, pond_environment, tmp_path
):
    binary, store, config, _, record = pond_environment
    store.chmod(mode)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        capture_pond_store_snapshot(
            binary,
            storage=LocalPondStore(store),
            pond_config=config,
            workspace=_workspace(tmp_path),
            timeout_seconds=60,
        )

    assert not record.exists()


def test_local_store_wrapper_rejects_wrong_owner_and_symlink(
    pond_environment, monkeypatch
):
    snapshot_module = importlib.import_module("drover.server.archive.pond_snapshot")
    _, store, _, _, _ = pond_environment
    current_user = os.geteuid()
    monkeypatch.setattr(snapshot_module.os, "geteuid", lambda: current_user + 1)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        LocalPondStore(store)

    monkeypatch.setattr(snapshot_module.os, "geteuid", lambda: current_user)
    target = store.with_name("local-store-target-private")
    store.rename(target)
    store.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        LocalPondStore(store)


def test_snapshot_rejects_same_mode_local_store_path_substitution_before_pond_runs(
    pond_environment, tmp_path
):
    binary, store, config, _, record = pond_environment
    storage = LocalPondStore(store)
    moved = store.with_name("moved-local-store-private")
    store.rename(moved)
    store.mkdir(mode=0o700)

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        capture_pond_store_snapshot(
            binary,
            storage=storage,
            pond_config=config,
            workspace=_workspace(tmp_path),
            timeout_seconds=60,
        )

    assert not record.exists()


def test_snapshot_revalidates_local_store_after_release_before_corpus_read(
    pond_environment, tmp_path
):
    binary, store, config, _, record = pond_environment
    storage = LocalPondStore(store)
    moved = store.with_name("moved-during-release-private")
    swapped = False

    def swap_after_release_sample() -> None:
        nonlocal swapped
        if not swapped:
            store.rename(moved)
            store.mkdir(mode=0o700)
            swapped = True

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        capture_pond_store_snapshot(
            binary,
            storage=storage,
            pond_config=config,
            workspace=_workspace(tmp_path),
            timeout_seconds=60,
            progress_callback=swap_after_release_sample,
        )

    calls = json.loads(record.read_text(encoding="utf-8"))
    assert [call[1:] for call in calls] == [["--version"]]


def test_snapshot_artifacts_are_private_and_bounded_to_the_supplied_workspace(
    pond_environment, tmp_path
):
    workspace = _workspace(tmp_path)

    _capture_local(pond_environment, tmp_path)

    names = {path.name for path in workspace.iterdir()}
    assert "pond-inventory.json" in names
    assert "corpus-snapshot.ndjson" in names
    artifact_directories = [
        path
        for path in workspace.iterdir()
        if path.name.startswith(".drover-pond-tool-")
    ]
    assert len(artifact_directories) == 2
    assert all(
        (path in artifact_directories and stat.S_IMODE(path.stat().st_mode) == 0o700)
        or (path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600)
        for path in workspace.iterdir()
    )
    assert all(
        len(children := list(directory.iterdir())) == 1
        and children[0].name.startswith(".drover-pond-executable-")
        and children[0].is_file()
        and stat.S_IMODE(children[0].stat().st_mode) == 0o500
        for directory in artifact_directories
    )


def test_pond_inventory_content_digest_excludes_only_capture_time() -> None:
    inventory = PondInventory(
        schema_version=1,
        captured_at="2026-08-29T12:00:00Z",
        pond_version="0.16.3",
        records=tuple(PondInventoryRecord(**row) for row in reversed(_ROOT_ROWS)),
    )
    later = replace(inventory, captured_at="2026-08-29T13:00:00Z")
    changed_record = replace(inventory.records[0], message_count=4)
    changed = replace(inventory, records=(changed_record, *inventory.records[1:]))

    digest = pond_inventory_content_sha256(inventory)

    assert digest == pond_inventory_content_sha256(later)
    assert digest != pond_inventory_content_sha256(changed)
    assert len(digest) == 64


def test_private_snapshot_dataclasses_do_not_repr_identifiers_or_paths(
    pond_environment, tmp_path
):
    _, store, _, _, _ = pond_environment
    snapshot = _capture_local(pond_environment, tmp_path)

    assert "claude-private" not in repr(snapshot)
    assert "codex-private" not in repr(snapshot)
    assert str(store) not in repr(LocalPondStore(store))
    assert repr(snapshot.counts) == (
        "PondCorpusCounts(sessions=3, messages=5, parts=8, "
        "disallowed_sessions=0, logical_duplicate_groups=0, "
        "sessions_in_logical_duplicate_groups=0)"
    )


def test_snapshot_rejects_root_inventory_larger_than_full_store_counts() -> None:
    counts = PondCorpusCounts(
        sessions=1,
        messages=1,
        parts=1,
        disallowed_sessions=0,
        logical_duplicate_groups=0,
        sessions_in_logical_duplicate_groups=0,
    )

    with pytest.raises(ValueError, match=rf"^{_PRIVATE_ERROR}$"):
        PondStoreSnapshot(
            root_inventory=PondInventory(
                schema_version=1,
                captured_at="2026-08-29T12:00:00Z",
                pond_version="0.16.3",
                records=tuple(PondInventoryRecord(**row) for row in _ROOT_ROWS),
            ),
            counts=counts,
        )
