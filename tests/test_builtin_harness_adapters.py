"""The four existing structured harnesses resolve through one registry."""

from __future__ import annotations

import pytest

from drover.server.harness.adapters import HarnessAdapterRegistry, LaunchRequest
from drover.server.harness.auth import default_auth_adapters
from drover.server.harness.daemon import HarnessDaemonState
from drover.server.harness.model_catalog import default_model_catalog_service
from drover.server.harness.structured import agy, claude, codex, deepseek
from drover.server.harness.structured.adapters import BUILTIN_ADAPTERS


def test_every_builtin_resolves_with_its_existing_policy():
    expected = {
        "claude-code": (False, True, True, True),
        "codex": (True, False, True, True),
        "agy": (True, False, True, False),
        "deepseek-harness": (True, False, False, True),
    }
    assert set(BUILTIN_ADAPTERS.ids()) == set(expected)
    for harness_id, (worktree, approvals, auth, recovery) in expected.items():
        adapter = BUILTIN_ADAPTERS.resolve(harness_id, operation="structured")
        assert adapter.capabilities.worktree is worktree
        assert adapter.capabilities.approvals is approvals
        assert adapter.capabilities.interactive_auth is auth
        assert adapter.recover_after_restart is recovery


@pytest.mark.parametrize(
    "harness_id,driver_type,native_suffix",
    [
        ("claude-code", claude.ClaudeDriver, ["--resume", "native-1"]),
        ("codex", codex.CodexDriver, []),
        ("agy", agy.AgyDriver, ["--conversation", "native-1"]),
        ("deepseek-harness", deepseek.DeepSeekDriver, []),
    ],
)
def test_adapter_constructs_existing_driver_without_starting_it(
    monkeypatch, harness_id, driver_type, native_suffix
):
    monkeypatch.setattr(claude, "child_env", lambda: {"FIXTURE": "1"})
    adapter = BUILTIN_ADAPTERS.resolve(harness_id)
    driver = adapter.start(
        LaunchRequest(
            command=("fixture-cli",), cwd="/tmp/project", native_session_id="native-1"
        ),
        lambda _message: None,
    )
    assert isinstance(driver, driver_type)
    assert driver.command == ["fixture-cli", *native_suffix]
    assert driver.cwd == "/tmp/project"
    if harness_id == "claude-code":
        assert driver.env == {"FIXTURE": "1"}
    elif harness_id == "codex":
        assert driver._thread_id == "native-1"
    elif harness_id == "deepseek-harness":
        assert driver._native_session_id == "native-1"
    else:
        assert driver.native_session_id == "native-1"


def test_registry_owns_default_commands(monkeypatch):
    for harness_id, module in (
        ("claude-code", claude),
        ("codex", codex),
        ("agy", agy),
        ("deepseek-harness", deepseek),
    ):
        monkeypatch.setattr(module, "default_command", lambda: ["fixture-cli"])
        assert BUILTIN_ADAPTERS.resolve(harness_id).default_command() == ["fixture-cli"]


def test_auth_and_catalog_factories_follow_the_supplied_registry(monkeypatch):
    monkeypatch.setattr(
        "drover.server.harness.structured.adapters.is_staging", lambda: False
    )
    monkeypatch.setattr(
        "drover.server.harness.structured.adapters._resolve_login_command",
        lambda binary, shell=None: [binary],
    )
    registry = HarnessAdapterRegistry([BUILTIN_ADAPTERS.resolve("codex")])
    assert set(default_auth_adapters(adapters=registry)) == {"codex"}

    preset = type("Preset", (), {"enabled": True, "executable": "/bin/codex"})()
    service = default_model_catalog_service(
        "test-host", {"codex": preset}, adapters=registry
    )
    assert set(service._adapters) == {"codex"}
    assert service._adapters["codex"].command == ("/bin/codex", "app-server", "--stdio")


def test_daemon_state_uses_one_supplied_adapter_registry(monkeypatch):
    monkeypatch.setattr(
        "drover.server.harness.structured.adapters.is_staging", lambda: False
    )
    monkeypatch.setattr(
        "drover.server.harness.structured.adapters._resolve_login_command",
        lambda binary, shell=None: [binary],
    )
    registry = HarnessAdapterRegistry([BUILTIN_ADAPTERS.resolve("codex")])
    state = HarnessDaemonState(
        host_id="host-1",
        display_name="Host",
        kind="linux",
        registry=object(),
        pty=object(),
        presets={},
        adapters=registry,
    )
    assert state.structured.adapters is registry
    assert set(state.auth._adapters) == {"codex"}
