"""Normalize provider token usage into one vocabulary with one arithmetic.

Every supported harness reports usage differently, and the differences are not
cosmetic -- they change how the numbers must be combined. Measured against the
live store on 2026-08-27:

    codex             usage rides `status` events carrying `turn_complete`,
                      and is a running session total: one session went
                      264,624 -> 485,949 -> ... -> 18,884,987. Summing it
                      overcounts by roughly five times. Take the last.

    claude-code       usage rides assistant and tool events and is per
                      message, so it is summed. `native_event_id` identifies
                      the event, which makes the sum idempotent under
                      re-delivery -- the case drover#280 made routine.

                      One message can still fan out across several events
                      carrying identical usage; measured at 121 of 8,817
                      output tokens (1.4%) in a 1,637-record session. Those
                      events share the first four groups of the synthesized
                      `native_event_id` and differ only in its trailing
                      counter, but truncating to that prefix would merge
                      genuinely distinct messages the moment the format
                      changes. The bias is left in and named here instead.
                      For a budget cap it errs in the safe direction: a cap
                      that overcounts stops early.

    deepseek-harness  per request, `inputTokens`/`outputTokens`, and the
                      payload carries no message identity at all -- the same
                      request's usage appears on consecutive events with no
                      way to tell it from a genuine repeat. Summed, and
                      reported as not exact.

    agy, gemini,      no usage in any recorded event.
    shell

The cache split is the other trap. For one real claude-code session:

    input_tokens                      19,109
    output_tokens                      8,817
    cache_creation_input_tokens        5,622
    cache_read_input_tokens      197,704,800

`input_tokens` is not the input. Reading it alone -- which is what the Phase 0
loop driver did -- understates the input side by four orders of magnitude, so
the four classes are kept apart rather than collapsed into prompt/completion.

Absent usage is None, never 0 (drover#17). A session nothing was measured for
must not read as a session that cost nothing, because a budgeted loop would
then run against it unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

#: Harnesses whose usage is a running total rather than a per-message delta.
CUMULATIVE_HARNESSES = frozenset({"codex"})

_INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens")
_CACHE_READ_KEYS = ("cache_read_input_tokens", "cached_input_tokens")
_CACHE_WRITE_KEYS = ("cache_creation_input_tokens", "cache_write_input_tokens")
_REASONING_KEYS = ("reasoning_output_tokens",)

_ALL_KEYS = (
    _INPUT_KEYS + _OUTPUT_KEYS + _CACHE_READ_KEYS + _CACHE_WRITE_KEYS + _REASONING_KEYS
)

#: A subagent's own running cost, reported as `{duration_ms, tool_uses,
#: total_tokens}` under `subtype: task_progress`. A different scope from the
#: session, and counting it here would double the work it already reports.
_SUBAGENT_KEYS = frozenset({"duration_ms", "tool_uses", "total_tokens"})


@dataclass(frozen=True)
class TokenTotals:
    """Normalized token counts for one session, or one model within it."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    #: False when the harness gives no way to tell a re-reported record from a
    #: genuine repeat, so the total is an upper bound rather than a count.
    exact: bool = True

    @property
    def observed(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
            )
        )

    @property
    def billable_input(self) -> Optional[int]:
        """Every class that is charged as input, which is not `input_tokens`.

        Cache reads and cache writes are priced differently from fresh input,
        so this is a volume rather than a cost. It exists because the one
        number callers reach for first should not be the one that is 10,000
        times too small.
        """
        parts = [
            value
            for value in (
                self.input_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
            if value is not None
        ]
        return sum(parts) if parts else None


def _int_for(usage: Mapping[str, Any], keys: Iterable[str]) -> Optional[int]:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


#: Where a usage object sits, depending on who handed us the event. Rows read
#: from `harness_events` nest it under `payload`; the harness API's message
#: list has been seen with all three.
_USAGE_PATHS = (("payload", "usage"), ("usage",), ("message", "usage"))


def _usage_holder(event: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The mapping that holds `usage`, so its sibling identity is reachable."""
    for path in _USAGE_PATHS:
        cursor: Any = event
        for key in path[:-1]:
            cursor = cursor.get(key) if isinstance(cursor, Mapping) else None
        if isinstance(cursor, Mapping) and isinstance(cursor.get("usage"), Mapping):
            return cursor
    return None


def _usage_records(
    events: Iterable[Mapping[str, Any]],
) -> list[tuple[str, bool, dict]]:
    """Every usage object worth counting, with what identifies it.

    Two identities, and they do different jobs. `native_event_id` is the
    provider's own event id and is what makes the sum idempotent under
    re-delivery. `seq` is the fallback: `harness_events` used to hold two rows
    per seq for about half of all events (drover#270, fixed in drover#280, but
    the harness API is a separate path), and without collapsing those the
    total roughly doubles.

    The bool says whether a real provider identity was there. Only that
    supports the exactness claim -- deduping on `seq` removes duplicate rows,
    not a provider echoing one request across two sequence numbers.
    """
    records: list[tuple[str, bool, dict]] = []
    for event in events:
        payload = _usage_holder(event)
        if payload is None:
            continue
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        if not any(key in usage for key in _ALL_KEYS):
            # Either the subagent shape or something we do not recognize.
            continue
        if set(usage) <= _SUBAGENT_KEYS:
            continue
        native_id = payload.get("native_event_id")
        if native_id:
            records.append((str(native_id), True, dict(usage)))
            continue
        seq = event.get("seq")
        records.append(("" if seq is None else f"seq:{seq}", False, dict(usage)))
    return records


def _totals_from(usage: Mapping[str, Any], *, exact: bool) -> TokenTotals:
    return TokenTotals(
        input_tokens=_int_for(usage, _INPUT_KEYS),
        output_tokens=_int_for(usage, _OUTPUT_KEYS),
        cache_read_tokens=_int_for(usage, _CACHE_READ_KEYS),
        cache_write_tokens=_int_for(usage, _CACHE_WRITE_KEYS),
        reasoning_tokens=_int_for(usage, _REASONING_KEYS),
        exact=exact,
    )


def session_totals(
    harness: Optional[str], events: Iterable[Mapping[str, Any]]
) -> TokenTotals:
    """Token totals for one session's events, under that harness's arithmetic.

    Returns an unobserved `TokenTotals` -- every field None -- when the harness
    reported nothing, rather than zeros.
    """
    records = _usage_records(events)
    if not records:
        return TokenTotals()

    if (harness or "") in CUMULATIVE_HARNESSES:
        # The last record already includes every one before it.
        return _totals_from(records[-1][2], exact=True)

    exact = all(has_native_id for _, has_native_id, _ in records)
    seen: set[str] = set()
    sums: dict[str, Optional[int]] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }
    for identity, _has_native_id, usage in records:
        if identity:
            if identity in seen:
                continue
            seen.add(identity)
        one = _totals_from(usage, exact=True)
        for field in sums:
            value = getattr(one, field)
            if value is None:
                continue
            sums[field] = (sums[field] or 0) + value
    return TokenTotals(exact=exact, **sums)  # type: ignore[arg-type]


def usage_turn_count(events: Iterable[Mapping[str, Any]]) -> int:
    """How many usage-bearing records the session carried, after dedup."""
    records = _usage_records(events)
    seen: set[str] = set()
    count = 0
    for identity, _has_native_id, _usage in records:
        if identity:
            if identity in seen:
                continue
            seen.add(identity)
        count += 1
    return count
