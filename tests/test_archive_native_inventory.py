"""Local, privacy-safe native source inventory discovery."""

from __future__ import annotations

import json
import os
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
        "schema_version": 1,
        "captured_sessions": 2,
        "source_copies": 2,
        "duplicate_source_groups": 0,
        "by_harness": {"claude-code": 1, "codex-cli": 1},
    }


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

    assert inventory.records == (
        NativeInventoryRecord(
            source_agent="claude-code",
            session_id="claude-duplicate",
            updated_at="2026-08-29T10:42:03.000000000Z",
            size_bytes=first.stat().st_size + second.stat().st_size,
            source_copies=2,
        ),
    )
    assert native_inventory_summary(inventory) == {
        "schema_version": 1,
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
    _write_claude_session(tmp_path, session_id="claude-race")
    original = harness_daemon._jsonl_metadata

    def remove_after_read(path, **kwargs):
        metadata = original(path, **kwargs)
        path.unlink()
        return metadata

    monkeypatch.setattr(harness_daemon, "_jsonl_metadata", remove_after_read)

    with pytest.raises(ValueError, match="native history discovery") as raised:
        discover_native_history_inventory(tmp_path, "host-test")

    assert str(tmp_path) not in str(raised.value)


def test_native_inventory_refuses_a_source_replaced_after_metadata_read(
    monkeypatch, tmp_path
):
    session = _write_claude_session(
        tmp_path, session_id="claude-original", body='{"sessionId":"claude-original"}\n'
    )
    original = harness_daemon._jsonl_metadata

    def replace_after_read(path, **kwargs):
        metadata = original(path, **kwargs)
        replacement = path.with_suffix(".replacement")
        replacement.write_text('{"sessionId":"claude-replaced"}\n', encoding="utf-8")
        os.replace(replacement, path)
        return metadata

    monkeypatch.setattr(harness_daemon, "_jsonl_metadata", replace_after_read)

    with pytest.raises(ValueError, match="native history discovery") as raised:
        discover_native_history_inventory(tmp_path, "host-test")

    assert str(session) not in str(raised.value)


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
