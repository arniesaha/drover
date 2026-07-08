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
