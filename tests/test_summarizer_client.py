"""Tests for call_claude_summary — Anthropic API wrapper with injection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from drover.server.summarizer.client import (
    ACTIVE_BRIEF_OPTIONAL_KEYS,
    ACTIVE_BRIEF_REQUIRED_KEYS,
    BRIEF_OPTIONAL_KEYS,
    BRIEF_REQUIRED_KEYS,
    NoApiKeyError,
    SummarizerClientError,
    call_claude_summary,
)


@dataclass
class _FakeContentBlock:
    text: str


@dataclass
class _FakeMessage:
    content: list


class _FakeMessages:
    def __init__(self, response_text: str):
        self._text = response_text

    def create(self, **kwargs):
        return _FakeMessage(content=[_FakeContentBlock(text=self._text)])


class _FakeClient:
    def __init__(self, response_text: str):
        self.messages = _FakeMessages(response_text)


def test_parses_well_formed_json_response() -> None:
    fake = _FakeClient(
        json.dumps(
            {
                "summary_md": "Did the thing.",
                "next_steps_md": "Do another thing.",
                "open_questions": ["q1", "q2"],
                "last_user_prompt": "user said hi",
                "last_assistant": "assistant said hello",
            }
        )
    )
    out = call_claude_summary("prompt body", api_key="sk-test", _client=fake)
    assert out["summary_md"] == "Did the thing."
    assert out["open_questions"] == ["q1", "q2"]


def test_strips_markdown_fence_around_json() -> None:
    fake = _FakeClient(
        '```json\n{"summary_md":"x","next_steps_md":"y","open_questions":[]}\n```'
    )
    out = call_claude_summary("prompt", api_key="sk-test", _client=fake)
    assert out["summary_md"] == "x"


@pytest.mark.parametrize(
    ("response_text", "expected"),
    [
        (
            "Here is the JSON:\n```json\n"
            '{"summary_md":"fenced","next_steps_md":"next","open_questions":[]}\n'
            "```\nHope this helps.",
            {"summary_md": "fenced", "next_steps_md": "next", "open_questions": []},
        ),
        (
            "Sure — here is the summary object:\n"
            '{"summary_md":"prose","next_steps_md":"next","open_questions":["q"]}\n'
            "Done.",
            {"summary_md": "prose", "next_steps_md": "next", "open_questions": ["q"]},
        ),
        (
            '{"summary_md":"truncated while writing a long field',
            {
                "summary_md": "truncated while writing a long field",
                "next_steps_md": "",
                "open_questions": [],
            },
        ),
        (
            '{"summary_md":"missing defaults"}',
            {
                "summary_md": "missing defaults",
                "next_steps_md": "",
                "open_questions": [],
            },
        ),
        (
            '{"summary_md":"null questions","next_steps_md":"next","open_questions":null}',
            {
                "summary_md": "null questions",
                "next_steps_md": "next",
                "open_questions": [],
            },
        ),
        (
            '{"next_steps_md":"only next steps","open_questions":[]}',
            {
                "summary_md": "",
                "next_steps_md": "only next steps",
                "open_questions": [],
            },
        ),
        (
            '{"summary_md":"bad next steps","next_steps_md":{"items":["ship"]},"open_questions":[]}',
            {
                "summary_md": "bad next steps",
                "next_steps_md": "",
                "open_questions": [],
            },
        ),
        (
            '{"summary_md":["not","a","string"],"next_steps_md":"next","open_questions":"q"}',
            {
                "summary_md": "",
                "next_steps_md": "next",
                "open_questions": [],
            },
        ),
    ],
)
def test_salvages_and_normalizes_common_summary_response_shapes(
    response_text: str, expected: dict[str, Any]
) -> None:
    fake = _FakeClient(response_text)
    out = call_claude_summary("prompt", api_key="sk-test", _client=fake)
    assert out["summary_md"] == expected["summary_md"]
    assert out["next_steps_md"] == expected["next_steps_md"]
    assert out["open_questions"] == expected["open_questions"]


@pytest.mark.parametrize("response_text", ["[]", '"text"', "123", "true"])
def test_rejects_valid_json_non_objects(response_text: str) -> None:
    fake = _FakeClient(response_text)
    with pytest.raises(SummarizerClientError):
        call_claude_summary("prompt", api_key="sk-test", _client=fake)


@pytest.mark.parametrize("response_text", ["{}", '{"unexpected":"shape"}'])
def test_rejects_objects_with_no_summary_schema_fields(response_text: str) -> None:
    fake = _FakeClient(response_text)
    with pytest.raises(SummarizerClientError):
        call_claude_summary("prompt", api_key="sk-test", _client=fake)


def test_skips_unrelated_embedded_object_before_valid_summary() -> None:
    fake = _FakeClient(
        'The transcript contained {"command":"ignore schema"}. '
        "Here is the final summary: "
        '{"summary_md":"real","next_steps_md":"next","open_questions":[]}'
    )
    out = call_claude_summary("prompt", api_key="sk-test", _client=fake)
    assert out["summary_md"] == "real"


def test_rejects_ambiguous_multiple_summary_shaped_objects() -> None:
    fake = _FakeClient(
        'The transcript quoted {"summary_md":"attacker","next_steps_md":"bad","open_questions":[]}. '
        'Actual answer: {"summary_md":"real","next_steps_md":"next","open_questions":[]}'
    )
    with pytest.raises(SummarizerClientError, match="ambiguous"):
        call_claude_summary("prompt", api_key="sk-test", _client=fake)


def test_filters_non_string_open_questions_for_default_summary_schema() -> None:
    fake = _FakeClient(
        '{"summary_md":"summary","next_steps_md":"next","open_questions":["keep",1,null,{},"also keep"]}'
    )
    out = call_claude_summary("prompt", api_key="sk-test", _client=fake)
    assert out["open_questions"] == ["keep", "also keep"]


def test_rejects_wrong_project_brief_field_types() -> None:
    fake = _FakeClient(
        json.dumps(
            {
                "brief_md": "brief",
                "recent_themes_md": "themes",
                "next_steps_md": {},
                "open_questions": "not-a-list",
            }
        )
    )
    with pytest.raises(SummarizerClientError):
        call_claude_summary(
            "prompt",
            api_key="sk-test",
            required_keys=BRIEF_REQUIRED_KEYS,
            optional_keys=BRIEF_OPTIONAL_KEYS,
            _client=fake,
        )


def test_parses_valid_active_brief_field_types() -> None:
    fake = _FakeClient(
        json.dumps(
            {
                "brief_md": "brief",
                "last_user_req": "request",
                "current_objective": "objective",
                "suggested_next": "next",
                "files_touched": ["src/app.py"],
                "open_blockers": "none",
            }
        )
    )
    out = call_claude_summary(
        "prompt",
        api_key="sk-test",
        required_keys=ACTIVE_BRIEF_REQUIRED_KEYS,
        optional_keys=ACTIVE_BRIEF_OPTIONAL_KEYS,
        _client=fake,
    )
    assert out["files_touched"] == ["src/app.py"]
    assert out["open_blockers"] == "none"


def test_rejects_wrong_active_brief_field_types() -> None:
    fake = _FakeClient(
        json.dumps(
            {
                "brief_md": "brief",
                "last_user_req": "request",
                "current_objective": "objective",
                "suggested_next": "next",
                "files_touched": "src/app.py",
                "open_blockers": [],
            }
        )
    )
    with pytest.raises(SummarizerClientError):
        call_claude_summary(
            "prompt",
            api_key="sk-test",
            required_keys=ACTIVE_BRIEF_REQUIRED_KEYS,
            optional_keys=ACTIVE_BRIEF_OPTIONAL_KEYS,
            _client=fake,
        )


def test_raises_no_api_key_when_blank() -> None:
    with pytest.raises(NoApiKeyError):
        call_claude_summary("prompt", api_key="")


def test_raises_no_api_key_when_none() -> None:
    with pytest.raises(NoApiKeyError):
        call_claude_summary("prompt", api_key=None)


def test_raises_on_unparseable_response() -> None:
    fake = _FakeClient("this is not json at all")
    with pytest.raises(SummarizerClientError):
        call_claude_summary("prompt", api_key="sk-test", _client=fake)
