"""Tests for the Tempo → OTLP relay."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from drover.collect.cursor import CursorStore
from drover.collect.tempo_relay import (
    CURSOR_KEY,
    DEFAULT_LOOKBACK_S,
    _build_traceql,
    backfill,
    build_export_request,
    push_otlp,
    relay_once,
    relay_window,
    trace_json_to_resource_spans,
)

SAMPLE_TRACE_JSON = {
    "batches": [
        {
            "resource": {
                "attributes": [
                    {
                        "key": "service.name",
                        "value": {"stringValue": "agentweave-proxy"},
                    },
                ]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "agentweave"},
                    "spans": [
                        {
                            "traceId": "0123456789abcdef0123456789abcdef",
                            "spanId": "0123456789abcdef",
                            "name": "llm.claude-opus-4-7",
                            "kind": 2,
                            "startTimeUnixNano": "1700000000000000000",
                            "endTimeUnixNano": "1700000001000000000",
                            "attributes": [
                                {
                                    "key": "prov.agent.id",
                                    "value": {"stringValue": "nas-claude"},
                                },
                                {
                                    "key": "gen_ai.usage.input_tokens",
                                    "value": {"intValue": "1234"},
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}


def _state(tmp_path: Path) -> CursorStore:
    return CursorStore(state_dir=tmp_path / "state")


# --- JSON → proto conversion -------------------------------------------------


def test_build_traceql_or_joins_services() -> None:
    q = _build_traceql(["agentweave-proxy", "mux-router"])
    assert (
        q
        == '{ resource.service.name = "agentweave-proxy" || resource.service.name = "mux-router" }'
    )


def test_build_traceql_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _build_traceql([])


def test_trace_json_to_resource_spans_round_trips_fields() -> None:
    rs_list = trace_json_to_resource_spans(SAMPLE_TRACE_JSON)
    assert len(rs_list) == 1
    rs = rs_list[0]
    assert any(
        kv.key == "service.name" and kv.value.string_value == "agentweave-proxy"
        for kv in rs.resource.attributes
    )
    spans = rs.scope_spans[0].spans
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "llm.claude-opus-4-7"
    assert span.start_time_unix_nano == 1700000000000000000
    assert any(
        kv.key == "prov.agent.id" and kv.value.string_value == "nas-claude"
        for kv in span.attributes
    )


def test_trace_json_to_resource_spans_skips_malformed_batch() -> None:
    # ParseError-triggering shape: scopeSpans must be a list, not a scalar.
    bad = {"batches": [{"scopeSpans": 42}, SAMPLE_TRACE_JSON["batches"][0]]}
    rs_list = trace_json_to_resource_spans(bad)
    assert len(rs_list) == 1  # the broken batch was dropped, the good one survives


def test_trace_json_to_resource_spans_handles_missing_batches() -> None:
    assert trace_json_to_resource_spans({}) == []


def test_build_export_request_aggregates() -> None:
    rs = trace_json_to_resource_spans(SAMPLE_TRACE_JSON)
    req = build_export_request(rs * 3)
    assert len(req.resource_spans) == 3


# --- relay_once orchestration ------------------------------------------------


def test_relay_once_no_traces_advances_cursor(tmp_path: Path) -> None:
    state = _state(tmp_path)
    captured = {}

    def fake_search(*args, **kwargs):
        captured["window"] = (kwargs["start_epoch"], kwargs["end_epoch"])
        return []

    stats = relay_once(
        tempo_base="http://tempo:31989",
        target_otlp="mac:4317",
        state=state,
        lookback_s=DEFAULT_LOOKBACK_S,
        now_epoch=2_000_000_000.0,
        searcher=fake_search,
        fetcher=lambda *a, **kw: pytest.fail("fetcher must not be called"),
        pusher=lambda *a, **kw: pytest.fail("pusher must not be called"),
    )

    assert stats.traces_seen == 0
    assert stats.spans_sent == 0
    cursor = state.read(CURSOR_KEY)
    assert cursor["last_end_iso"].startswith("2033-")


def test_relay_once_initial_window_uses_initial_window_s(tmp_path: Path) -> None:
    state = _state(tmp_path)
    captured = {}

    def fake_search(*args, **kwargs):
        captured["window"] = (kwargs["start_epoch"], kwargs["end_epoch"])
        return []

    relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        initial_window_s=600,
        now_epoch=1_000_000_000.0,
        searcher=fake_search,
    )

    start, end = captured["window"]
    assert end == 1_000_000_000
    assert end - start == 600


def test_relay_once_bounds_window_to_max(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.write(CURSOR_KEY, {"last_end_iso": "2033-05-18T03:33:20+00:00"})
    captured = {}

    def fake_search(*args, **kwargs):
        captured["window"] = (kwargs["start_epoch"], kwargs["end_epoch"])
        return []

    relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        now_epoch=2_000_000_000.0,
        lookback_s=1000,
        max_window_s=120,
        searcher=fake_search,
    )

    start, end = captured["window"]
    assert end - start == 120


def test_relay_once_resumes_with_lookback(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.write(CURSOR_KEY, {"last_end_iso": "2033-05-18T03:33:20+00:00"})
    captured = {}

    def fake_search(*args, **kwargs):
        captured["window"] = (kwargs["start_epoch"], kwargs["end_epoch"])
        return []

    relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        lookback_s=120,
        now_epoch=2_000_000_000.0,
        searcher=fake_search,
    )

    start, end = captured["window"]
    # last_end was 2_000_000_000; start should be last_end - lookback
    assert start == 2_000_000_000 - 120
    assert end == 2_000_000_000


def test_relay_once_pushes_otlp_and_advances(tmp_path: Path) -> None:
    state = _state(tmp_path)
    pushed: list = []

    def fake_search(*args, **kwargs):
        return ["abc"]

    def fake_fetch(*args, **kwargs):
        return SAMPLE_TRACE_JSON

    def fake_push(target, request, **kwargs):
        pushed.append((target, request))

    stats = relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        now_epoch=2_000_000_000.0,
        searcher=fake_search,
        fetcher=fake_fetch,
        pusher=fake_push,
    )

    assert stats.traces_seen == 1
    assert stats.traces_fetched == 1
    assert stats.spans_sent == 1
    assert stats.push_calls == 1
    assert pushed[0][0] == "mac:4317"
    assert len(pushed[0][1].resource_spans) == 1
    cursor = state.read(CURSOR_KEY)
    assert cursor["last_end_iso"].startswith("2033-")


def test_relay_once_keeps_cursor_on_push_failure(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.write(CURSOR_KEY, {"last_end_iso": "2033-05-18T03:33:20+00:00"})

    def fake_push(target, request, **kwargs):
        raise RuntimeError("server unreachable")

    stats = relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        now_epoch=2_000_000_000.0,
        searcher=lambda *a, **kw: ["abc"],
        fetcher=lambda *a, **kw: SAMPLE_TRACE_JSON,
        pusher=fake_push,
    )

    assert stats.errors and "server unreachable" in stats.errors[0]
    cursor = state.read(CURSOR_KEY)
    assert cursor["last_end_iso"] == "2033-05-18T03:33:20+00:00"  # unchanged


# --- backfill ----------------------------------------------------------------


def test_backfill_walks_chunked_windows() -> None:
    windows: list[tuple[int, int]] = []

    def fake_search(_base, *, services, start_epoch, end_epoch, limit):
        windows.append((start_epoch, end_epoch))
        return []

    stats = backfill(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        services=["agentweave-proxy"],
        start_epoch=1000,
        end_epoch=4500,
        chunk_seconds=1000,
        searcher=fake_search,
        fetcher=lambda *a, **kw: pytest.fail(
            "fetcher should not run when search is empty"
        ),
        pusher=lambda *a, **kw: pytest.fail(
            "pusher should not run when search is empty"
        ),
    )

    # 1000-2000, 2000-3000, 3000-4000, 4000-4500
    assert windows == [(1000, 2000), (2000, 3000), (3000, 4000), (4000, 4500)]
    assert stats.traces_seen == 0


def test_backfill_aggregates_stats_across_chunks() -> None:
    pushed = []

    def fake_search(_base, *, services, start_epoch, end_epoch, limit):
        # Two chunks return one trace each
        return ["t1"] if start_epoch == 0 else ["t2"]

    def fake_push(target, request, **kwargs):
        pushed.append(request)

    stats = backfill(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        services=["agentweave-proxy"],
        start_epoch=0,
        end_epoch=120,
        chunk_seconds=60,
        searcher=fake_search,
        fetcher=lambda *a, **kw: SAMPLE_TRACE_JSON,
        pusher=fake_push,
    )

    assert stats.traces_seen == 2
    assert stats.spans_sent == 2
    assert stats.push_calls == 2
    assert len(pushed) == 2


def test_backfill_continues_after_chunk_error() -> None:
    pushed = []

    def fake_search(_base, *, services, start_epoch, end_epoch, limit):
        return ["x"]

    def fake_push(target, request, **kwargs):
        # First chunk fails; second succeeds
        if not pushed:
            pushed.append(None)
            raise RuntimeError("boom")
        pushed.append(request)

    stats = backfill(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        services=["x"],
        start_epoch=0,
        end_epoch=120,
        chunk_seconds=60,
        searcher=fake_search,
        fetcher=lambda *a, **kw: SAMPLE_TRACE_JSON,
        pusher=fake_push,
    )

    assert stats.spans_sent == 1  # second chunk succeeded
    assert any("boom" in e for e in stats.errors)


def test_backfill_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        backfill(
            tempo_base="x",
            target_otlp="y",
            services=["z"],
            start_epoch=100,
            end_epoch=100,
        )


def test_relay_window_no_cursor_touch(tmp_path: Path) -> None:
    """relay_window must not depend on or write any state."""
    state = _state(tmp_path)
    state.write(CURSOR_KEY, {"last_end_iso": "2030-01-01T00:00:00+00:00"})

    relay_window(
        tempo_base="x",
        target_otlp="y",
        services=["s"],
        start_epoch=0,
        end_epoch=60,
        searcher=lambda *a, **kw: [],
    )

    # Cursor untouched
    assert state.read(CURSOR_KEY)["last_end_iso"] == "2030-01-01T00:00:00+00:00"


def test_relay_once_skips_failed_trace_fetch(tmp_path: Path) -> None:
    state = _state(tmp_path)
    pushed = []

    def fake_fetch(_base, tid):
        if tid == "bad":
            raise RuntimeError("404")
        return SAMPLE_TRACE_JSON

    relay_once(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        state=state,
        now_epoch=2_000_000_000.0,
        searcher=lambda *a, **kw: ["bad", "good"],
        fetcher=fake_fetch,
        pusher=lambda target, req: pushed.append(req),
    )

    assert len(pushed) == 1
    assert len(pushed[0].resource_spans) == 1  # only "good" got fetched


def test_relay_window_counts_zero_span_traces(tmp_path: Path) -> None:
    def fake_fetch(_base, tid, **kwargs):
        if tid == "no_spans":
            return {"batches": []}
        return SAMPLE_TRACE_JSON

    def fake_push(*args, **kwargs):
        return None

    stats = relay_window(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        services=["agentweave-proxy"],
        start_epoch=0,
        end_epoch=10,
        searcher=lambda *a, **kw: ["no_spans", "with_spans"],
        fetcher=fake_fetch,
        pusher=fake_push,
    )

    assert stats.zero_span_traces == 1
    assert stats.zero_span_trace_ids == ["no_spans"]
    assert stats.traces_seen == 2
    assert stats.traces_fetched == 1
    assert stats.spans_sent == 1


def test_backfill_propagates_timeouts() -> None:
    seen = {}

    def fake_search(_base, *, services, start_epoch, end_epoch, limit, timeout):
        seen["search_timeout"] = timeout
        return ["t1"]

    def fake_fetch(_base, tid, *, timeout):
        seen["fetch_timeout"] = timeout
        return SAMPLE_TRACE_JSON

    def fake_push(_target, _request, **kwargs):
        seen["push_timeout"] = kwargs.get("timeout")

    backfill(
        tempo_base="http://tempo",
        target_otlp="mac:4317",
        services=["agentweave-proxy"],
        start_epoch=0,
        end_epoch=10,
        chunk_seconds=10,
        search_timeout_s=12.5,
        fetch_timeout_s=13.5,
        push_timeout_s=14.5,
        searcher=fake_search,
        fetcher=fake_fetch,
        pusher=fake_push,
    )

    assert seen["search_timeout"] == 12.5
    assert seen["fetch_timeout"] == 13.5
    assert seen["push_timeout"] == 14.5


def test_zero_span_traces_emit_warning(caplog, tmp_path: Path) -> None:
    state = _state(tmp_path)

    def fake_search(*args, **kwargs):
        return ["z"]

    def fake_fetch(_base, _tid, **kwargs):
        return {"batches": []}

    with caplog.at_level(logging.WARNING):
        relay_once(
            tempo_base="http://tempo",
            target_otlp="mac:4317",
            state=state,
            now_epoch=2_000_000_000.0,
            searcher=fake_search,
            fetcher=fake_fetch,
            pusher=lambda target, request: None,
        )

    assert "tempo_relay: trace z had zero spans" in caplog.text
