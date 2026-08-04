"""Tests for normalized Meta Harness event previews."""

from __future__ import annotations

from drover.server.harness.events import clean_terminal_preview, normalize_harness_event


def test_terminal_preview_strips_ansi_and_control_bytes() -> None:
    raw = (
        "\x1b[?25h\x1b[22;3H"
        "\x1b[0m\x1b[49m\x1b[K"
        "Give summary of the fork"
        "\x1b[39m\x1b[49m\x1b[0m\x1b[0 q"
    )

    assert clean_terminal_preview(raw) == "Give summary of the fork"


def test_normalized_terminal_output_preview_is_readable() -> None:
    event = normalize_harness_event(
        event_type="terminal.output",
        payload={"text": "\x1b[?25h\x1b[20;2Hhello\x1b[0m\r\n"},
    )

    assert event["normalized_type"] == "assistant_output"
    assert event["normalized_source"] == "inferred_terminal"
    assert event["content_preview"] == "hello"


def test_terminal_input_preview_keeps_command_newline() -> None:
    event = normalize_harness_event(
        event_type="terminal.input",
        harness="shell",
        payload={"text": "echo OK\n"},
    )

    assert event["normalized_type"] == "command"
    assert event["content_preview"] == "echo OK"


def test_structured_event_types_keep_their_semantic_kind() -> None:
    """Structured drivers emit bare names, not dotted ones.

    The prefix rules only ever matched "tool."/"session."/"approval.", so every
    structured event used to collapse to "status" -- the whole chat transcript
    landed in one undifferentiated bucket.
    """
    for event_type in (
        "assistant_output",
        "user_input",
        "tool_action",
        "tool_result",
        "approval_prompt",
        "approval_response",
        "error",
    ):
        event = normalize_harness_event(
            event_type=event_type,
            payload={"text": "x"},
            normalized_source="structured",
        )
        assert event["normalized_type"] == event_type, event_type
        assert event["normalized_source"] == "structured"


def test_unknown_event_type_still_falls_back_to_status() -> None:
    event = normalize_harness_event(event_type="raw", payload={})

    assert event["normalized_type"] == "status"
