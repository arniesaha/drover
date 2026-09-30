"""Startup delays must not consume the hub's request allowance (#354)."""

import base64
import io
import json
import selectors
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from drover.config import load_config
from drover.server import __main__ as server_main
from drover.server import setup_readiness_transport as transport


@pytest.mark.parametrize(
    "spawn_seconds,request_seconds,spawn_budget,request_budget,expected",
    [
        (12.0, 4.9, 20.0, 5.0, "pass"),
        (30.0, 6.0, 40.0, 7.0, "pass"),
        (21.0, 0.0, 20.0, 5.0, "worker failed to start in time"),
        (12.0, 6.0, 20.0, 5.0, "hub did not answer"),
    ],
)
def test_setup_check_reports_slow_spawn(
    monkeypatch,
    tmp_path,
    spawn_seconds,
    request_seconds,
    spawn_budget,
    request_budget,
    expected,
):
    clock = [0.0]
    processes = []

    class Worker:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(b"R")
            self.killed = False
            processes.append(self)
            # Simulate time inside Popen as well as interpreter initialization.
            clock[0] += 2.0

        def communicate(self, input=None, timeout=None):
            if self.killed:
                return b"", b""
            if request_seconds > timeout:
                raise subprocess.TimeoutExpired("worker", timeout)
            clock[0] += request_seconds
            request = json.loads(input)
            url = request["url"]
            if url.endswith("/harness/hosts"):
                body = {
                    "hosts": [
                        {
                            "host_id": "host",
                            "status": "online",
                            "capabilities": {
                                "harnesses": [{"name": "codex", "enabled": True}]
                            },
                        }
                    ]
                }
            elif url.endswith("/status"):
                body = {"state": "authenticated"}
            elif url.endswith("/exists"):
                body = {"exists": {"/private": True}}
            else:
                body = {}
            return (
                json.dumps(
                    {
                        "outcome": "ok",
                        "status": 200,
                        "body": base64.b64encode(json.dumps(body).encode()).decode(),
                    }
                ).encode(),
                b"",
            )

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def select(self, timeout):
            remaining_spawn = spawn_seconds - 2.0
            clock[0] += min(timeout, remaining_spawn)
            return [object()] if remaining_spawn <= timeout else []

    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(transport, "time", fake_time)
    monkeypatch.setattr(server_main, "time", fake_time)
    monkeypatch.setattr(transport.subprocess, "Popen", Worker)
    monkeypatch.setattr(selectors, "DefaultSelector", Selector)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[setup_check]\nspawn_timeout_seconds = {spawn_budget}\nrequest_timeout_seconds = {request_budget}\n[auth]\napi_token = "test"\n'
    )
    result = CliRunner().invoke(
        server_main.main,
        [
            "--config",
            str(config),
            "setup-check",
            "--host",
            "host",
            "--harness",
            "codex",
            "--project",
            "/private",
            "--json",
        ],
    )
    report = json.loads(result.output)
    for check in report["checks"][:2]:
        if expected == "pass":
            assert check["state"] == "pass"
        else:
            assert expected in check["action"].lower()
    if expected == "pass":
        # Five healthy requests exceed the old shared 25-second budget.
        assert report["ready"] is True
        assert result.exit_code == 0
        assert clock[0] > 25
    else:
        assert expected in report["checks"][2]["action"].lower()
        assert result.exit_code == 2
        assert processes and all(process.killed for process in processes)


@pytest.mark.parametrize("key", ["spawn_timeout_seconds", "request_timeout_seconds"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "true", '"bad"'])
def test_setup_check_rejects_invalid_budget(tmp_path, key, value):
    config = tmp_path / "config.toml"
    config.write_text(f"[setup_check]\n{key} = {value}\n")
    with pytest.raises(ValueError, match="setup_check"):
        load_config(config)


def test_worker_classifies_http_client_timeout(monkeypatch):
    """Socket inactivity timeouts must produce the same safe hub diagnosis."""
    import httpx

    async def timeout(*args):
        raise httpx.ReadTimeout("private URL and token")

    stdin = io.BytesIO(
        transport._encode_request("http://localhost", "GET", None, {}, 5, None)
    )
    stdout = io.BytesIO()
    monkeypatch.setattr(
        transport,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(buffer=stdin), stdout=SimpleNamespace(buffer=stdout)
        ),
    )
    monkeypatch.setattr(transport, "_request_async", timeout)
    transport._worker_main()
    assert stdout.getvalue().startswith(b"R")
    with pytest.raises(TimeoutError, match="hub did not answer"):
        transport._decode_result(stdout.getvalue()[1:])
    assert b"private" not in stdout.getvalue()
