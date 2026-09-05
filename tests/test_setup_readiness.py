"""Tests for the read-only first-computer readiness report."""

import json

from drover.config import default_config
from drover.server.setup_readiness import SetupTarget, evaluate_setup


def test_evaluate_setup_reports_advertised_liveness_independently():
    """A passing local listener cannot conceal a failed advertised address."""
    cfg = default_config()
    cfg = cfg.__class__(
        **{
            **cfg.__dict__,
            "metrics_http_port": 7080,
            "server_metrics_host": "127.0.0.1",
            "server_advertised_url": "100.64.0.10:7080",
        }
    )
    calls: list[tuple[str, str, dict | None]] = []

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        calls.append((method, path, payload))
        return {
            "/harness/hosts": {
                "hosts": [
                    {
                        "host_id": "first-host",
                        "status": "online",
                        "capabilities": {
                            "harnesses": [{"name": "codex", "enabled": True}]
                        },
                    }
                ]
            },
            "/harness/hosts/first-host/auth/codex/status": {"state": "authenticated"},
            "/harness/hosts/first-host/fs/exists": {"exists": {"/work/app": True}},
        }[path]

    report = evaluate_setup(
        cfg,
        SetupTarget(host_id="first-host", harness="codex", project="/work/app"),
        liveness=lambda url: url != "http://100.64.0.10:7080/healthz",
        request_json=request_json,
    )

    assert report.ready is False
    assert report.check("local_liveness").state == "pass"
    assert report.check("advertised_liveness").state == "fail"
    assert calls == [
        ("GET", "/harness/hosts", None),
        ("GET", "/harness/hosts/first-host/auth/codex/status", None),
        ("POST", "/harness/hosts/first-host/fs/exists", {"paths": ["/work/app"]}),
    ]
    assert "100.64" not in json.dumps(report.as_dict())
    assert "/work/app" not in json.dumps(report.as_dict())


def test_evaluate_setup_selects_the_online_host_by_wire_host_id():
    """A display or legacy identifier cannot make a different host ready."""
    calls: list[str] = []

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        calls.append(path)
        return {
            "hosts": [
                {
                    "id": "first-host",
                    "status": "online",
                    "capabilities": {"harnesses": [{"name": "codex", "enabled": True}]},
                }
            ]
        }

    report = evaluate_setup(
        default_config(),
        SetupTarget(host_id="first-host", harness="codex", project="/work/app"),
        liveness=lambda _: True,
        request_json=request_json,
    )

    assert report.check("host").state == "fail"
    assert report.check("harness_auth").state == "fail"
    assert report.check("project").state == "fail"
    assert calls == ["/harness/hosts"]


def test_evaluate_setup_requires_an_explicitly_enabled_harness():
    """An advertised but disabled harness cannot be reported as authenticated."""
    calls: list[str] = []

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        calls.append(path)
        return {
            "hosts": [
                {
                    "host_id": "first-host",
                    "status": "online",
                    "capabilities": {
                        "harnesses": [{"name": "codex", "enabled": False}]
                    },
                }
            ]
        }

    report = evaluate_setup(
        default_config(),
        SetupTarget(host_id="first-host", harness="codex", project="/work/app"),
        liveness=lambda _: True,
        request_json=request_json,
    )

    assert report.check("host").state == "pass"
    assert report.check("harness_auth").state == "fail"
    assert "Enable the selected harness" in report.check("harness_auth").action
    assert report.check("project").state == "fail"
    assert calls == ["/harness/hosts"]


def test_evaluate_setup_encodes_host_and_harness_route_components():
    """Host and harness identifiers cannot turn into extra control-plane paths."""
    calls: list[str] = []

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        calls.append(path)
        return {
            "/harness/hosts": {
                "hosts": [
                    {
                        "host_id": "first host/one",
                        "status": "online",
                        "capabilities": {
                            "harnesses": [{"name": "provider/test", "enabled": True}]
                        },
                    }
                ]
            },
            "/harness/hosts/first%20host%2Fone/auth/provider%2Ftest/status": {
                "state": "authenticated"
            },
            "/harness/hosts/first%20host%2Fone/fs/exists": {
                "exists": {"/work/app": True}
            },
        }[path]

    report = evaluate_setup(
        default_config(),
        SetupTarget(
            host_id="first host/one", harness="provider/test", project="/work/app"
        ),
        liveness=lambda _: True,
        request_json=request_json,
    )

    assert report.ready is True
    assert calls == [
        "/harness/hosts",
        "/harness/hosts/first%20host%2Fone/auth/provider%2Ftest/status",
        "/harness/hosts/first%20host%2Fone/fs/exists",
    ]


def test_evaluate_setup_classifies_unsupported_auth_without_auth_detail():
    """An unsupported auth response has a harness recovery action, not its detail."""

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        return {
            "/harness/hosts": {
                "hosts": [
                    {
                        "host_id": "first-host",
                        "status": "online",
                        "capabilities": {
                            "harnesses": [{"name": "codex", "enabled": True}]
                        },
                    }
                ]
            },
            "/harness/hosts/first-host/auth/codex/status": {
                "state": "unsupported",
                "stale_reason": "private implementation detail",
            },
        }[path]

    report = evaluate_setup(
        default_config(),
        SetupTarget(host_id="first-host", harness="codex", project="/work/app"),
        liveness=lambda _: True,
        request_json=request_json,
    )

    auth = report.check("harness_auth")
    assert auth.state == "fail"
    assert "supported harness" in auth.action
    assert "private implementation detail" not in json.dumps(report.as_dict())


def test_evaluate_setup_sanitizes_transport_and_malformed_control_responses():
    """Private transport errors never become report data or recovery text."""

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        raise RuntimeError("token secret-token for /private/project")

    report = evaluate_setup(
        default_config(),
        SetupTarget(
            host_id="private-host", harness="codex", project="/private/project"
        ),
        liveness=lambda _: True,
        request_json=request_json,
    )

    assert report.check("control_api").state == "fail"
    assert report.check("host").state == "fail"
    output = json.dumps(report.as_dict())
    assert "secret-token" not in output
    assert "private-host" not in output
    assert "/private/project" not in output


def test_evaluate_setup_uses_a_fixed_no_action_result_for_passing_checks():
    """A ready report does not prescribe recovery work that has already succeeded."""

    def request_json(method: str, path: str, payload: dict | None) -> dict:
        return {
            "/harness/hosts": {
                "hosts": [
                    {
                        "host_id": "first-host",
                        "status": "online",
                        "capabilities": {
                            "harnesses": [{"name": "codex", "enabled": True}]
                        },
                    }
                ]
            },
            "/harness/hosts/first-host/auth/codex/status": {"state": "authenticated"},
            "/harness/hosts/first-host/fs/exists": {"exists": {"/work/app": True}},
        }[path]

    report = evaluate_setup(
        default_config(),
        SetupTarget(host_id="first-host", harness="codex", project="/work/app"),
        liveness=lambda _: True,
        request_json=request_json,
    )

    assert report.ready is True
    assert {check.action for check in report.checks} == {"No action needed."}
