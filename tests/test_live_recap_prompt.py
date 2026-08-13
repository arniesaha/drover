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


# --- Dropping the "The user ..." opener -----------------------------------
#
# Inbox cards get two lines. Every recap opening with the same three words
# spends a third of the first line saying nothing that distinguishes one
# session from another -- the reader already knows whose sessions these are.


def test_normalize_recap_drops_the_user_subject_and_its_auxiliary() -> None:
    recap = normalize_live_recap(
        "The user is repairing harness authentication sign-in flows"
    )

    assert recap == "Repairing harness authentication sign-in flows"


def test_normalize_recap_drops_a_bare_user_subject() -> None:
    recap = normalize_live_recap(
        "The user deploys and validates codex-native OTLP tracing"
    )

    assert recap == "Deploys and validates codex-native OTLP tracing"


def test_normalize_recap_drops_the_subject_before_a_perfect_auxiliary() -> None:
    recap = normalize_live_recap("The user has successfully deployed the fleet")

    assert recap == "Successfully deployed the fleet"


def test_normalize_recap_drops_the_subject_without_a_leading_article() -> None:
    recap = normalize_live_recap("User is investigating a flaky test")

    assert recap == "Investigating a flaky test"


def test_normalize_recap_keeps_a_possessive_user_reference() -> None:
    """Only the sentence subject goes; "the user's X" is the actual topic."""
    recap = normalize_live_recap("The user's credentials expired mid-deploy")

    assert recap == "The user's credentials expired mid-deploy"


def test_normalize_recap_keeps_user_when_it_is_not_the_subject() -> None:
    recap = normalize_live_recap("Adding a user lookup to the pairing flow")

    assert recap == "Adding a user lookup to the pairing flow"


def test_normalize_recap_does_not_strip_a_main_verb_it_mistook_for_auxiliary() -> None:
    """ "wants" is what the sentence is about; dropping it leaves a fragment."""
    recap = normalize_live_recap("The user wants to retire the legacy gateway")

    assert recap == "Wants to retire the legacy gateway"


def test_normalize_recap_leaves_nothing_behind_when_the_subject_is_all_there_is() -> (
    None
):
    assert normalize_live_recap("The user is") == ""


def test_stored_recaps_are_cleaned_when_served(tmp_path) -> None:
    """Recaps are normalized on write, so anything already stored keeps its
    subject. Cleaning on the way out means the fix is retroactive instead of
    waiting for every session to be re-recapped."""
    from datetime import datetime, timezone

    from drover.server.harness.recap_jobs import LiveRecap
    from drover.server.metrics import _harness_session_dict

    class _Session:
        def __init__(self) -> None:
            self.session_id = "harness-1"
            self.started_at = None
            self.updated_at = None
            self.ended_at = None
            self.last_activity = None

    item = _harness_session_dict(
        _Session(),
        preview=None,
        recap=LiveRecap(
            session_id="harness-1",
            text="The user is repairing harness authentication sign-in flows",
            source_seq=7,
            generated_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            generator_model=None,
        ),
    )

    assert item["recap"] == "Repairing harness authentication sign-in flows"
