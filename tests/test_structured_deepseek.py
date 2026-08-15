from __future__ import annotations

import threading
import time

import pytest

from drover.server.harness.structured.deepseek import (
    DeepSeekApiError,
    DeepSeekDriver,
    default_command,
)


def _tool_call_event(seq: int, call_id: str, name: str) -> dict:
    return {
        "event": {
            "type": "tool/call",
            "seq": seq,
            "data": {
                "callId": call_id,
                "name": name,
                "arguments": '{"path":"README.md"}',
            },
        }
    }


def _tool_result_event(seq: int, call_id: str, text: str) -> dict:
    return {
        "event": {
            "type": "tool/result",
            "seq": seq,
            "data": {
                "message": {
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": call_id,
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        }
                    ]
                }
            },
        }
    }


def _assistant_event(seq: int, text: str) -> dict:
    return {
        "event": {
            "type": "assistant/message",
            "seq": seq,
            "surfaceOp": "append",
            "data": {"message": {"content": [{"type": "text", "text": text}]}},
        }
    }


def _turn_end_event(seq: int) -> dict:
    return {
        "event": {
            "type": "turn/end",
            "seq": seq,
            "data": {"reason": {"kind": "completed"}},
        }
    }


def _window(events: list[dict], payload: dict) -> dict:
    """Model ``session.history``: a tail window plus the ``hasMore`` flag."""
    size = int(payload.get("maxMessages") or 1)
    return {"events": list(events[-size:]), "hasMore": len(events) > size}


class FakeApi:
    base_url = "http://127.0.0.1:3080"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method == "session.create":
            return {"sessionId": "session-native-1"}
        if method == "session.history":
            return _window(self.events, payload)
        if method == "session.prompt":
            self.events = [
                {
                    "event": {
                        "type": "tool/call",
                        "seq": 1,
                        "data": {
                            "callId": "call-1",
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                },
                {
                    "event": {
                        "type": "tool/result",
                        "seq": 2,
                        "data": {
                            "message": {
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "call-1",
                                        "content": [{"type": "text", "text": "ok"}],
                                        "isError": False,
                                    }
                                ]
                            }
                        },
                    }
                },
                {
                    "event": {
                        "type": "assistant/message",
                        "seq": 3,
                        "surfaceOp": "append",
                        "data": {
                            "message": {
                                "content": [
                                    {"type": "reasoning", "text": "checked"},
                                    {"type": "text", "text": "done"},
                                ]
                            },
                            "usage": {"inputTokens": 10, "outputTokens": 2},
                        },
                    }
                },
                {
                    "event": {
                        "type": "turn/end",
                        "seq": 4,
                        "data": {"reason": {"kind": "completed"}},
                    }
                },
            ]
            return {"accepted": True}
        if method == "session.selectModel":
            return {"selected": payload}
        if method == "session.cancel":
            return {"accepted": True}
        raise AssertionError(method)


def _wait_for(sink: list, predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(sink):
            return
        time.sleep(0.01)
    raise AssertionError([message.type for message in sink])


def test_default_command() -> None:
    assert default_command("/opt/dsh") == ["/opt/dsh"]


def test_turn_uses_native_session_and_maps_events() -> None:
    api = FakeApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"], "/repo", sink.append, api=api, poll_interval_s=0.01
    )
    driver.start()
    assert sink[0].payload["native_session_id"] == "session-native-1"

    driver.send_turn(
        "inspect",
        "turn-1",
        model="ollama/qwen3.5:35b-a3b",
    )
    _wait_for(
        sink,
        lambda messages: any(
            message.type == "status" and message.payload.get("turn_complete")
            for message in messages
        ),
    )

    assert next(message for message in sink if message.type == "tool_action").payload[
        "input"
    ] == {"path": "README.md"}
    assert (
        next(message for message in sink if message.type == "tool_result").text == "ok"
    )
    outputs = [message for message in sink if message.type == "assistant_output"]
    assert [(message.text, message.payload["thinking"]) for message in outputs] == [
        ("checked", True),
        ("done", False),
    ]
    selection = next(
        payload for method, payload in api.calls if method == "session.selectModel"
    )
    assert selection["provider"] == "ollama"
    assert selection["model"] == "qwen3.5:35b-a3b"
    driver.close()


