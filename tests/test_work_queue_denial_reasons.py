import json
import logging
from types import SimpleNamespace

import pytest

from drover.server.work_queue import client, runtime
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.service import WorkDenied


class _Service:
    config = SimpleNamespace(principal_roles={})

    def __init__(self, exc):
        self.exc = exc

    def call(self, operation, args, actor):
        raise self.exc


@pytest.fixture(autouse=True)
def _actor(monkeypatch):
    monkeypatch.setattr(
        runtime, "authenticated_actor", lambda auth, headers, config: None
    )


ARGS = {"goal_id": "continuity-soak-819", "number": 828}


@pytest.mark.parametrize(
    "exc,status,reason",
    [
        (WorkDenied("worker capacity unavailable"), 409, "worker capacity unavailable"),
        (
            WorkDenied("launch intent uncertain; inspection required"),
            409,
            "launch intent uncertain; inspection required",
        ),
        (PermissionError("coordinator required"), 403, "coordinator required"),
        (
            ProviderUnavailable("token ghp_SECRET rejected"),
            409,
            "item provider unavailable",
        ),
    ],
)
def test_dispatch_returns_and_logs_the_denial_reason(caplog, exc, status, reason):
    with caplog.at_level(logging.WARNING):
        code, body = runtime.dispatch(_Service(exc), None, {}, "launch", ARGS)
    assert (code, body["reason"]) == (status, reason)
    assert "continuity-soak-819" in caplog.text
    assert reason in caplog.text or "provider unavailable" in caplog.text
    assert "ghp_SECRET" not in json.dumps(body)


def test_unexpected_exception_logs_traceback_but_reflects_only_the_class(caplog):
    secret = RuntimeError("postgres://user:hunter2@db/x")
    with caplog.at_level(logging.ERROR):
        code, body = runtime.dispatch(_Service(secret), None, {}, "launch", ARGS)
    assert code == 409
    assert body["reason"] == "unexpected RuntimeError"
    assert "hunter2" not in json.dumps(body)
    assert "hunter2" in caplog.text  # server log keeps the cause


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield self._body


def test_mcp_client_surfaces_status_and_reason(monkeypatch):
    body = json.dumps(
        {"error": "work operation forbidden", "reason": "coordinator required"}
    ).encode()
    monkeypatch.setattr(
        client.requests, "request", lambda *a, **k: _Response(403, body)
    )
    with pytest.raises(RuntimeError) as err:
        client.control_call(
            "http://127.0.0.1:1", {"Authorization": "Bearer t"}, "launch", {}
        )
    assert "403" in str(err.value) and "coordinator required" in str(err.value)
    assert "Bearer" not in str(err.value)


def test_mcp_client_tolerates_non_json_error(monkeypatch):
    monkeypatch.setattr(
        client.requests, "request", lambda *a, **k: _Response(502, b"<html>")
    )
    with pytest.raises(RuntimeError, match="HTTP 502: unavailable"):
        client.control_call(
            "http://127.0.0.1:1", {"Authorization": "Bearer t"}, "get", {}
        )
