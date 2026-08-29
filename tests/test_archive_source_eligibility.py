"""Bounded, content-free source eligibility receipts."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from drover.server.archive import source_eligibility as eligibility_module


def _write_claude_source(home, session_id: str, rows: list[dict[str, object]]):
    path = home / ".claude/projects/project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_metadata_only_claude_source_produces_content_free_fingerprint_receipt(
    tmp_path,
):
    from drover.server.archive.native_inventory import (
        discover_native_history_inventory,
    )
    from drover.server.archive.source_eligibility import (
        assess_metadata_only_source,
    )

    source = _write_claude_source(
        tmp_path,
        "metadata-only-session",
        [
            {
                "type": "ai-title",
                "sessionId": "metadata-only-session",
                "title": "private title",
            },
            {
                "type": "agent-name",
                "sessionId": "metadata-only-session",
                "agentName": "private agent",
            },
        ],
    )

    receipt = assess_metadata_only_source(
        tmp_path,
        source,
        "host-private",
        assessed_at=datetime(2026, 8, 29, 12, tzinfo=timezone.utc),
    )
    inventory = discover_native_history_inventory(tmp_path, "host-private")

    assert receipt.schema_version == 1
    assert receipt.assessed_at == "2026-08-29T12:00:00Z"
    assert receipt.host_id == "host-private"
    assert receipt.source_agent == "claude-code"
    assert receipt.session_id == "metadata-only-session"
    assert receipt.classification == "source_not_archive_eligible"
    assert receipt.source_fingerprint == inventory.records[0].source_fingerprint
    serialized = json.dumps(receipt.to_wire(), sort_keys=True)
    assert str(source) not in serialized
    assert "private title" not in serialized
    assert "private agent" not in serialized


def test_eligibility_refuses_oversized_source_before_json_parsing(
    monkeypatch, tmp_path
):
    source = tmp_path / ".claude/projects/project/oversized.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 4097)
    parsed: list[object] = []

    def unexpected_parse(value):
        parsed.append(value)
        raise AssertionError("oversized source reached JSON parser")

    monkeypatch.setattr(eligibility_module.json, "loads", unexpected_parse)

    with pytest.raises(ValueError, match=r"^source eligibility input rejected$"):
        eligibility_module.assess_metadata_only_source(tmp_path, source, "host-private")

    assert parsed == []


@pytest.mark.parametrize(
    "row",
    [
        {"type": "user", "message": {"content": "private prompt"}},
        {"type": "ai-title", "content": "private transcript"},
        {"type": "unknown-metadata", "value": "private value"},
    ],
    ids=["message-event", "content-key", "unknown-event"],
)
def test_eligibility_refuses_message_bearing_or_unknown_events(row, tmp_path):
    source = _write_claude_source(tmp_path, "not-metadata-only", [row])

    with pytest.raises(
        ValueError, match=r"^source eligibility not metadata only$"
    ) as raised:
        eligibility_module.assess_metadata_only_source(tmp_path, source, "host-private")

    rendered = str(raised.value)
    assert str(source) not in rendered
    assert "private" not in rendered


@pytest.mark.parametrize("location", ["outside", "symlink"])
def test_eligibility_accepts_only_canonical_nonsymlink_claude_sources(
    location, tmp_path
):
    canonical = _write_claude_source(
        tmp_path,
        "canonical",
        [{"type": "ai-title", "title": "private title"}],
    )
    if location == "outside":
        source = tmp_path / "outside.jsonl"
        source.write_bytes(canonical.read_bytes())
    else:
        source = canonical.with_name("symlink.jsonl")
        source.symlink_to(canonical)

    with pytest.raises(ValueError, match=r"^source eligibility invalid source$"):
        eligibility_module.assess_metadata_only_source(tmp_path, source, "host-private")


def test_eligibility_fails_closed_when_source_changes_during_assessment(
    monkeypatch, tmp_path
):
    original = b'{"type":"ai-title","title":"aaaa"}\n'
    replacement = b'{"type":"ai-title","title":"bbbb"}\n'
    source = _write_claude_source(tmp_path, "racing-source", [])
    source.write_bytes(original)
    before = source.stat()
    time.sleep(0.01)
    real_fstat = eligibility_module.os.fstat
    source_calls = 0

    def replace_before_final_fstat(descriptor):
        nonlocal source_calls
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (before.st_dev, before.st_ino):
            source_calls += 1
            if source_calls == 2:
                with source.open("r+b") as stream:
                    stream.write(replacement)
                    stream.truncate()
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                metadata = real_fstat(descriptor)
        return metadata

    monkeypatch.setattr(eligibility_module.os, "fstat", replace_before_final_fstat)

    with pytest.raises(ValueError, match=r"^source eligibility source changed$"):
        eligibility_module.assess_metadata_only_source(tmp_path, source, "host-private")

    assert source_calls >= 2
