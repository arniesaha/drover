"""Host-replaceable shell/Codex contracts, run unchanged on both daemons."""

from __future__ import annotations

import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import pytest

from drover.server.harness.relay_protocol import req_frame
from drover.server.harness.websocket import client_send_frame, recv_json, send_json

from .support import wait_until
from .test_harnessd_contract import request


def codex(daemon, **extra):
    status, body = request(
        daemon,
        "/sessions",
        payload={
            "harness": "codex",
            "mode": "structured",
            "cwd": str(daemon.root),
            **extra,
        },
    )
    assert status == 201, body
    return body["session_id"]


def test_codex_turn_native_id_resume_and_durable_dedup(daemon, fake_hub):
    sid = codex(daemon)
    key = str(uuid4())
    status, first = request(
        daemon,
        f"/sessions/{sid}/turns",
        payload={"text": "hello", "client_turn_id": key},
    )
    assert status == 202, first
    wait_until(
        lambda: any(
            e.get("payload", {}).get("thread_id")
            for r in fake_hub.requests
            if r["path"] == "/harness/events"
            for e in r["body"]["events"]
        )
    )
    wait_until(
        lambda: any(
            e.get("payload", {}).get("turn_complete")
            for r in fake_hub.requests
            if r["path"] == "/harness/events"
            for e in r["body"]["events"]
        )
    )
    assert request(
        daemon,
        f"/sessions/{sid}/turns",
        payload={"text": "hello", "client_turn_id": key},
    ) == (202, first)
    assert (
        request(daemon, f"/sessions/{sid}/turns", payload={"text": "again"})[0] == 202
    )
    log = daemon.root / "fake-cli.jsonl"
    wait_until(lambda: log.exists() and '"resume"' in log.read_text())


def test_codex_restart_recovery_and_termination(daemon):
    sid = codex(daemon)
    # SIGKILL models an unplanned restart; graceful stop terminates sessions.
    daemon.process.kill()
    daemon.process.wait(timeout=5)
    daemon.restart()
    assert request(daemon, f"/sessions/{sid}")[1]["status"] == "errored"
    assert request(daemon, f"/sessions/{sid}/recover", payload={})[0] == 409
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: request(
                    daemon,
                    f"/sessions/{sid}/recover",
                    payload={"native_session_id": "native-pilot"},
                ),
                range(3),
            )
        )
    assert all(status == 200 for status, _ in results), results
    assert sum(body["recovered"] for _, body in results) == 1
    assert request(daemon, f"/sessions/{sid}/interrupt", payload={}) == (
        200,
        {"ok": True},
    )
    assert (
        request(
            daemon,
            f"/sessions/{sid}/permission",
            payload={"request_id": "missing", "decision": "allow"},
        )[0]
        == 409
    )
    assert request(daemon, f"/sessions/{sid}/terminate", payload={})[0] == 200
    assert (
        request(
            daemon,
            f"/sessions/{sid}/recover",
            payload={"native_session_id": "native-pilot"},
        )[0]
        == 409
    )