def test_resume_does_not_create_another_native_session() -> None:
    api = FakeApi()
    driver = DeepSeekDriver(
        ["dsh"],
        "/repo",
        lambda message: None,
        native_session_id="session-existing",
        api=api,
    )
    driver.start()
    assert not any(method == "session.create" for method, _ in api.calls)
    assert api.calls[0][1]["sessionId"] == "session-existing"
    driver.close()


def test_rejects_model_without_provider() -> None:
    driver = DeepSeekDriver(["dsh"], None, lambda message: None, api=FakeApi())
    driver.start()
    with pytest.raises(RuntimeError, match="provider/model"):
        driver.send_turn("hello", "turn-1", model="qwen3.5:35b-a3b")
    driver.close()


class ResumedApi:
    """A native session whose previous turn ended with no poller watching.

    This is the ``/recover`` path: harnessd restarted mid-turn, so only the
    head of the previous turn was on disk when ``start()`` read the history.
    The native turn then finished and wrote its tail (assistant answer plus
    ``turn/end``) with nothing consuming it.
    """

    base_url = "http://127.0.0.1:3080"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events = [
            _tool_call_event(1, "call-old", "read"),
            _tool_result_event(2, "call-old", "old"),
        ]
        self._prompted = False
        self._polls = 0

    def finish_previous_turn(self) -> None:
        self.events.extend([_assistant_event(3, "previous answer"), _turn_end_event(4)])

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method == "session.history":
            if self._prompted:
                self._polls += 1
                # The native runtime takes a beat to start the new turn, so
                # the first poll after the prompt sees only the stale tail.
                if self._polls == 2:
                    self.events.extend(
                        [_assistant_event(5, "new answer"), _turn_end_event(6)]
                    )
            return _window(self.events, payload)
        if method == "session.prompt":
            self._prompted = True
            return {"accepted": True}
        if method == "session.cancel":
            return {"accepted": True}
        raise AssertionError(method)


def test_stale_turn_end_does_not_complete_the_following_turn() -> None:
    api = ResumedApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"],
        "/repo",
        sink.append,
        native_session_id="session-existing",
        api=api,
        poll_interval_s=0.01,
    )
    driver.start()
    api.finish_previous_turn()

    driver.send_turn("what next?", "turn-2")
    _wait_for(
        sink,
        lambda messages: any(
            message.type == "status" and message.payload.get("turn_complete")
            for message in messages
        ),
    )

    assert not any(message.text == "previous answer" for message in sink)
    assert [message.text for message in sink if message.type == "assistant_output"] == [
        "new answer"
    ]
    completions = [
        message
        for message in sink
        if message.type == "status" and message.payload.get("turn_complete")
    ]
    assert len(completions) == 1
    driver.close()


class BurstApi(FakeApi):
    """A turn that writes far more history than one tail window holds."""

    def call(self, method: str, payload: dict) -> dict:
        if method == "session.prompt":
            self.calls.append((method, payload))
            events: list[dict] = []
            for index in range(1, 5):
                events.append(_tool_call_event(index * 2 - 1, f"call-{index}", "read"))
                events.append(_tool_result_event(index * 2, f"call-{index}", "ok"))
            events.append(_assistant_event(9, "done"))
            events.append(_turn_end_event(10))
            self.events = events
            return {"accepted": True}
        return super().call(method, payload)


def test_history_longer_than_the_tail_window_is_not_dropped() -> None:
    api = BurstApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"], "/repo", sink.append, api=api, poll_interval_s=0.01
    )
    driver.start()

    driver.send_turn("inspect", "turn-1")
    _wait_for(
        sink,
        lambda messages: any(
            message.type == "status" and message.payload.get("turn_complete")
            for message in messages
        ),
    )

    assert [
        message.payload["tool_use_id"]
        for message in sink
        if message.type == "tool_action"
    ] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
    ]
    assert len([message for message in sink if message.type == "tool_result"]) == 4
    assert any(
        message.type == "assistant_output" and message.text == "done"
        for message in sink
    )
    driver.close()


