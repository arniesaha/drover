"""Enforced staging credential routing without any live provider or Keychain."""

import json
import shlex

import pytest

from drover.server import staging_credentials as staging


@pytest.fixture
def credential_root(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    home = root / "home"
    key = home / ".drover/anthropic_api_key"
    key.parent.mkdir(parents=True)
    key.write_text("staging-fixture-value\n")
    key.chmod(0o600)
    monkeypatch.setenv("DROVER_RELEASE_ROLE", "testflight-staging")
    monkeypatch.setenv("DROVER_STAGING_ROOT", str(root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return root, key


def test_api_helper_reads_only_private_staging_file(
    credential_root, monkeypatch, capsys
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "personal-fixture")
    assert staging.main() == 0
    output = capsys.readouterr()
    assert output.out == "staging-fixture-value\n"
    assert output.err == ""


@pytest.mark.parametrize("kind", ["missing", "public", "symlink", "parent-symlink"])
def test_api_helper_refuses_unsafe_source_without_fallback(
    credential_root, monkeypatch, capsys, kind
):
    root, key = credential_root
    monkeypatch.setenv("ANTHROPIC_API_KEY", "personal-fixture")
    if kind == "missing":
        key.unlink()
    elif kind == "public":
        key.chmod(0o644)
    elif kind == "symlink":
        elsewhere = root / "elsewhere"
        key.rename(elsewhere)
        key.symlink_to(elsewhere)
    else:
        key.parent.rename(root / "elsewhere")
        key.parent.symlink_to(root / "elsewhere", target_is_directory=True)
    assert staging.main() == 1
    output = capsys.readouterr()
    assert "fixture" not in output.out + output.err


def test_claude_staging_ignores_personal_keychain_and_oauth_file(credential_root):
    from drover.server.providers.claude_credentials import (
        ClaudeCredentialError,
        load_claude_credential,
    )

    root, _ = credential_root
    credential = root / "home/.claude/.credentials.json"
    credential.parent.mkdir()
    credential.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "personal-oauth-fixture"}})
    )

    def personal_keychain():
        pytest.fail("staging must never query personal Keychain")

    with pytest.raises(ClaudeCredentialError, match="not_authenticated"):
        load_claude_credential(keychain_reader=personal_keychain)
    assert staging.read_api_key() == "staging-fixture-value"


def test_staging_claude_driver_requires_bare_and_helper_without_secret_env(
    credential_root, monkeypatch
):
    from drover.server.harness.structured import claude

    monkeypatch.setenv("ANTHROPIC_API_KEY", "personal-fixture")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "personal-fixture")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "personal-fixture")
    command = claude.default_command("/trusted/claude")
    assert "--bare" in command
    settings = json.loads(command[command.index("--settings") + 1])
    helper = shlex.split(settings["apiKeyHelper"])
    assert helper[-2:] == ["-m", "drover.server.staging_credentials"]
    assert "fixture" not in " ".join(command)
    env = claude.child_env()
    assert not any(
        key in env
        for key in [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ]
    )
    assert "staging-fixture-value" not in env.values()


def test_staging_auth_status_never_runs_subscription_login(
    credential_root, monkeypatch
):
    from drover.server.harness import auth

    def unexpected(*args, **kwargs):
        pytest.fail("staging auth must not invoke a login shell or OAuth command")

    monkeypatch.setattr(auth, "_resolve_login_command", unexpected)
    adapters = auth.default_auth_adapters()
    assert adapters["claude-code"].status().state == "authenticated"
    with pytest.raises(RuntimeError):
        adapters["claude-code"].command()


def test_staging_agy_does_not_query_keychain(credential_root):
    from drover.server.providers.agy import AgyUsageProbe

    def personal_keychain():
        pytest.fail("staging must never query personal Keychain")

    probe = AgyUsageProbe(keychain_reader=personal_keychain)
    assert probe.read().status == "usage_unavailable"


def test_staging_codex_commands_force_file_credential_store(credential_root):
    from drover.server.harness.structured import codex
    from drover.server.providers.codex_app_server import CodexAppServerSession

    for command in [
        codex.default_command("/trusted/codex"),
        CodexAppServerSession(("/trusted/codex", "app-server", "--stdio"), 1).command,
    ]:
        assert 'cli_auth_credentials_store="file"' in command


def test_staging_catalog_selects_private_api_source(credential_root):
    from drover.server.harness.model_catalog.claude import ClaudeCatalogAdapter

    adapter = ClaudeCatalogAdapter(
        command=["/trusted/claude"],
        env={"ANTHROPIC_API_KEY": "personal-fixture"},
        settings_paths=["/unrelated/managed-settings.json"],
    )
    assert adapter.settings_paths == ()
    assert "ANTHROPIC_API_KEY" not in adapter.env
    headers, _ = adapter._authentication(adapter.env, "https://api.anthropic.com")
    assert headers["x-api-key"] == "staging-fixture-value"


@pytest.mark.parametrize(
    "body",
    [
        {"harness": "claude-code", "mode": "pty"},
        {"harness": "agy", "mode": "structured"},
        {"harness": "claude-code", "mode": "structured", "command": ["custom"]},
    ],
)
def test_stage_rejects_session_credential_bypasses(credential_root, body):
    from drover.server.harness.daemon import HarnessRequestHandler

    response = {}
    handler = object.__new__(HarnessRequestHandler)
    handler._read_json = lambda: body
    handler._write_json = lambda payload, status: response.update(status=status)
    handler._create_structured_session = lambda body: pytest.fail(
        "unsafe session reached launcher"
    )
    HarnessRequestHandler._create_session(handler)
    assert response["status"] == 400


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_stage_allows_supported_structured_session(credential_root, harness):
    from drover.server.harness.daemon import HarnessRequestHandler

    body = {"harness": harness, "mode": "structured"}
    launched = []
    handler = object.__new__(HarnessRequestHandler)
    handler._read_json = lambda: body
    handler._write_json = lambda *args, **kwargs: pytest.fail(
        "valid staging session rejected"
    )
    handler._create_structured_session = launched.append
    HarnessRequestHandler._create_session(handler)
    assert launched == [body]
