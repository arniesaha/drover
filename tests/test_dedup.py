"""Tests for src/drover/dedup.py."""

from drover.dedup import make_dedup_key


def test_same_inputs_produce_same_key():
    a = make_dedup_key(
        timestamp_iso="2026-05-08T10:00:00Z",
        agent_id="macmini-claude",
        session_id="abc-123",
        event_type="user_message",
        content="hello world",
    )
    b = make_dedup_key(
        timestamp_iso="2026-05-08T10:00:00Z",
        agent_id="macmini-claude",
        session_id="abc-123",
        event_type="user_message",
        content="hello world",
    )
    assert a == b


def test_different_timestamps_produce_different_keys():
    a = make_dedup_key("2026-05-08T10:00:00Z", "x", "y", "z", "c")
    b = make_dedup_key("2026-05-08T10:00:01Z", "x", "y", "z", "c")
    assert a != b


def test_content_truncated_at_200_chars():
    short = "x" * 200
    long = "x" * 500
    # Same first 200 chars → same key
    assert make_dedup_key("t", "a", "s", "e", short) == make_dedup_key(
        "t", "a", "s", "e", long
    )


def test_handles_none_inputs():
    """Cloud Function calls this with None when fields missing."""
    key = make_dedup_key(None, None, None, None, None)
    assert isinstance(key, str)
    assert len(key) == 64  # sha256 hex


def test_returns_64_char_hex():
    key = make_dedup_key("t", "a", "s", "e", "c")
    assert len(key) == 64
    int(key, 16)  # raises if not valid hex
