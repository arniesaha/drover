"""Focused boundary tests for the stateless Factory observer bridge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from drover.server.harness.factory_observer import (
    FactoryObserverRequestError,
    factory_observer_command,
    factory_observer_projection,
    parse_factory_observer_launch,
)
from drover.server.web.auth import AuthSettings, request_authorized


class _Headers(dict):
    """Minimal request-header stand-in."""


def _payload() -> dict:
    return {
        "factory_observer": {
            "run_id": "run_FACTORY001",
            "expected_revision": 4,
            "idempotency_key": "factory.launch:run_FACTORY001:4",
            "target_hostname": "studio",
            "repo": {
                "owner": "arniesaha",
                "name": "drover",
                "branch": "factory/run_FACTORY001",
            },
            "worktree": {"cwd": "/work/drover", "policy": "isolated_required"},
            "command": "harness_default",
        },
        "harness": "codex",
        "model": "gpt-5",
        "thinking_effort": "medium",
    }


def test_factory_observer_requires_authenticated_bearer():
    auth = AuthSettings(enabled=True, api_token="factory-token")
    assert not request_authorized(
        auth, _Headers(), method="POST", path="/harness/hosts/studio/sessions"
    )
    assert request_authorized(
        auth,
        _Headers({"Authorization": "Bearer factory-token"}),
        method="POST",
        path="/harness/hosts/studio/sessions",
    )


def test_factory_observer_normalizes_to_existing_structured_launch_and_is_idempotent():
    first, body = parse_factory_observer_launch(_payload(), host_id="studio")
    second, retry = parse_factory_observer_launch(_payload(), host_id="studio")

    assert first == second
    assert first.client_session_id == second.client_session_id
    assert body == retry
    assert body["mode"] == "structured"
    assert "command" not in body
    assert body["handoff_mode"] == "factory_observer"
    assert body["source_session_id"] == "factory/run_FACTORY001@4"
    assert body["cwd"] == "/work/drover"


def test_factory_observer_rejects_mutation_controls_and_target_mismatch():
    mutation = _payload()
    mutation["factory_observer"]["advance"] = "implement"
    with pytest.raises(FactoryObserverRequestError, match="mutation controls"):
        parse_factory_observer_launch(mutation, host_id="studio")

    wrong_host = _payload()
    wrong_host["factory_observer"]["target_hostname"] = "other-host"
    with pytest.raises(FactoryObserverRequestError, match="match this host"):
        parse_factory_observer_launch(wrong_host, host_id="studio")


def test_factory_observer_projection_is_derived_not_a_factory_ledger():
    session = SimpleNamespace(
        handoff_mode="factory_observer",
        source_session_id="factory/run_FACTORY001@4",
        host_id="studio",
        status="running",
    )
    assert factory_observer_projection(session) == {
        "run_id": "run_FACTORY001",
        "expected_revision": 4,
        "target_hostname": "studio",
        "status": "running",
        "authority": "taskflow",
    }


@pytest.mark.parametrize("effort", [None, " high "])
def test_factory_effort_is_optional(effort):
    payload = _payload()
    payload.pop("thinking_effort")
    if effort is not None:
        payload["thinking_effort"] = effort
    _, normalized = parse_factory_observer_launch(payload, host_id="studio")
    assert normalized["thinking_effort"] == (effort.strip() if effort else None)


@pytest.mark.parametrize("effort", ["", "  ", 3, False, []])
def test_factory_rejects_malformed_effort(effort):
    payload = _payload()
    payload["thinking_effort"] = effort
    with pytest.raises(FactoryObserverRequestError, match="thinking_effort"):
        parse_factory_observer_launch(payload, host_id="studio")


def test_factory_permission_policy_applies_to_launch_and_recovery_commands():
    command = [
        "claude",
        "--permission-mode=bypassPermissions",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
    ]
    safe = factory_observer_command("claude-code", command)
    assert safe == ["claude", "--permission-mode", "dontAsk"]
    assert factory_observer_command("claude-code", safe) == safe
    assert factory_observer_command("codex", command) == command
