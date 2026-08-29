"""Bounded, local-only Pond inventory export contract."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import stat
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from drover.server.archive import pond_inventory as pond_inventory_module
from drover.server.archive.inventory import load_pond_inventory
from drover.server.archive.pond_inventory import (
    POND_INVENTORY_PREFLIGHT_SQL,
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
import signal
import stat
import subprocess
import sys
import time

record_path = Path(os.environ["FAKE_POND_RECORD"])
try:
    calls = json.loads(record_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    calls = []
calls.append({"argv": sys.argv, "storage_env": os.environ.get("POND_STORAGE_PATH")})
record_path.write_text(json.dumps(calls), encoding="utf-8")

def update_call(**values):
    current = json.loads(record_path.read_text(encoding="utf-8"))
    current[-1].update(values)
    record_path.write_text(json.dumps(current), encoding="utf-8")

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
sql = sys.argv[sys.argv.index("sql") + 1]
if "worst_case_ndjson_bytes" in sql:
    preflight_mode = os.environ.get("FAKE_PREFLIGHT_MODE", "success")
    if preflight_mode == "oversize_output":
        output.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
        time.sleep(30)
    elif preflight_mode == "invalid":
        output.write_text("not-json\n", encoding="utf-8")
    elif preflight_mode == "two_rows":
        row = {"row_count": 1, "worst_case_ndjson_bytes": 512}
        output.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    else:
        row = {
            "row_count": json.loads(os.environ.get("FAKE_PREFLIGHT_ROWS", "2")),
            "worst_case_ndjson_bytes": json.loads(
                os.environ.get("FAKE_PREFLIGHT_BYTES", "2048")
            ),
        }
        output.write_text(json.dumps(row) + "\n", encoding="utf-8")
    raise SystemExit(0)
elif mode == "timeout":
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
elif mode == "swap_output_parent":
    final_parent = Path(os.environ["FAKE_FINAL_OUTPUT_PARENT"])
    moved_parent = Path(os.environ["FAKE_MOVED_OUTPUT_PARENT"])
    final_parent.rename(moved_parent)
    final_parent.symlink_to(
        Path(os.environ["FAKE_OUTPUT_PARENT_TARGET"]),
        target_is_directory=True,
    )
    update_call(output_parent_swapped=True)
    output.write_text(os.environ["FAKE_NDJSON"], encoding="utf-8")
elif mode == "relocate_output_parent_into_store":
    final_parent = Path(os.environ["FAKE_FINAL_OUTPUT_PARENT"])
    relocated_parent = Path(os.environ["FAKE_RELOCATED_OUTPUT_PARENT"])
    final_parent.rename(relocated_parent)
    final_parent.symlink_to(relocated_parent, target_is_directory=True)
    update_call(output_parent_relocated=True)
    output.write_text(os.environ["FAKE_NDJSON"], encoding="utf-8")
elif mode == "mutate_store":
    storage = Path(sys.argv[sys.argv.index("--storage-path") + 1])
    relative = os.environ.get("FAKE_MUTATE_RELATIVE", "manifest.bin")
    target = storage / relative
    target.write_bytes(target.read_bytes() + b"-backfilled")
    (storage / "pond-backfill.created").write_bytes(b"created by pond")
    update_call(
        snapshot_mutated=True,
        snapshot_file_mode=stat.S_IMODE(target.stat().st_mode),
    )
    output.write_text(os.environ["FAKE_NDJSON"], encoding="utf-8")
elif mode == "orphan_descendant":
    child_code = r'''import os
from pathlib import Path
import signal
import sys
import time

pid_path = Path(os.environ["FAKE_DESCENDANT_PID"])
marker_path = Path(os.environ["FAKE_DESCENDANT_MARKER"])

def terminate(_signum, _frame):
    marker_path.write_text("terminated", encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
pid_path.write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
'''
    subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ,
    )
    pid_path = Path(os.environ["FAKE_DESCENDANT_PID"])
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    raise SystemExit(0)
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
    control = tmp_path / "fake-control"
    control.mkdir()
    record = control / "calls.json"
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


def _stable_metadata(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_export_uses_exact_pinned_cli_contract_and_private_staging_target(
    fake_pond, tmp_path
):
    binary, local_store, _ = fake_pond
    output = tmp_path / "pond-inventory.json"

    inventory, calls = _export(fake_pond, output)

    preflight_path = Path(calls[1]["argv"][calls[1]["argv"].index("--output-file") + 1])
    export_path = Path(calls[2]["argv"][calls[2]["argv"].index("--output-file") + 1])
    snapshot_path = Path(calls[1]["argv"][2])
    assert calls[0]["argv"] == [str(binary), "--version"]
    assert calls[1]["argv"] == [
        str(binary),
        "--storage-path",
        str(snapshot_path),
        "sql",
        POND_INVENTORY_PREFLIGHT_SQL,
        "--format",
        "ndjson",
        "--output-file",
        str(preflight_path),
        "--timeout",
        "60",
    ]
    assert calls[2]["argv"] == [
        str(binary),
        "--storage-path",
        str(snapshot_path),
        "sql",
        POND_INVENTORY_SQL,
        "--format",
        "ndjson",
        "--output-file",
        str(export_path),
        "--timeout",
        "60",
    ]
    assert snapshot_path != local_store
    assert calls[2]["argv"][2] == str(snapshot_path)
    assert export_path != output
    assert not snapshot_path.exists()
    assert not preflight_path.exists()
    assert not export_path.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert load_pond_inventory(output) == inventory


@pytest.mark.parametrize("relative_binary", ["./pond", "pond"])
def test_export_executes_the_validated_relative_binary_not_a_path_namesake(
    relative_binary, fake_pond, monkeypatch, tmp_path
):
    binary, local_store, record = fake_pond
    path_bin = tmp_path / "hostile-path"
    path_bin.mkdir()
    namesake_marker = tmp_path / "path-namesake-ran"
    namesake = path_bin / "pond"
    namesake.write_text(
        '#!/bin/sh\nprintf namesake > "$PATH_NAMESAKE_MARKER"\n'
        "printf 'pond 9.9.9\\n'\n",
        encoding="utf-8",
    )
    namesake.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "relative-pond-inventory.json"
    environment = _environment(
        record,
        PATH=str(path_bin) + os.pathsep + os.environ["PATH"],
        PATH_NAMESAKE_MARKER=str(namesake_marker),
    )

    inventory = export_pond_inventory(
        Path(relative_binary),
        output,
        storage_path=local_store,
        env=environment,
    )

    calls = json.loads(record.read_text(encoding="utf-8"))
    assert inventory.pond_version == "0.16.3"
    assert [call["argv"][0] for call in calls] == [str(binary.resolve())] * 3
    assert not namesake_marker.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


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


def test_export_accepts_the_pinned_release_version_detail(fake_pond, tmp_path):
    inventory, _ = _export(
        fake_pond,
        tmp_path / "pond.json",
        FAKE_POND_VERSION="pond 0.16.3 (23c7d0e x86_64-linux)\n",
    )

    assert inventory.pond_version == "0.16.3"


@pytest.mark.parametrize(
    "version",
    [
        "pond 0.16.2\n",
        "pond 0.16.30\n",
        "pond 0.16.3-dev\n",
        "pond 0.16.3 (0000000 x86_64-linux)\n",
        "pond 0.16.3 (23c7d0e unknown-linux)\n",
        "pond 0.16.3 (23c7d0e x86_64-linux) extra\n",
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


@pytest.mark.parametrize("output_kind", ["equal", "inside", "symlink-alias"])
def test_export_refuses_output_in_original_store_before_temporary_work(
    fake_pond, monkeypatch, tmp_path, output_kind
):
    binary, local_store, record = fake_pond
    sentinel = local_store / "opaque-private-store-data"
    sentinel.write_bytes(b"unchanged")
    before_entries = tuple(sorted(path.name for path in local_store.iterdir()))
    before_store_metadata = _stable_metadata(local_store)
    before_sentinel_metadata = _stable_metadata(sentinel)
    alias = tmp_path / "existing-parent-alias"
    if output_kind == "equal":
        output = local_store
    elif output_kind == "inside":
        output = local_store / "private-inventory.json"
    else:
        alias.symlink_to(local_store, target_is_directory=True)
        output = alias / "private-inventory.json"

    def unexpected_temporary_directory(*_args, **_kwargs):
        raise AssertionError("unsafe output reached temporary snapshot setup")

    monkeypatch.setattr(
        pond_inventory_module.tempfile,
        "TemporaryDirectory",
        unexpected_temporary_directory,
    )

    with pytest.raises(ValueError, match=r"^pond inventory output$") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(record),
        )

    assert not record.exists()
    assert tuple(sorted(path.name for path in local_store.iterdir())) == before_entries
    assert sentinel.read_bytes() == b"unchanged"
    assert _stable_metadata(local_store) == before_store_metadata
    assert _stable_metadata(sentinel) == before_sentinel_metadata
    assert str(local_store) not in str(raised.value)
    assert str(output) not in str(raised.value)


def test_export_preserves_exclusive_writer_refusal_for_external_dangling_symlink(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    target = tmp_path / "must-not-be-created.json"
    output = tmp_path / "dangling-output-symlink.json"
    output.symlink_to(target)

    with pytest.raises(ValueError, match="output") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(record),
        )

    assert output.is_symlink()
    assert not target.exists()
    assert str(output) not in str(raised.value)
    assert str(target) not in str(raised.value)


def test_export_sanitizes_output_symlink_loop_before_temporary_work(
    fake_pond, monkeypatch, tmp_path
):
    binary, local_store, record = fake_pond
    output = tmp_path / "private-output-loop"
    output.symlink_to(output)

    def unexpected_temporary_directory(*_args, **_kwargs):
        raise AssertionError("invalid output reached temporary snapshot setup")

    monkeypatch.setattr(
        pond_inventory_module.tempfile,
        "TemporaryDirectory",
        unexpected_temporary_directory,
    )

    with pytest.raises(ValueError, match=r"^pond inventory output$") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(record),
        )

    assert not record.exists()
    assert output.is_symlink()
    assert str(output) not in str(raised.value)


def test_export_fails_closed_if_external_output_parent_is_swapped_to_store(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    output_parent = tmp_path / "initially-external-output"
    output_parent.mkdir()
    moved_parent = tmp_path / "moved-original-output"
    output = output_parent / "private-inventory.json"

    with pytest.raises(ValueError, match=r"^pond inventory output$") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(
                record,
                FAKE_SQL_MODE="swap_output_parent",
                FAKE_FINAL_OUTPUT_PARENT=str(output_parent),
                FAKE_MOVED_OUTPUT_PARENT=str(moved_parent),
                FAKE_OUTPUT_PARENT_TARGET=str(local_store),
            ),
        )

    assert output_parent.is_symlink()
    assert output_parent.resolve() == local_store
    assert not (local_store / output.name).exists()
    assert not (moved_parent / output.name).exists()
    assert not output.exists()
    message = str(raised.value)
    for private_path in (local_store, output_parent, moved_parent, output):
        assert str(private_path) not in message


def test_export_fails_closed_if_pinned_output_parent_is_relocated_into_store(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    output_parent = tmp_path / "initially-external-output"
    output_parent.mkdir()
    relocated_parent = local_store / "relocated-output-parent"
    output = output_parent / "private-inventory.json"

    with pytest.raises(ValueError, match=r"^pond inventory output$") as raised:
        export_pond_inventory(
            binary,
            output,
            storage_path=local_store,
            env=_environment(
                record,
                FAKE_SQL_MODE="relocate_output_parent_into_store",
                FAKE_FINAL_OUTPUT_PARENT=str(output_parent),
                FAKE_RELOCATED_OUTPUT_PARENT=str(relocated_parent),
            ),
        )

    assert output_parent.is_symlink()
    assert output_parent.resolve() == relocated_parent
    assert not (relocated_parent / output.name).exists()
    assert not output.exists()
    message = str(raised.value)
    for private_path in (local_store, output_parent, relocated_parent, output):
        assert str(private_path) not in message


def test_pond_mutations_are_confined_to_a_private_store_snapshot(fake_pond, tmp_path):
    _, local_store, _ = fake_pond
    manifest = local_store / "manifest.bin"
    manifest.write_bytes(b"original opaque pond bytes")
    manifest.chmod(0o644)
    original_bytes = manifest.read_bytes()
    original_file_metadata = _stable_metadata(manifest)
    original_dir_metadata = _stable_metadata(local_store)

    _, calls = _export(
        fake_pond,
        tmp_path / "pond.json",
        FAKE_SQL_MODE="mutate_store",
    )

    snapshot_path = Path(calls[1]["argv"][2])
    assert calls[2]["snapshot_mutated"] is True
    assert calls[2]["snapshot_file_mode"] == 0o600
    assert not snapshot_path.exists()
    assert manifest.read_bytes() == original_bytes
    assert _stable_metadata(manifest) == original_file_metadata
    assert _stable_metadata(local_store) == original_dir_metadata
    assert not (local_store / "pond-backfill.created").exists()


@pytest.mark.parametrize("kind", ["root_symlink", "child_symlink", "fifo"])
def test_snapshot_refuses_symlinks_and_special_files_before_pond_starts(
    fake_pond, tmp_path, kind
):
    binary, local_store, record = fake_pond
    storage_path = local_store
    if kind == "root_symlink":
        storage_path = tmp_path / "linked-store"
        storage_path.symlink_to(local_store, target_is_directory=True)
    elif kind == "child_symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        (local_store / "linked-file").symlink_to(outside)
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(local_store / "special-fifo")

    with pytest.raises(ValueError, match="snapshot|storage") as raised:
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=storage_path,
            env=_environment(record),
        )

    assert not record.exists()
    assert str(storage_path) not in str(raised.value)


def test_snapshot_fails_closed_if_a_source_file_changes_during_copy(
    fake_pond, tmp_path, monkeypatch
):
    binary, local_store, record = fake_pond
    source = local_store / "racy.bin"
    source.write_bytes(b"a" * (256 * 1024))
    monkeypatch.setattr(pond_inventory_module, "_COPY_CHUNK_BYTES", 1)
    # The watcher globs the process temp root for the snapshot directory.
    # Under xdist a sibling test's snapshot lands in the shared system root
    # and both the watcher and the final baseline assertion see it, so each
    # test gets a private root instead.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "snapshot-root"))
    (tmp_path / "snapshot-root").mkdir()
    temporary_root = Path(tempfile.gettempdir())
    baseline = set(temporary_root.glob("drover-pond-inventory-*"))
    changed = threading.Event()

    def mutate_during_copy() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for candidate in (
                set(temporary_root.glob("drover-pond-inventory-*")) - baseline
            ):
                copied = candidate / "pond-store" / "racy.bin"
                try:
                    if copied.stat().st_size > 0:
                        source.write_bytes(b"changed during snapshot")
                        changed.set()
                        return
                except FileNotFoundError:
                    pass
            time.sleep(0.001)

    racer = threading.Thread(target=mutate_during_copy, daemon=True)
    racer.start()
    with pytest.raises(ValueError, match="snapshot"):
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=local_store,
            env=_environment(record),
        )
    racer.join(timeout=6)

    assert changed.is_set()
    assert not record.exists()
    assert set(temporary_root.glob("drover-pond-inventory-*")) == baseline


def test_snapshot_revalidates_earlier_files_after_the_whole_copy(
    fake_pond, tmp_path, monkeypatch
):
    binary, local_store, record = fake_pond
    early = local_store / "a-early.bin"
    early.write_bytes(b"early original")
    (local_store / "z-slow.bin").write_bytes(b"z" * (256 * 1024))
    monkeypatch.setattr(pond_inventory_module, "_COPY_CHUNK_BYTES", 1)
    # Same isolation as the mid-copy race above: a shared system temp root
    # makes sibling xdist workers visible to the glob and the baseline check.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "snapshot-root"))
    (tmp_path / "snapshot-root").mkdir()
    temporary_root = Path(tempfile.gettempdir())
    baseline = set(temporary_root.glob("drover-pond-inventory-*"))
    changed = threading.Event()

    def mutate_after_early_copy() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for candidate in (
                set(temporary_root.glob("drover-pond-inventory-*")) - baseline
            ):
                copied_early = candidate / "pond-store" / early.name
                copied_slow = candidate / "pond-store" / "z-slow.bin"
                try:
                    if copied_early.exists() and copied_slow.stat().st_size > 0:
                        early.write_bytes(b"changed after early copy")
                        changed.set()
                        return
                except FileNotFoundError:
                    pass
            time.sleep(0.001)

    racer = threading.Thread(target=mutate_after_early_copy, daemon=True)
    racer.start()
    with pytest.raises(ValueError, match="snapshot"):
        export_pond_inventory(
            binary,
            tmp_path / "pond.json",
            storage_path=local_store,
            env=_environment(record),
        )
    racer.join(timeout=6)

    assert changed.is_set()
    assert not record.exists()
    assert set(temporary_root.glob("drover-pond-inventory-*")) == baseline


@pytest.mark.parametrize(
    ("rows", "size"),
    [(100_001, 1024), (1, 32 * 1024 * 1024 + 1)],
)
def test_preflight_refuses_over_limit_inventory_before_main_export(
    fake_pond, tmp_path, rows, size
):
    _, _, record = fake_pond

    with pytest.raises(ValueError, match="preflight"):
        _export(
            fake_pond,
            tmp_path / "pond.json",
            FAKE_PREFLIGHT_ROWS=str(rows),
            FAKE_PREFLIGHT_BYTES=str(size),
        )

    calls = json.loads(record.read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert calls[1]["argv"][calls[1]["argv"].index("sql") + 1] == (
        POND_INVENTORY_PREFLIGHT_SQL
    )


def test_preflight_allows_exact_row_and_byte_limits(fake_pond, tmp_path):
    _, calls = _export(
        fake_pond,
        tmp_path / "pond.json",
        FAKE_PREFLIGHT_ROWS="100000",
        FAKE_PREFLIGHT_BYTES=str(32 * 1024 * 1024),
    )

    assert len(calls) == 3
    assert calls[2]["argv"][calls[2]["argv"].index("sql") + 1] == POND_INVENTORY_SQL


@pytest.mark.parametrize(
    "values",
    [
        {"FAKE_PREFLIGHT_MODE": "invalid"},
        {"FAKE_PREFLIGHT_MODE": "two_rows"},
        {"FAKE_PREFLIGHT_ROWS": "true"},
        {"FAKE_PREFLIGHT_BYTES": "-1"},
        {"FAKE_PREFLIGHT_MODE": "oversize_output"},
    ],
)
def test_preflight_output_is_bounded_and_strictly_parsed(fake_pond, tmp_path, values):
    _, _, record = fake_pond

    with pytest.raises(ValueError, match="preflight|size"):
        _export(fake_pond, tmp_path / "pond.json", **values)

    assert len(json.loads(record.read_text(encoding="utf-8"))) == 2


def test_timeout_cleanup_signals_descendants_after_the_leader_exits(
    fake_pond, tmp_path
):
    binary, local_store, record = fake_pond
    pid_path = tmp_path / "descendant.pid"
    marker_path = tmp_path / "descendant-terminated"
    descendant_pid = None
    try:
        with pytest.raises(ValueError, match="timeout"):
            export_pond_inventory(
                binary,
                tmp_path / "pond.json",
                storage_path=local_store,
                timeout_seconds=5,
                env=_environment(
                    record,
                    FAKE_SQL_MODE="orphan_descendant",
                    FAKE_DESCENDANT_PID=str(pid_path),
                    FAKE_DESCENDANT_MARKER=str(marker_path),
                ),
            )
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while _pid_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert marker_path.read_text(encoding="utf-8") == "terminated"
        assert not _pid_exists(descendant_pid)
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_explicit_storage_path_removes_inherited_remote_selector(fake_pond, tmp_path):
    _, calls = _export(
        fake_pond,
        tmp_path / "pond.json",
        POND_STORAGE_PATH="s3://sensitive-bucket/private-prefix",
    )

    assert calls[0]["storage_env"] is None
    assert calls[1]["storage_env"] is None
    assert calls[2]["storage_env"] is None


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
    assert calls[2]["argv"][-2:] == ["--timeout", "5"]


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
    assert len(json.loads(record.read_text(encoding="utf-8"))) == 3


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

    assert len(json.loads(record.read_text(encoding="utf-8"))) == 3
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


def test_export_normalizes_omitted_null_timestamp_keys_for_an_empty_row(
    fake_pond, tmp_path
):
    row = dict(_DEFAULT_ROWS[1])
    del row["first_message_at"]
    del row["last_message_at"]

    inventory, _ = _export(
        fake_pond,
        tmp_path / "pond.json",
        FAKE_NDJSON=_ndjson((row,)),
    )

    assert inventory.records[0].message_count == 0
    assert inventory.records[0].first_message_at is None
    assert inventory.records[0].last_message_at is None


@pytest.mark.parametrize("missing_timestamp", ["first_message_at", "last_message_at"])
def test_export_rejects_only_one_omitted_timestamp_key_for_an_empty_row(
    fake_pond, tmp_path, missing_timestamp
):
    row = dict(_DEFAULT_ROWS[1])
    del row[missing_timestamp]

    with pytest.raises(ValueError, match="columns"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))


def test_export_rejects_omitted_timestamp_keys_for_a_nonempty_row(fake_pond, tmp_path):
    row = dict(_DEFAULT_ROWS[0])
    del row["first_message_at"]
    del row["last_message_at"]

    with pytest.raises(ValueError, match="columns"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))


@pytest.mark.parametrize(
    "missing_column",
    ["session_id", "source_agent", "created_at", "message_count"],
)
def test_export_rejects_every_other_missing_required_column(
    fake_pond, tmp_path, missing_column
):
    row = dict(_DEFAULT_ROWS[1])
    del row[missing_column]

    with pytest.raises(ValueError, match="columns"):
        _export(fake_pond, tmp_path / "pond.json", FAKE_NDJSON=_ndjson((row,)))


def test_export_rejects_an_extra_column(fake_pond, tmp_path):
    row = dict(_DEFAULT_ROWS[0])
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
    control = tmp_path / "fake-control"
    control.mkdir()
    record = control / "calls.json"

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


def test_pond_inventory_preflight_sql_returns_only_conservative_aggregates():
    assert POND_INVENTORY_PREFLIGHT_SQL == """WITH inventory AS (
    SELECT s.session_id, s.source_agent, s.created_at,
           count(m.message_id) AS message_count,
           min(m.timestamp) AS first_message_at,
           max(m.timestamp) AS last_message_at
    FROM sessions s
    LEFT JOIN messages m ON m.session_id = s.session_id
    WHERE s.source_agent IN ('claude-code', 'codex-cli')
    GROUP BY s.session_id, s.source_agent, s.created_at
    ORDER BY s.source_agent, s.session_id
    LIMIT 100001
)
SELECT count(*) AS row_count,
       coalesce(sum(
           117
           + 6 * coalesce(octet_length(session_id), 4)
           + 6 * coalesce(octet_length(source_agent), 4)
           + 6 * coalesce(octet_length(CAST(created_at AS VARCHAR)), 4)
           + octet_length(CAST(message_count AS VARCHAR))
           + CASE WHEN first_message_at IS NULL THEN 0
                  ELSE 2 + 6 * octet_length(CAST(first_message_at AS VARCHAR)) END
           + CASE WHEN last_message_at IS NULL THEN 0
                  ELSE 2 + 6 * octet_length(CAST(last_message_at AS VARCHAR)) END
       ), 0) AS worst_case_ndjson_bytes
FROM inventory"""


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
