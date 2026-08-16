"""End-to-end: structured harness sessions, daemon to central.

Wires a real daemon HTTP server driving a structured session (via the fake
claude-code-shaped CLI also used in tests/test_harness_daemon.py) through a
real EventPusher to a real central metrics HTTP server, and asserts the full
event trail lands there in strict seq order. A second test drives two
structured sessions concurrently on one daemon to probe the cross-session
DuckDB write path the manager's per-session locking is meant to protect.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.structured.pusher import EventPusher
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.auth import AuthSettings

# Same fake, headless-safe "claude-code"-shaped CLI as
# tests/test_harness_daemon.py:FAKE_STRUCTURED_CLI. Duplicated rather than
# imported: tests/ has no __init__.py, so cross-module test imports aren't
# wired up here. Keep the two in sync if the wire protocol changes.
FAKE_STRUCTURED_CLI = [
    sys.executable,
    "-c",
    (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    obj=json.loads(line)\n"
        "    if obj.get('type')=='control_response':\n"
        "        print(json.dumps({'type':'assistant','message':{'role':'assistant',"
        "'content':[{'type':'text','text':'approved and done'}]}}),flush=True)\n"
        "        print(json.dumps({'type':'result','subtype':'success'}),flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'type':'control_request','request_id':'req-1',"
        "'request':{'subtype':'can_use_tool','tool_name':'Bash',"
        "'input':{'command':'ls'}}}),flush=True)\n"
    ),
]

# A fake CLI that ignores its input turn and free-runs: 20 self-generated
# "heartbeat" events (unrecognized top-level kind -> ClaudeDriver maps them
# to type="status") with a short sleep between each, then a "result" that
# flips awaiting to "input". Used to keep two sessions' pump threads busy
# writing to the shared registry at the same time.
FAKE_LOOPY_CLI = [
    sys.executable,
    "-c",
    (
        "import json,sys,time\n"
        "sys.stdin.readline()\n"
        "for i in range(20):\n"
        "    time.sleep(0.01)\n"
        "    print(json.dumps({'type':'heartbeat','i':i}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success'}),flush=True)\n"
    ),
]

_TEST_TOKEN = "test-token"
_TEST_AUTH = AuthSettings(enabled=True, api_token=_TEST_TOKEN)
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


# A session create spawns a CLI subprocess behind this request, so the HTTP
# call is not a cheap lookup. 5s was a coin flip on a contended runner (issue
# #90: reproduced as a bare `TimeoutError: timed out` out of urlopen while the
# daemon was still writing its 201). These are hang guards, not budgets.
_HTTP_TIMEOUT = 30


def _json_request(url: str, *, payload: dict | None = None):
    data = None
    headers = dict(_AUTH_HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _authed_get(url: str):
    request = urllib.request.Request(url, headers=_AUTH_HEADERS)
    return urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT)


def _wait_until(predicate, timeout: float = 30, *, what: str = "condition") -> None:
    """Poll ``predicate`` until it is true, or fail with WHY it never was.

    The old version swallowed every exception and raised a bare "condition
    was not met before timeout", so a predicate that raised on every single
    attempt was indistinguishable from one that merely ran out of time --
    which is exactly the state this test failed in on CI (issue #90). Keep
    the last exception and the last value and put them in the message.

    Also monotonic: ``time.time()`` can step backwards under NTP correction
    and silently extend or truncate the wait.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    last_value: object = None
    while time.monotonic() < deadline:
        try:
            last_value = predicate()
            if last_value:
                return
            last_error = None
        except Exception as exc:  # noqa: BLE001 - reported below
            last_error = exc
        time.sleep(0.1)
    detail = (
        f"last call raised {type(last_error).__name__}: {last_error}"
        if last_error is not None
        else f"last value was {last_value!r}"
    )
    raise AssertionError(f"{what} was not met within {timeout}s; {detail}")


def _fetch_session(base_url: str, session_id: str) -> dict:
    _, session = _json_request(f"{base_url}/sessions/{session_id}")
    return session


def _start_central(tmp_path):
    duckdb_path = tmp_path / "central.duckdb"
    bootstrap(parquet_dir=tmp_path / "central-parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


def _start_daemon(tmp_path, *, name: str, central_url: str | None = None):
    duckdb_path = tmp_path / f"{name}.duckdb"
    bootstrap(parquet_dir=tmp_path / f"{name}-parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    state = HarnessDaemonState(
        host_id=f"test-{name}",
        display_name=f"Test {name}",
        kind="linux",
        registry=registry,
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
        api_token=_TEST_TOKEN,
        central_url=central_url,
    )
    register_daemon_host(state)
    pusher = None
    if central_url:
        # batch_interval=0.2 (not wire_event_pusher's default 2.0) so the
        # E2E poll below doesn't need a long deadline.
        pusher = EventPusher(central_url, _TEST_TOKEN, batch_interval=0.2)
        pusher.start()
        state.push_event = pusher.push
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, state, pusher, f"http://{host}:{port}"


def _close_structured_sessions(state) -> None:
    for session_id in list(state.structured.session_ids()):
        state.structured.close(session_id)


def test_structured_session_events_reach_central(tmp_path):
    central_server, central_url = _start_central(tmp_path)
    daemon_server, state, pusher, base_url = _start_daemon(
        tmp_path, name="daemon", central_url=central_url
    )
    try:
        status, body = _json_request(
            f"{base_url}/sessions",
            payload={
                "harness": "claude-code",
                "mode": "structured",
                "prompt": "list files",
                "command": FAKE_STRUCTURED_CLI,
                "cwd": str(tmp_path),
            },
        )
        assert status == 201
        assert body["mode"] == "structured"
        sid = body["session_id"]

        _wait_until(
            lambda: _fetch_session(base_url, sid)["awaiting"] == "approval",
            what="session awaiting=approval",
        )

        status, _ = _json_request(
            f"{base_url}/sessions/{sid}/permission",
            payload={"request_id": "req-1", "decision": "allow"},
        )
        assert status == 200

        _wait_until(
            lambda: _fetch_session(base_url, sid)["awaiting"] == "input",
            what="session awaiting=input",
        )

        expected_types = [
            "user_input",
            "approval_prompt",
            "approval_response",
            "assistant_output",
            "status",
        ]

        def _messages() -> list[dict]:
            with _authed_get(
                f"{central_url}/harness/sessions/{sid}/messages"
            ) as response:
                return json.loads(response.read())["messages"]

        _wait_until(
            lambda: [m["type"] for m in _messages()] == expected_types,
            what=f"central mirrored {expected_types}",
        )

        messages = _messages()
        assert [m["type"] for m in messages] == expected_types

        seqs = [m["seq"] for m in messages]
        assert seqs == list(range(1, len(seqs) + 1))  # strictly increasing, no gaps

        assistant_message = messages[expected_types.index("assistant_output")]
        assert assistant_message["text"] == "approved and done"

        status_message = messages[expected_types.index("status")]
        assert status_message["payload"]["turn_complete"] is True

        listing = _fetch_session(base_url, sid)
        assert listing["awaiting"] == "input"
    finally:
        if pusher is not None:
            pusher.stop()
        _close_structured_sessions(state)
        state.pty.close_all()
        daemon_server.shutdown()
        daemon_server.server_close()
        central_server.shutdown()


def test_two_concurrent_structured_sessions_do_not_corrupt_registry(tmp_path):
    server, state, pusher, base_url = _start_daemon(tmp_path, name="concurrent")
    assert pusher is None  # no central wired -- this test targets the daemon registry
    try:
        results: dict[int, tuple[int, dict]] = {}

        def _create(index: int) -> None:
            results[index] = _json_request(
                f"{base_url}/sessions",
                payload={
                    "harness": "claude-code",
                    "mode": "structured",
                    "prompt": "go",
                    "command": FAKE_LOOPY_CLI,
                    "cwd": str(tmp_path),
                },
            )

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert set(results) == {0, 1}
        session_ids = []
        for index in (0, 1):
            status, body = results[index]
            assert status == 201
            session_ids.append(body["session_id"])
        assert len(set(session_ids)) == 2  # distinct sessions

        for sid in session_ids:
            _wait_until(
                lambda sid=sid: (
                    _fetch_session(base_url, sid).get("status") == "completed"
                    or _fetch_session(base_url, sid).get("awaiting") == "input"
                ),
                what=f"session {sid} completed or awaiting=input",
            )

        for sid in session_ids:
            # list_events_after(sid, 0) orders strictly by seq (unlike
            # list_events, which ties on created_at/event_id) so this is the
            # right lens on "did every event land, in order, no corruption."
            events = state.registry.list_events_after(sid, 0)
            # user_input (prompt) + 20 heartbeats + final "result" status +
            # ProcessDriver.on_exit()'s "process exited" status (the fake
            # CLI's script ends right after printing the result) = 23.
            assert len(events) == 23
            seqs = [event.seq for event in events]
            assert seqs == list(range(1, 24))
            assert events[0].event_type == "user_input"
            assert events[-1].event_type == "status"

        # Both sessions' pump threads reached their process-exit events without
        # registry corruption. Completed process drivers are deliberately
        # removed from the live manager so later recovery cannot mistake a
        # dead entry for an idempotently recovered session.
        assert all(not state.structured.has(sid) for sid in session_ids)
    finally:
        _close_structured_sessions(state)
        state.pty.close_all()
        server.shutdown()
        server.server_close()
