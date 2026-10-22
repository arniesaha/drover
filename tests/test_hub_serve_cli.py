"""Tests for the explicit private Tailscale Serve hub helper."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from drover.config import default_config
from drover.server.__main__ import main


def _result(*, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_hub_serve_tailscale_requires_the_tailscale_cli(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(server_main.shutil, "which", lambda _: None)

    result = CliRunner().invoke(main, ["hub", "serve", "--tailscale"])

    assert result.exit_code != 0
    assert "Tailscale CLI is not available" in result.output


def test_hub_serve_tailscale_surfaces_serve_errors(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(server_main.shutil, "which", lambda _: "/test/tailscale")
    monkeypatch.setattr(server_main, "_resolve_config", lambda _: default_config())
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[2:] == ["status", "--json"]:
            return _result(stdout="{}")
        return _result(returncode=1, stderr="not logged in")

    monkeypatch.setattr("subprocess.run", run)

    result = CliRunner().invoke(main, ["hub", "serve", "--tailscale"])

    assert result.exit_code != 0
    assert "Tailscale Serve failed: not logged in" in result.output
    assert calls == [
        (
            ["/test/tailscale", "serve", "status", "--json"],
            {"capture_output": True, "check": False, "text": True, "timeout": 10},
        ),
        (
            [
                "/test/tailscale",
                "serve",
                "--bg",
                "--https=443",
                "http://127.0.0.1:7080",
            ],
            {"capture_output": True, "check": False, "text": True, "timeout": 10},
        ),
    ]


def test_hub_serve_tailscale_uses_private_serve_not_funnel(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(server_main.shutil, "which", lambda _: "/test/tailscale")
    monkeypatch.setattr(server_main, "_resolve_config", lambda _: default_config())
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[2:] == ["status", "--json"]:
            return _result(stdout="{}")
        if args[2:] == ["status"]:
            return _result(
                stdout="https://hub.example-tailnet.ts.net (tailnet only)\\n"
            )
        return _result()

    monkeypatch.setattr("subprocess.run", run)

    result = CliRunner().invoke(main, ["hub", "serve", "--tailscale"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ["/test/tailscale", "serve", "status", "--json"],
        [
            "/test/tailscale",
            "serve",
            "--bg",
            "--https=443",
            "http://127.0.0.1:7080",
        ],
        ["/test/tailscale", "serve", "status"],
    ]
    assert all("funnel" not in argument.lower() for call in calls for argument in call)
    assert (
        "Private Tailscale Serve URL: https://hub.example-tailnet.ts.net"
        in result.output
    )
    assert "Tailnet-only: Tailscale Funnel was not enabled." in result.output


def test_hub_serve_tailscale_refuses_existing_serve_routes(monkeypatch):
    import drover.server.__main__ as server_main

    monkeypatch.setattr(server_main.shutil, "which", lambda _: "/test/tailscale")
    monkeypatch.setattr(server_main, "_resolve_config", lambda _: default_config())
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return _result(stdout='{"Web":{"hub.example.ts.net:443":{"Handlers":{}}}}')

    monkeypatch.setattr("subprocess.run", run)

    result = CliRunner().invoke(main, ["hub", "serve", "--tailscale"])

    assert result.exit_code != 0
    assert "refusing to overwrite unrelated routes" in result.output
    assert calls == [["/test/tailscale", "serve", "status", "--json"]]
