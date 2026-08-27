"""One vocabulary and one arithmetic over three providers that agree on neither.

The payload shapes here are the ones in the live store, not invented: codex
puts a running total on a `turn_complete` status, claude-code puts a
per-message delta on assistant and tool events, deepseek uses camelCase and
carries no message identity at all.
"""

from __future__ import annotations

from drover.server.harness.usage import TokenTotals, session_totals


def _claude_event(native_event_id, *, inp, out, cache_read=0, cache_write=0):
    return {
        "payload": {
            "model": "claude-opus-5",
            "native_event_id": native_event_id,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "cache_creation": {"ephemeral_5m_input_tokens": 0},
                "inference_geo": "not_available",
            },
        }
    }


def _codex_event(*, inp, out, cached=0):
    return {
        "payload": {
            "awaiting": None,
            "turn_complete": True,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cached_input_tokens": cached,
                "reasoning_output_tokens": 0,
            },
        }
    }


def _deepseek_event(*, inp, out):
    return {
        "payload": {
            "native_session_id": "s",
            "thinking": "",
            "usage": {"inputTokens": inp, "outputTokens": out},
        }
    }


def test_a_cumulative_provider_is_read_not_summed():
    """codex reports a running total; summing it overcounted ~5x."""
    events = [
        _codex_event(inp=264_624, out=3_242),
        _codex_event(inp=485_949, out=4_156),
        _codex_event(inp=18_884_987, out=42_294),
    ]
    totals = session_totals("codex", events)

    assert totals.input_tokens == 18_884_987
    assert totals.output_tokens == 42_294
    assert totals.exact is True


def test_a_per_message_provider_is_summed():
    events = [
        _claude_event("a-0", inp=2, out=121, cache_read=62_735),
        _claude_event("a-1", inp=2, out=16, cache_read=63_000),
    ]
    totals = session_totals("claude-code", events)

    assert totals.input_tokens == 4
    assert totals.output_tokens == 137
    assert totals.cache_read_tokens == 125_735
    assert totals.exact is True


def test_a_redelivered_event_is_counted_once():
    """The sum has to be idempotent; re-delivery is routine (drover#280)."""
    once = session_totals("claude-code", [_claude_event("a-0", inp=2, out=121)])
    twice = session_totals(
        "claude-code",
        [_claude_event("a-0", inp=2, out=121), _claude_event("a-0", inp=2, out=121)],
    )

    assert twice == once


def test_a_provider_with_no_message_identity_is_not_exact():
    """deepseek echoes a request on consecutive events and offers no id."""
    totals = session_totals(
        "deepseek-harness",
        [_deepseek_event(inp=7760, out=10), _deepseek_event(inp=7760, out=10)],
    )

    assert totals.input_tokens == 15_520, "no identity means no dedupe"
    assert totals.exact is False, "so the total is an upper bound, and says so"


def test_a_provider_that_reports_nothing_reports_none_not_zero():
    """A session nothing was measured for must not read as a free session."""
    totals = session_totals("agy", [{"payload": {"text": "hi"}}, {"payload": {}}])

    assert totals == TokenTotals()
    assert totals.observed is False
    assert totals.input_tokens is None
    assert totals.billable_input is None


def test_the_input_side_is_not_input_tokens():
    """Reading input_tokens alone understated one real session by ~10,000x."""
    totals = session_totals(
        "claude-code",
        [
            _claude_event(
                "a-0", inp=19_109, out=8_817, cache_read=197_704_800, cache_write=5_622
            )
        ],
    )

    assert totals.input_tokens == 19_109
    assert totals.billable_input == 197_729_531


def test_a_subagents_own_running_cost_is_not_the_sessions():
    """`{duration_ms, tool_uses, total_tokens}` is task_progress, another scope."""
    events = [
        {
            "payload": {
                "usage": {"duration_ms": 4559, "tool_uses": 1, "total_tokens": 39_865}
            }
        },
        _claude_event("a-0", inp=2, out=121),
    ]
    totals = session_totals("claude-code", events)

    assert totals.input_tokens == 2
    assert totals.output_tokens == 121
