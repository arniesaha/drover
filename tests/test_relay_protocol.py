import pytest

from drover.server.harness.relay_protocol import (
    RelayProtocolError,
    close_frame,
    data_frame,
    hello_frame,
    open_error_frame,
    open_frame,
    opened_frame,
    parse_frame,
    req_frame,
    res_start_frame,
    res_frame,
)


def test_req_res_round_trip() -> None:
    req = req_frame("abc", "POST", "/sessions", {"harness": "shell"})
    assert parse_frame(req) == {
        "kind": "req",
        "id": "abc",
        "method": "POST",
        "path": "/sessions",
        "body": {"harness": "shell"},
    }
    res = res_frame("abc", 200, '{"session_id": "s1"}\n')
    assert res["status"] == 200
    assert res["body"] == '{"session_id": "s1"}\n'


def test_req_frame_carries_optional_response_bound() -> None:
    assert (
        req_frame(
            "abc",
            "POST",
            "/advisory/content-bundle",
            {"target_ids": ["global-agents"]},
            max_response_bytes=4096,
        )["max_response_bytes"]
        == 4096
    )


def test_res_start_frame_declares_request_identity_and_body_size() -> None:
    assert parse_frame(res_start_frame("abc", 200, 4096)) == {
        "kind": "res_start",
        "id": "abc",
        "status": 200,
        "body_bytes": 4096,
    }


def test_channel_frames() -> None:
    assert open_frame("c1", "/sessions/s1/terminal")["kind"] == "open"
    assert opened_frame("c1") == {"kind": "opened", "chan": "c1"}
    assert open_error_frame("c1", "no session")["error"] == "no session"
    assert data_frame("c1", {"type": "stdin", "data": "ls\n"})["message"] == {
        "type": "stdin",
        "data": "ls\n",
    }
    assert close_frame("c1") == {"kind": "close", "chan": "c1"}


def test_hello_frame() -> None:
    assert hello_frame("work-laptop") == {"kind": "hello", "host_id": "work-laptop"}


@pytest.mark.parametrize("bad", [None, [], "req", {}, {"kind": "unknown"}, {"kind": 7}])
def test_parse_frame_rejects_garbage(bad: object) -> None:
    with pytest.raises(RelayProtocolError):
        parse_frame(bad)
