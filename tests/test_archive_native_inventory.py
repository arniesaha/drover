"""Local, privacy-safe native source inventory discovery."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from drover.server.archive.inventory import NativeInventory, NativeInventoryRecord
from drover.server.archive.native_inventory import (
    discover_native_history_inventory,
    native_inventory_summary,
)
from drover.server.harness import daemon as harness_daemon


def _write_claude_session(
    home, *, session_id: str, project: str = "project", body: str = ""
):
    path = home / ".claude/projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        body
        or json.dumps(
            {
                "type": "last-prompt",
                "sessionId": session_id,
                "cwd": "/private/project",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_codex_session(home, *, session_id: str):
    path = home / ".codex/sessions/2026/08/28" / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/private/project"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_native_inventory_captures_all_supported_sources_without_paths(tmp_path):
    _write_claude_session(tmp_path, session_id="claude-1")
    _write_codex_session(tmp_path, session_id="019ef2b6-7000-79c3-93c6-039d129b9513")

    inventory = discover_native_history_inventory(
        tmp_path,
        "host-test",
        captured_at=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
    )

    assert [(row.source_agent, row.session_id) for row in inventory.records] == [
        ("claude-code", "claude-1"),
        ("codex-cli", "019ef2b6-7000-79c3-93c6-039d129b9513"),
    ]
    wire = json.dumps(inventory.to_wire())
    assert str(tmp_path) not in wire
    assert ".claude" not in wire
    assert ".codex" not in wire
    assert "/private/project" not in wire
    assert native_inventory_summary(inventory) == {
        "schema_version": 2,
        "captured_sessions": 2,
        "source_copies": 2,
        "duplicate_source_groups": 0,
        "by_harness": {"claude-code": 1, "codex-cli": 1},
    }


@pytest.mark.parametrize("source_agent", ["claude-code", "codex-cli"])
def test_native_inventory_never_parses_large_canonical_transcript_records(
    source_agent, monkeypatch, tmp_path
):
    codex_id = "019ef2b6-7000-79c3-93c6-039d129b9513"
    if source_agent == "claude-code":
        session_id = "claude-large-record"
        path = tmp_path / ".claude/projects/project" / f"{session_id}.jsonl"
    else:
        session_id = codex_id
        path = (
            tmp_path
            / ".codex/sessions/2026/08/28"
            / f"rollout-2026-08-28T12-00-00-{session_id}.jsonl"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"tool_output":"' + b"x" * (8 * 1024 * 1024) + b'"}\n')
    parsed_sizes: list[int] = []
    original_loads = harness_daemon.json.loads

    def record_parse_size(value, *args, **kwargs):
        parsed_sizes.append(len(value))
        if len(value) > 4096:
            return {}
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(harness_daemon.json, "loads", record_parse_size)

    inventory = discover_native_history_inventory(tmp_path, "host-test")

    assert [(row.source_agent, row.session_id) for row in inventory.records] == [
        (source_agent, session_id)
    ]
    assert parsed_sizes == []


def test_native_inventory_fingerprint_invalidates_same_size_rewrite_with_restored_mtime(
    tmp_path,
):
    original_body = b'{"type":"ai-title","title":"aaaa"}\n'
    replacement_body = b'{"type":"ai-title","title":"bbbb"}\n'
    assert len(original_body) == len(replacement_body)
    session = _write_claude_session(
        tmp_path,
        session_id="claude-metadata-only",
        body=original_body.decode("utf-8"),
    )
    before = session.stat()

    first = discover_native_history_inventory(tmp_path, "host-test")
    time.sleep(0.01)
    session.write_bytes(replacement_body)
    os.utime(session, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = discover_native_history_inventory(tmp_path, "host-test")

    first_record = first.records[0]
    second_record = second.records[0]
    assert first.schema_version == 2
    assert second.schema_version == 2
    assert first_record.size_bytes == second_record.size_bytes
    assert first_record.updated_at == second_record.updated_at
    assert len(first_record.source_fingerprint) == 64
    assert first_record.source_fingerprint != second_record.source_fingerprint
    serialized = json.dumps(first.to_wire(), sort_keys=True)
    assert str(session) not in serialized
    assert "aaaa" not in serialized


def test_native_inventory_groups_duplicate_source_sessions_and_uses_latest_mtime(
    tmp_path,
):
    first = _write_claude_session(
        tmp_path, session_id="claude-duplicate", project="one", body="first\n"
    )
    second = _write_claude_session(
        tmp_path, session_id="claude-duplicate", project="two", body="second\n"
    )
    os.utime(first, (1_788_000_000, 1_788_000_000))
    os.utime(second, (1_788_000_123, 1_788_000_123))

    inventory = discover_native_history_inventory(
        tmp_path,
        "host-test",
        captured_at=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
    )

    record = inventory.records[0]
    assert record.source_agent == "claude-code"
    assert record.session_id == "claude-duplicate"
    assert record.updated_at == "2026-08-29T10:42:03.000000000Z"
    assert record.size_bytes == first.stat().st_size + second.stat().st_size
    assert record.source_copies == 2
    assert len(record.source_fingerprint) == 64
    assert native_inventory_summary(inventory) == {
        "schema_version": 2,
        "captured_sessions": 1,
        "source_copies": 2,
        "duplicate_source_groups": 1,
        "by_harness": {"claude-code": 1},
    }


def test_native_inventory_uses_later_fractional_mtime_within_the_same_second(
    tmp_path,
):
    first = _write_claude_session(
        tmp_path, session_id="claude-fractional", project="one", body="first\n"
    )
    second = _write_claude_session(
        tmp_path, session_id="claude-fractional", project="two", body="second\n"
    )
    os.utime(first, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(second, ns=(1_700_000_000_500_000_000, 1_700_000_000_500_000_000))

    inventory = discover_native_history_inventory(tmp_path, "host-test")

    assert inventory.records[0].updated_at == "2023-11-14T22:13:20.500000000Z"


def test_native_inventory_excludes_unsupported_harness_files_and_sorts_records(
    tmp_path,
):
    _write_codex_session(tmp_path, session_id="019ef2b6-7000-79c3-93c6-039d129b9513")
    _write_claude_session(tmp_path, session_id="claude-1")
    unsupported = tmp_path / ".gemini/conversations/other.jsonl"
    unsupported.parent.mkdir(parents=True)
    unsupported.write_text('{"session_id":"other"}\n', encoding="utf-8")

    inventory = discover_native_history_inventory(tmp_path, "host-test")

    assert [(row.source_agent, row.session_id) for row in inventory.records] == [
        ("claude-code", "claude-1"),
        ("codex-cli", "019ef2b6-7000-79c3-93c6-039d129b9513"),
    ]


@pytest.mark.parametrize("host_id", ["", " \t "])
def test_native_inventory_refuses_missing_or_blank_host_id(tmp_path, host_id):
    with pytest.raises(ValueError, match="host_id"):
        discover_native_history_inventory(tmp_path, host_id)


def test_native_inventory_fails_when_a_source_disappears_between_read_and_stat(
    monkeypatch, tmp_path
):
    session = _write_claude_session(tmp_path, session_id="claude-race")
    source_metadata = session.stat()
    original_fstat = harness_daemon.os.fstat
    source_fstat_calls = 0

    def remove_after_capture(descriptor):
        nonlocal source_fstat_calls
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            source_fstat_calls += 1
            if source_fstat_calls == 2:
                session.unlink()
        return metadata

    monkeypatch.setattr(harness_daemon.os, "fstat", remove_after_capture)

    with pytest.raises(ValueError, match="native history discovery") as raised:
        discover_native_history_inventory(tmp_path, "host-test")

    assert str(tmp_path) not in str(raised.value)


def test_native_inventory_refuses_a_source_replaced_after_metadata_read(
    monkeypatch, tmp_path
):
    session = _write_claude_session(
        tmp_path, session_id="claude-original", body='{"sessionId":"claude-original"}\n'
    )
    source_metadata = session.stat()
    original_fstat = harness_daemon.os.fstat
    source_fstat_calls = 0

    def replace_after_capture(descriptor):
        nonlocal source_fstat_calls
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            source_fstat_calls += 1
            if source_fstat_calls == 2:
                replacement = session.with_suffix(".replacement")
                replacement.write_text(
                    '{"sessionId":"claude-replaced"}\n', encoding="utf-8"
                )
                os.replace(replacement, session)
        return metadata

    monkeypatch.setattr(harness_daemon.os, "fstat", replace_after_capture)

    with pytest.raises(ValueError, match="native history discovery") as raised:
        discover_native_history_inventory(tmp_path, "host-test")

    assert str(session) not in str(raised.value)


def test_native_inventory_fails_closed_when_ctime_changes_after_metadata_capture(
    monkeypatch, tmp_path
):
    original_body = b'{"sessionId":"aaaa"}\n'
    replacement_body = b'{"sessionId":"bbbb"}\n'
    assert len(original_body) == len(replacement_body)
    session = _write_claude_session(
        tmp_path,
        session_id="aaaa",
        body=original_body.decode("utf-8"),
    )
    before = session.stat()
    time.sleep(0.01)
    original_fstat = harness_daemon.os.fstat
    source_fstat_calls = 0

    def rewrite_after_metadata_capture(descriptor):
        nonlocal source_fstat_calls
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (before.st_dev, before.st_ino):
            source_fstat_calls += 1
            if source_fstat_calls == 2:
                with session.open("r+b") as stream:
                    stream.write(replacement_body)
                    stream.truncate()
                os.utime(session, ns=(before.st_atime_ns, before.st_mtime_ns))
        return metadata

    monkeypatch.setattr(harness_daemon.os, "fstat", rewrite_after_metadata_capture)

    failure = None
    try:
        discover_native_history_inventory(tmp_path, "host-test")
    except ValueError as exc:
        failure = exc

    after = session.stat()
    assert source_fstat_calls >= 2
    assert after.st_ino == before.st_ino
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ctime_ns != before.st_ctime_ns
    assert failure is not None
    assert "native history discovery" in str(failure)
    assert str(session) not in str(failure)
    assert "aaaa" not in str(failure)
    assert "bbbb" not in str(failure)


def test_native_inventory_fails_closed_when_a_source_cannot_be_read(
    monkeypatch, tmp_path
):
    session = _write_claude_session(tmp_path, session_id="claude-unreadable")
    original_open = type(session).open

    def deny_session_read(path, *args, **kwargs):
        if path == session:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(session), "open", deny_session_read)

    with pytest.raises(ValueError, match="native history discovery") as raised:
        discover_native_history_inventory(tmp_path, "host-test")

    assert str(session) not in str(raised.value)


def test_native_inventory_summary_refuses_unsupported_source_agent():
    unsafe_source_agent = "private-untrusted-agent"
    inventory = NativeInventory(
        schema_version=1,
        captured_at="2026-08-28T12:00:00Z",
        host_id="host-test",
        records=(
            NativeInventoryRecord(
                source_agent=unsafe_source_agent,
                session_id="session-test",
                updated_at="2026-08-28T11:00:00Z",
                size_bytes=1,
                source_copies=1,
            ),
        ),
    )

    with pytest.raises(ValueError, match="source_agent") as raised:
        native_inventory_summary(inventory)

    assert unsafe_source_agent not in str(raised.value)


def test_native_inventory_refuses_more_than_one_hundred_thousand_records(tmp_path):
    with pytest.raises(ValueError, match="max_records"):
        discover_native_history_inventory(tmp_path, "host-test", max_records=100_001)