class BottomlessApi(FakeApi):
    """An API that always claims more history than it will ever return."""

    def call(self, method: str, payload: dict) -> dict:
        if method == "session.history":
            self.calls.append((method, payload))
            return {"events": list(self.events[-2:]), "hasMore": True}
        if method == "session.prompt":
            self.calls.append((method, payload))
            self.events = [_assistant_event(99, "done"), _turn_end_event(100)]
            return {"accepted": True}
        return super().call(method, payload)


def test_history_paging_is_bounded_and_reports_truncation() -> None:
    api = BottomlessApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"], "/repo", sink.append, api=api, poll_interval_s=0.01
    )
    driver.start()

    driver.send_turn("inspect", "turn-1")
    _wait_for(
        sink,
        lambda messages: any(
            message.type == "status" and message.payload.get("turn_complete")
            for message in messages
        ),
    )

    assert any(
        message.type == "status" and message.payload.get("history_truncated")
        for message in sink
    )
    history_calls = [call for call in api.calls if call[0] == "session.history"]
    assert len(history_calls) <= 8
    driver.close()


class BatchedToolResultApi(FakeApi):
    """Parallel tool calls whose results share one message."""

    def call(self, method: str, payload: dict) -> dict:
        if method == "session.prompt":
            self.calls.append((method, payload))
            self.events = [
                _tool_call_event(1, "call-1", "read"),
                _tool_call_event(2, "call-2", "grep"),
                {
                    "event": {
                        "type": "tool/result",
                        "seq": 3,
                        "data": {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "results follow"},
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "call-1",
                                        "content": [
                                            {"type": "text", "text": "read ok"}
                                        ],
                                        "isError": False,
                                    },
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "call-2",
                                        "content": [
                                            {"type": "text", "text": "grep failed"}
                                        ],
                                        "isError": True,
                                    },
                                ]
                            }
                        },
                    }
                },
                _turn_end_event(4),
            ]
            return {"accepted": True}
        return super().call(method, payload)


def test_batched_tool_results_are_all_surfaced() -> None:
    api = BatchedToolResultApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"], "/repo", sink.append, api=api, poll_interval_s=0.01
    )
    driver.start()

    driver.send_turn("inspect", "turn-1")
    _wait_for(
        sink,
        lambda messages: any(
            message.type == "status" and message.payload.get("turn_complete")
            for message in messages
        ),
    )

    results = [message for message in sink if message.type == "tool_result"]
    assert [
        (message.payload["tool"], message.text, message.payload["is_error"])
        for message in results
    ] == [
        ("read", "read ok", False),
        ("grep", "grep failed", True),
    ]
    driver.close()


def test_interrupt_does_not_raise_when_the_api_is_unreachable() -> None:
    class BrokenApi(FakeApi):
        def call(self, method: str, payload: dict) -> dict:
            if method == "session.cancel":
                raise DeepSeekApiError("DeepSeek Harness API unavailable")
            if method == "session.prompt":
                self.calls.append((method, payload))
                return {"accepted": True}  # no events: the turn stays in flight
            return super().call(method, payload)

    api = BrokenApi()
    driver = DeepSeekDriver(
        ["dsh"], "/repo", lambda message: None, api=api, poll_interval_s=0.01
    )
    driver.start()
    driver.send_turn("inspect", "turn-1")

    driver.interrupt()

    driver.close()


def test_close_silences_a_poll_still_blocked_in_a_request() -> None:
    release = threading.Event()
    entered = threading.Event()

    class SlowApi(FakeApi):
        def call(self, method: str, payload: dict) -> dict:
            if method == "session.history" and self.events:
                entered.set()
                release.wait(2)
            return super().call(method, payload)

    api = SlowApi()
    sink: list = []
    driver = DeepSeekDriver(
        ["dsh"], "/repo", sink.append, api=api, poll_interval_s=0.01
    )
    driver.start()
    driver.send_turn("inspect", "turn-1")
    assert entered.wait(2), "the poll thread never reached its history request"
    # The poll thread is parked inside that request; let it return only after
    # close() has already finalized the session.
    threading.Timer(0.1, release.set).start()

    driver.close()
    time.sleep(0.3)

    assert not any(message.type == "assistant_output" for message in sink)
    assert not any(
        message.type == "status" and message.payload.get("turn_complete")
        for message in sink
    )
