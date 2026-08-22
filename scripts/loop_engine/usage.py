"""Read token usage off a session's own events.

Deliberately a *bound*, not accounting. drover#17 is the accounting, and it is
blocked on drover#270; what this needs to do is stop a loop before it spends
more than it was allowed, which is a weaker and much more achievable job.

The difference matters in one direction only. A cap that overcounts stops the
loop early, which costs an iteration. A cap that undercounts does not stop it
at all. So where the data is ambiguous this errs upward on purpose, and it says
so rather than presenting the number as a total.

Three real payload shapes, taken from a live store rather than from docs:

    claude-code        input_tokens, output_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens
    codex              + cached_input_tokens, cache_write_input_tokens,
                       reasoning_output_tokens
    deepseek-harness   inputTokens, outputTokens          (camelCase)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

_INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens")
_EXTRA_OUTPUT_KEYS = ("reasoning_output_tokens",)
_USAGE_PATHS = (("usage",), ("payload", "usage"), ("message", "usage"))


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Events that carried usage, after de-duplication. Zero of these is not
    #: the same as zero tokens, and the caller has to be able to tell.
    samples: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def observed(self) -> bool:
        return self.samples > 0


def _first_int(source: Mapping[str, Any], keys: Iterable[str]) -> int:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _usage_of(event: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for path in _USAGE_PATHS:
        cursor: Any = event
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, Mapping) else None
        if isinstance(cursor, Mapping) and cursor:
            return cursor
    return None


def from_events(events: Iterable[Mapping[str, Any]]) -> Usage:
    """Sum usage over a session's events, one sample per sequence number.

    De-duplicating on `seq` is necessary but not sufficient. `harness_events`
    holds two rows for the same `seq` on roughly half of all events
    (drover#270), so without this the total is about double. It still
    overcounts where one assistant message's usage is repeated across several
    derived events in a turn, which is the erring-upward this exists to
    tolerate.
    """
    prompt = 0
    completion = 0
    samples = 0
    seen_seq: set[Any] = set()
    for event in events:
        usage = _usage_of(event)
        if usage is None:
            continue
        seq = event.get("seq")
        if seq is not None:
            if seq in seen_seq:
                continue
            seen_seq.add(seq)
        prompt += _first_int(usage, _INPUT_KEYS)
        completion += _first_int(usage, _OUTPUT_KEYS) + _first_int(
            usage, _EXTRA_OUTPUT_KEYS
        )
        samples += 1
    return Usage(prompt_tokens=prompt, completion_tokens=completion, samples=samples)
