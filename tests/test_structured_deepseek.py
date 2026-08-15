from __future__ import annotations

import time

import pytest

from drover.server.harness.structured.deepseek import DeepSeekDriver, default_command


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
            return {"events": list(self.events), "hasMore": False}
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
