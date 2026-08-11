"""Tests for the bounded live-session recap prompt."""

from __future__ import annotations

from drover.server.harness.recap_prompt import (
    build_live_recap_prompt,
    normalize_live_recap,
)


def test_prompt_keeps_only_newest_thirty_events_in_chronological_order() -> None:
    events = [
        {"seq": n, "event_type": "user_input", "content_preview": f"event-{n}"}
        for n in range(35)
    ]

    prompt = build_live_recap_prompt(events, template="{turns}")

    assert "event-4" not in prompt
    assert prompt.index("event-5") < prompt.index("event-34")


def test_prompt_orders_shuffled_events_by_sequence_before_bounding() -> None:
    events = [
        {"seq": n, "event_type": "user_input", "content_preview": f"event-{n}"}
        for n in reversed(range(35))
    ]

    prompt = build_live_recap_prompt(events, template="{turns}")

    assert "event-4" not in prompt
    assert prompt.index("event-5") < prompt.index("event-34")


def test_prompt_uses_only_allowed_bounded_content_previews() -> None:
    prompt = build_live_recap_prompt(
        [
            {
                "event_type": "user_input",
                "content_preview": "safe preview",
                "payload": {"content": "unbounded raw payload"},
            },
            {
                "event_type": "status",
                "content_preview": "excluded status",
            },
            {
                "event_type": "tool_result",
                "content_preview": "x" * 501,
            },
        ],
        template="{turns}",
    )

    assert "safe preview" in prompt
    assert "unbounded raw payload" not in prompt
    assert "excluded status" not in prompt
    assert "x" * 501 not in prompt
    assert "x" * 500 in prompt


def test_prompt_redacts_secret_like_content_previews() -> None:
    secret = "sk-" + ("a" * 20)

    prompt = build_live_recap_prompt(
        [
            {
                "event_type": "tool_result",
                "content_preview": f"API_KEY={secret}",
            }
        ],
        template="{turns}",
    )

    assert secret not in prompt
    assert "API_KEY=<redacted>" in prompt


def test_normalize_recap_removes_formatting_and_truncates_at_word_boundary() -> None:
    value = "**Improve previews** while " + ("checking progress " * 20)

    recap = normalize_live_recap(value)

    assert "**" not in recap
    assert len(recap) <= 160
    assert not recap.endswith(" ")


def test_normalize_recap_preserves_plain_text_underscores() -> None:
    recap = normalize_live_recap("Update recap_prompt.py before release")

    assert recap == "Update recap_prompt.py before release"


def test_normalize_recap_keeps_only_the_first_sentence() -> None:
    recap = normalize_live_recap("First sentence. Second sentence.")

    assert recap == "First sentence."


def test_normalize_recap_rejects_an_overlong_single_token() -> None:
    recap = normalize_live_recap("x" * 161)

    assert recap == ""