def test_direct_shell_websocket_input_resize_ping_and_exit(daemon):
    _, created = request(
        daemon,
        "/sessions",
        payload={"harness": "shell", "cwd": str(daemon.root), "command": ["/bin/sh"]},
    )
    sid = created["session_id"]
    parsed = urlparse(daemon.url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(
            (
                f"GET /sessions/{sid}/terminal HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {daemon.token}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += sock.recv(1)
        assert b" 101 " in header, header
        assert recv_json(sock)["type"] == "attached"
        client_send_frame(
            sock, 1, json.dumps({"type": "resize", "rows": 40, "cols": 100}).encode()
        )
        client_send_frame(
            sock,
            1,
            json.dumps({"type": "input", "data": "printf 'pilot-%s\\n' ok\n"}).encode(),
        )
        output = ""
        deadline = time.monotonic() + 5
        while "pilot-ok" not in output and time.monotonic() < deadline:
            message = recv_json(sock)
            if message.get("type") == "output":
                output += message.get("data", "")
        assert "pilot-ok" in output
        client_send_frame(sock, 1, json.dumps({"type": "ping"}).encode())
        while recv_json(sock).get("type") != "pong":
            pass
        client_send_frame(
            sock, 1, json.dumps({"type": "input", "data": "exit\n"}).encode()
        )
        wait_until(
            lambda: request(daemon, f"/sessions/{sid}")[1]["status"] != "running"
        )
    finally:
        sock.close()
        request(daemon, f"/sessions/{sid}/terminate", payload={})


@pytest.mark.parametrize("daemon", [True], indirect=True)
def test_relay_session_mutations_use_http_routes(daemon, fake_hub):
    sock = fake_hub.relay()
    send_json(
        sock,
        req_frame(
            "create",
            "POST",
            "/sessions",
            {"harness": "codex", "mode": "structured", "cwd": str(daemon.root)},
        ),
    )
    frame = recv_json(sock)
    assert frame["status"] == 201, frame
    sid = json.loads(frame["body"])["session_id"]
    send_json(sock, req_frame("stop", "POST", f"/sessions/{sid}/terminate", {}))
    frame = recv_json(sock)
    assert frame["status"] == 200
    assert request(daemon, f"/sessions/{sid}")[1]["status"] == "terminated"


def test_folder_cursor_filter_and_root_fence(daemon):
    root = daemon.root / "home" / "workspace"
    for name in ["alpha", "alpine", "beta"]:
        (root / name).mkdir(parents=True)

    def listing(**params):
        return request(
            daemon,
            "/fs/list?"
            + urlencode({"host_id": "contract-host", "path": str(root), **params}),
        )

    _, first = listing(limit=1, filter="al")
    assert len(first["entries"]) == 1
    _, second = listing(limit=1, filter="al", cursor=first["next_cursor"])
    assert {e["name"] for page in (first, second) for e in page["entries"]} == {
        "alpha",
        "alpine",
    }
    assert listing(filter="beta", cursor=first["next_cursor"])[0] == 400
    assert request(daemon, "/fs/list?host_id=contract-host&path=/etc")[0] == 403
    (root / "escape").symlink_to("/etc", target_is_directory=True)
    assert listing(path=str(root / "escape"))[0] == 403


def test_413_retries_identity_without_blocking_delivery(daemon, fake_hub):
    fake_hub.script("/harness/events", 413, {})
    sid = codex(daemon, prompt="hello")
    wait_until(
        lambda: len([r for r in fake_hub.requests if r["path"] == "/harness/events"])
        >= 2
    )
    batches = [
        r["body"]["events"] for r in fake_hub.requests if r["path"] == "/harness/events"
    ]
    first_ids = {e["event_id"] for e in batches[0]}
    assert any(e["event_id"] in first_ids for batch in batches[1:] for e in batch)
    assert request(daemon, f"/sessions/{sid}/terminate", payload={})[0] == 200


@pytest.mark.parametrize("daemon", [True], indirect=True)
def test_relay_negotiated_response_frames(daemon, fake_hub):
    from drover.server.harness.websocket import recv_frame

    sock = fake_hub.relay()
    send_json(
        sock,
        req_frame(
            "framed", "GET", "/sessions", None, response_framing="framed_responses_v1"
        ),
    )
    start = recv_json(sock)
    assert start["kind"] == "res_start"
    assert start["status"] == 200
    payload = recv_frame(sock).payload
    assert len(payload) == start["body_bytes"]
    assert json.loads(payload)["sessions"] == []


def test_codex_git_evidence_and_worktree_preservation(daemon, fake_hub):
    import subprocess

    repo = daemon.root / "repo"
    repo.mkdir()
    for args in [
        ["init"],
        ["config", "user.email", "contract@example.test"],
        ["config", "user.name", "Contract"],
        ["commit", "--allow-empty", "-m", "initial"],
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    sid = codex(daemon, cwd=str(repo))
    _, session = request(daemon, f"/sessions/{sid}")
    worktree = session["cwd"]
    from pathlib import Path

    assert Path(worktree) != repo
    # Simulate a file left by a previous turn; evidence must include untracked
    # paths and teardown must preserve work that has not been published.
    (Path(worktree) / "pilot-change.txt").write_text("keep this\n")
    assert (
        request(daemon, f"/sessions/{sid}/turns", payload={"text": "inspect"})[0] == 202
    )
    wait_until(
        lambda: any(
            e.get("payload", {}).get("source") == "git_diff"
            and {c["path"] for c in e["payload"]["changes"]} == {"pilot-change.txt"}
            for r in fake_hub.requests
            if r["path"] == "/harness/events"
            for e in r["body"]["events"]
        )
    )
    assert request(daemon, f"/sessions/{sid}/terminate", payload={})[0] == 200
    assert (Path(worktree) / "pilot-change.txt").read_text() == "keep this\n"


@pytest.mark.parametrize(
    "daemon",
    [
        {
            "hub_responses": [
                (
                    "/harness/hosts",
                    200,
                    {"content_consent": {"enabled": False, "epoch": 8}},
                )
            ]
        }
    ],
    indirect=True,
)
def test_registration_persists_consent_epoch(daemon):
    path = daemon.root / ".config.toml.harness-content-consent.json"
    wait_until(lambda: path.exists())
    assert json.loads(path.read_text()) == {"enabled": False, "epoch": 8}


def test_capabilities_are_accepted_by_hub_validator(daemon, fake_hub):
    from drover.server.harness.capabilities import validate_capabilities

    status, envelope = request(daemon, "/capabilities")
    assert status == 200
    projected = validate_capabilities(envelope, "contract-host")
    rows = {row["name"]: row for row in projected["harnesses"]}
    for name, mode in [("shell", "pty"), ("codex", "structured")]:
        assert rows[name]["enabled"] is True
        assert rows[name]["capabilities"]["schema_version"] == 1
        assert mode in rows[name]["capabilities"]["launch_modes"]
    registration = next(r for r in fake_hub.requests if r["path"] == "/harness/hosts")
    validate_capabilities(registration["body"]["capabilities"], "contract-host")
