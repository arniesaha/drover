from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from drover.server.harness.auth import (
    _SHEBANG_PROBE_BYTES,
    AuthFlowLaunchError,
    AuthFlowManager,
    CommandAuthAdapter,
    HarnessAuthStatus,
    StaticAuthAdapter,
    TerminalSignInRequired,
    _resolve_known_versioned_cli,
    _uses_env_node,
    default_auth_adapters,
    redact_auth_text,
)


def wait_for_state(manager, harness, flow_id, state, timeout_s=2):
    deadline = time.time() + timeout_s
    current = manager.snapshot(harness, flow_id)
    while time.time() < deadline:
        current = manager.snapshot(harness, flow_id)
        if current["state"] == state:
            return current
        time.sleep(0.01)
    pytest.fail(f"flow did not reach {state}: {current}")


def test_redact_auth_text_removes_secret_query_values():
    text = (
        "Open https://example.test/login?code=abc&state=ok&access_token=secret "
        "and keep client_secret=hidden"
    )

    redacted = redact_auth_text(text)

    assert "abc" not in redacted
    assert "secret" not in redacted
    assert "hidden" not in redacted
    assert "state=ok" in redacted
    assert "code=<redacted>" in redacted
    assert "access_token=<redacted>" in redacted


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer bearer-secret", "bearer-secret"),
        ("Authorization: Basic basic-secret", "basic-secret"),
        ("token: colon-secret", "colon-secret"),
        ("X-Api-Key: header-secret", "header-secret"),
        ('{"access_token":"json-secret","state":"ok"}', "json-secret"),
        ('{"access_token":12345,"state":"ok"}', "12345"),
        (r'{"access_token":"escaped-\"secret","state":"ok"}', "escaped"),
        ("https://example.test?api-key=query-secret&state=ok", "query-secret"),
    ],
)
def test_redact_auth_text_removes_non_query_secret_values(text, secret):
    redacted = redact_auth_text(text)

    assert secret not in redacted
    assert "<redacted>" in redacted


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"authorization":"Bearer json-secret"}', "json-secret"),
        ('{"password":"json-password"}', "json-password"),
        ('{"cookie":"session=secret-cookie"}', "secret-cookie"),
        ('{"credentials":"credential-secret"}', "credential-secret"),
        ("password: colon-password", "colon-password"),
        ("Cookie: session=header-cookie", "header-cookie"),
        ("Bearer standalone-secret", "standalone-secret"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        ('{"OPENAI_API_KEY":"openai-secret"}', "openai-secret"),
        ("ANTHROPIC_API_KEY: anthropic-secret", "anthropic-secret"),
        ("CLAUDE_CODE_OAUTH_TOKEN = oauth-secret", "oauth-secret"),
    ],
)
def test_redact_auth_text_removes_common_auth_secret_formats(text, secret):
    redacted = redact_auth_text(text)

    assert secret not in redacted
    assert "<redacted>" in redacted


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("OPENAI_API_KEY sk-secret", "sk-secret"),
        ("token abc123", "abc123"),
        ("Cookie cookie-secret", "cookie-secret"),
        ("secret quoted-secret", "quoted-secret"),
        ("api_key lower-secret", "lower-secret"),
        ("api-key hyphen-secret", "hyphen-secret"),
        ("access token abc123", "abc123"),
        (
            "provider returned sk-proj-abcdefghijklmnopqrstuvwxyz",
            "sk-proj-abcdefghijklmnopqrstuvwxyz",
        ),
        (
            "provider returned sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        ),
        (
            "provider returned AIzaSyabcdefghijklmnopqrstuvwxyz12345",
            "AIzaSyabcdefghijklmnopqrstuvwxyz12345",
        ),
        ("open https://user:password@example.test/device", "user:password"),
    ],
)
def test_redact_auth_text_removes_space_delimited_and_provider_secrets(text, secret):
    redacted = redact_auth_text(text)

    assert secret not in redacted
    assert "<redacted>" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "Enter device code ABCD-EFGH",
        "Enter user code WXYZ-1234",
    ],
)
def test_redact_auth_text_preserves_device_and_user_codes(text):
    assert redact_auth_text(text) == text


def test_manager_defaults_to_ten_minute_timeout_and_retention():
    manager = AuthFlowManager({})

    assert manager._timeout_s == 600
    assert manager._retention_s == 600


def test_static_adapter_reports_unavailable_status():
    adapter = StaticAuthAdapter("openclaw")

    status = adapter.status()

    assert status.as_json() == {
        "harness": "openclaw",
        "state": "unavailable",
        "label": None,
        "detail": "auth is not supported for openclaw",
        "sign_in": "flow",
    }


def test_claude_status_parses_logged_in_json(tmp_path):
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' \'{"loggedIn":true,"email":"a@example.test","subscriptionType":"max"}\'\n'
    )
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="claude-code",
        status_command=[str(cli), "auth", "status", "--json"],
        login_command=[str(cli), "auth", "login"],
    )

    status = adapter.status()

    assert status.state == "authenticated"
    assert status.label == "a@example.test"
    assert status.detail == "max"
    assert adapter.command() == [str(cli), "auth", "login"]


@pytest.mark.parametrize("output", ["not-json", "[]", "null"])
def test_claude_status_handles_malformed_or_non_object_json(tmp_path, output):
    cli = tmp_path / "claude"
    cli.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="claude-code",
        status_command=[str(cli), "auth", "status", "--json"],
        login_command=[str(cli), "auth", "login"],
    )

    status = adapter.status()

    assert status.state == "unknown"
    assert status.detail == output


def test_claude_status_reports_nonzero_exit_as_unauthenticated(tmp_path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n" "printf '%s\\n' '{\"loggedIn\":true}'\n" "exit 1\n")
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="claude-code",
        status_command=[str(cli), "auth", "status", "--json"],
        login_command=[str(cli), "auth", "login"],
    )

    assert adapter.status().state == "unauthenticated"


def test_codex_status_parses_logged_out_text(tmp_path):
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' 'Not logged in'\n")
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="codex",
        status_command=[str(cli), "login", "status"],
        login_command=[str(cli), "login", "--device-auth"],
    )

    assert adapter.status().state == "unauthenticated"
    assert adapter.command() == [str(cli), "login", "--device-auth"]


def test_codex_status_parses_logged_in_text(tmp_path):
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' 'Logged in as a@example.test'\n")
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="codex",
        status_command=[str(cli), "login", "status"],
        login_command=[str(cli), "login", "--device-auth"],
    )

    assert adapter.status().state == "authenticated"


def test_command_adapter_redacts_status_output_and_replaces_invalid_bytes(tmp_path):
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\n" "printf 'token: super-secret '\n" "printf '\\377'\n")
    cli.chmod(0o755)
    adapter = CommandAuthAdapter(
        harness="codex",
        status_command=[str(cli), "login", "status"],
        login_command=[str(cli), "login", "--device-auth"],
    )

    status = adapter.status()

    assert status.state == "unknown"
    assert status.detail == "token: <redacted> \ufffd"


def test_default_auth_adapters_include_structured_harnesses(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex", "agy"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    adapters = default_auth_adapters()

    assert sorted(adapters) == ["agy", "claude-code", "codex"]


def test_default_auth_adapters_use_login_shell_command_and_nvm_path(
    monkeypatch, tmp_path
):
    nvm_bin = tmp_path / ".nvm/versions/node/v24.13.0/bin"
    nvm_bin.mkdir(parents=True)
    codex = nvm_bin / "codex"
    codex.write_text("#!/usr/bin/env node\n")
    codex.chmod(0o755)

    class _Completed:
        returncode = 0
        stdout = f"{codex}\n"

    monkeypatch.setattr(
        "drover.server.harness.auth.subprocess.run",
        lambda *args, **kwargs: _Completed(),
    )
    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)

    adapters = default_auth_adapters(shell="/bin/zsh")

    # Quote the way the adapter does. Building these expectations by bare
    # interpolation only passes while the temp root happens to be free of
    # shell metacharacters, so it broke the moment TMPDIR moved to a volume
    # with a space in its name — a test artifact, not a defect: the adapter
    # has always quoted correctly.
    quoted_bin = shlex.quote(str(nvm_bin))
    quoted_codex = shlex.quote(str(codex))

    adapter = adapters["codex"]
    assert adapter.status_command == [
        "/bin/zsh",
        "-lc",
        f"export PATH={quoted_bin}:$PATH; exec {quoted_codex} login status",
    ]
    assert adapter.command() == [
        "/bin/zsh",
        "-lc",
        f"export PATH={quoted_bin}:$PATH; exec {quoted_codex} login --device-auth",
    ]


def _agy_adapter(monkeypatch, tmp_path):
    """An agy adapter whose binary and home directory are both injected.

    ``Path.home()`` has to be redirected: agy's sign-in state lives in
    ``~/.gemini``, and reading the real one would make these assertions
    depend on whether this machine happens to be signed into agy.
    """
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 0\n")
    agy.chmod(0o755)
    monkeypatch.setattr(
        "drover.server.harness.auth.resolve_executable",
        lambda binary, *, login_shell: str(agy) if binary == "agy" else None,
    )
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: home)
    return default_auth_adapters()["agy"], agy, home


def test_agy_auth_status_reports_the_signed_in_account(monkeypatch, tmp_path):
    adapter, agy, home = _agy_adapter(monkeypatch, tmp_path)
    (home / ".gemini/google_accounts.json").write_text(
        json.dumps({"active": "someone@example.com", "old": []})
    )

    status = adapter.status()

    assert status.state == "authenticated"
    assert status.label == "someone@example.com"
    assert status.detail == "Antigravity CLI"
    assert adapter.command()[-1].endswith(f"exec {shlex.quote(str(agy))}")


def test_agy_auth_status_is_unknown_without_sign_in_state(monkeypatch, tmp_path):
    """``agy --version`` exits 0 whether or not anyone is signed in."""
    adapter, _agy, _home = _agy_adapter(monkeypatch, tmp_path)

    assert adapter.status().state == "unknown"


def test_agy_auth_status_accepts_oauth_blob_without_an_account_file(
    monkeypatch, tmp_path
):
    adapter, _agy, home = _agy_adapter(monkeypatch, tmp_path)
    (home / ".gemini/oauth_creds.json").write_text("{}")

    status = adapter.status()

    assert status.state == "authenticated"
    assert status.label is None


def test_agy_auth_status_accepts_antigravity_oauth_token_file(monkeypatch, tmp_path):
    adapter, _agy, home = _agy_adapter(monkeypatch, tmp_path)
    cli_dir = home / ".gemini/antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text("{}")

    status = adapter.status()

    assert status.state == "authenticated"
    assert status.label is None


def test_agy_auth_status_falls_back_to_old_account_list(monkeypatch, tmp_path):
    adapter, _agy, home = _agy_adapter(monkeypatch, tmp_path)
    (home / ".gemini/google_accounts.json").write_text(
        json.dumps({"active": None, "old": ["someone@example.com"]})
    )

    status = adapter.status()

    assert status.state == "authenticated"
    assert status.label == "someone@example.com"


def test_manager_starts_and_polls_successful_flow(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('Open https://example.test/device and enter ABCD-EFGH', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter}, timeout_s=5, retention_s=60)

    flow = manager.start("codex")
    assert flow["state"] in {"starting", "waiting_for_user"}

    deadline = time.time() + 5
    current = flow
    while time.time() < deadline:
        current = manager.snapshot("codex", flow["flow_id"])
        if current["state"] == "authenticated":
            break
        time.sleep(0.05)

    assert current["state"] == "authenticated"
    assert current["login_url"] == "https://example.test/device"
    assert current["user_code"] == "ABCD-EFGH"


def test_manager_extracts_pairing_code_before_redacting_message(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('Pairing code: ABCD-EFGH', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter}, timeout_s=5, retention_s=60)

    flow = manager.start("codex")
    current = wait_for_state(manager, "codex", flow["flow_id"], "authenticated")

    assert current["user_code"] == "ABCD-EFGH"
    assert current["message"] == "Pairing code: <redacted>"


def test_manager_wraps_launch_failures_as_structured_errors():
    adapter = StaticAuthAdapter("codex", start_command=["missing-cli"])
    manager = AuthFlowManager({"codex": adapter})

    with pytest.raises(AuthFlowLaunchError, match="could not start"):
        manager.start("codex")


def test_manager_replaces_malformed_output_bytes(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write("
        "b'Open https://example.test/device and enter WXYZ-1234 \\xff\\n'"
        ")\n"
        "sys.stdout.flush()\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    current = wait_for_state(manager, "codex", flow["flow_id"], "authenticated")

    assert current["message"] is not None
    assert "\ufffd" in current["message"]
    assert current["login_url"] == "https://example.test/device"
    assert current["user_code"] == "WXYZ-1234"


def test_manager_reuses_active_flow_for_duplicate_start():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    manager = AuthFlowManager({"codex": adapter})

    first = manager.start("codex")
    second = manager.start("codex")

    assert second["flow_id"] == first["flow_id"]
    assert manager.cancel("codex", first["flow_id"])["state"] == "cancelled"


def test_manager_marks_nonzero_exit_as_failed():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "raise SystemExit(3)"],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    failed = wait_for_state(manager, "codex", flow["flow_id"], "failed")

    assert failed["last_error"] == "authentication process exited with code 3"


def test_manager_requires_authenticated_status_after_zero_exit():
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus(
            "codex", "unauthenticated", detail="access token status-secret"
        ),
        start_command=[sys.executable, "-c", "pass"],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    failed = wait_for_state(manager, "codex", flow["flow_id"], "failed")

    assert "unauthenticated" in failed["last_error"]
    assert "status-secret" not in failed["last_error"]
    assert "<redacted>" in failed["last_error"]


def test_manager_cancels_active_flow():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")

    assert manager.cancel("codex", flow["flow_id"])["state"] == "cancelled"


def test_manager_kills_flow_that_ignores_termination(tmp_path):
    script = tmp_path / "ignore_term.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(5)\n"
    )
    adapter = StaticAuthAdapter("codex", start_command=[sys.executable, str(script)])
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    assert manager.cancel("codex", flow["flow_id"])["state"] == "cancelled"

    process = manager._flows_by_id[flow["flow_id"]].process
    deadline = time.time() + 1
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    assert process.poll() is not None


def test_manager_kills_descendant_process_group_on_cancel(tmp_path):
    heartbeat = tmp_path / "child-heartbeat"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, textwrap, time\n"
        f"heartbeat = {str(heartbeat)!r}\n"
        "child_code = textwrap.dedent(f'''\n"
        "    import pathlib, signal, time\n"
        "    path = pathlib.Path({heartbeat!r})\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True:\n"
        "        path.write_text(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "''')\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('Open https://example.test/device and enter KILL-0001', flush=True)\n"
        "time.sleep(10)\n"
    )
    adapter = StaticAuthAdapter("codex", start_command=[sys.executable, str(script)])
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    deadline = time.time() + 2
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert heartbeat.exists()

    assert manager.cancel("codex", flow["flow_id"])["state"] == "cancelled"
    heartbeat.unlink(missing_ok=True)
    time.sleep(0.2)

    assert not heartbeat.exists()
    assert manager._flows_by_id[flow["flow_id"]].process.poll() is not None


def test_manager_kills_descendant_after_launcher_exits(tmp_path):
    heartbeat = tmp_path / "orphan-heartbeat"
    script = tmp_path / "spawn_orphan.py"
    script.write_text(
        "import pathlib, subprocess, sys, textwrap, time\n"
        f"heartbeat = {str(heartbeat)!r}\n"
        "child_code = textwrap.dedent(f'''\n"
        "    import pathlib, signal, time\n"
        "    path = pathlib.Path({heartbeat!r})\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True:\n"
        "        path.write_text(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "''')\n"
        "subprocess.Popen([sys.executable, '-c', child_code], stdout=sys.stdout, stderr=subprocess.DEVNULL)\n"
        "deadline = time.time() + 2\n"
        "while not pathlib.Path(heartbeat).exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print('Open https://example.test/device and enter ORPH-0001', flush=True)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    deadline = time.time() + 2
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert heartbeat.exists()

    wait_for_state(manager, "codex", flow["flow_id"], "authenticated")
    heartbeat.unlink(missing_ok=True)
    time.sleep(0.2)

    assert not heartbeat.exists()


def test_manager_close_all_terminates_active_flows():
    adapters = {
        harness: StaticAuthAdapter(
            harness,
            start_command=[sys.executable, "-c", "import time; time.sleep(10)"],
        )
        for harness in ("claude-code", "codex")
    }
    manager = AuthFlowManager(adapters)
    flows = [manager.start(harness) for harness in adapters]

    manager.close_all()

    for flow in flows:
        managed = manager._flows_by_id[flow["flow_id"]]
        assert managed.process.poll() is not None
        assert (
            manager.snapshot(flow["harness"], flow["flow_id"])["state"] == "cancelled"
        )


def test_manager_expires_timed_out_flow():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    manager = AuthFlowManager({"codex": adapter}, timeout_s=0.01)

    flow = manager.start("codex")
    expired = wait_for_state(manager, "codex", flow["flow_id"], "expired")

    assert expired["last_error"] == "authentication flow expired"


def test_manager_expires_descendant_output(tmp_path):
    heartbeat = tmp_path / "timeout-heartbeat"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import pathlib, subprocess, sys, textwrap, time\n"
        f"heartbeat = {str(heartbeat)!r}\n"
        "child_code = textwrap.dedent(f'''\n"
        "    import pathlib, signal, time\n"
        "    path = pathlib.Path({heartbeat!r})\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True:\n"
        "        path.write_text(str(time.time()))\n"
        "        print('still waiting', flush=True)\n"
        "        time.sleep(0.05)\n"
        "''')\n"
        "subprocess.Popen([sys.executable, '-c', child_code], stdout=sys.stdout, stderr=subprocess.DEVNULL)\n"
        "print('Open https://example.test/device', flush=True)\n"
        "deadline = time.time() + 2\n"
        "while not pathlib.Path(heartbeat).exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(10)\n"
    )
    adapter = StaticAuthAdapter("codex", start_command=[sys.executable, str(script)])
    manager = AuthFlowManager({"codex": adapter}, timeout_s=0.2, retention_s=60)

    flow = manager.start("codex")
    try:
        deadline = time.time() + 2
        while not heartbeat.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert heartbeat.exists()

        expired = wait_for_state(
            manager, "codex", flow["flow_id"], "expired", timeout_s=3
        )

        assert expired["last_error"] == "authentication flow expired"
        heartbeat.unlink(missing_ok=True)
        time.sleep(0.2)
        assert not heartbeat.exists()
    finally:
        managed = manager._flows_by_id.get(flow["flow_id"])
        if managed is not None:
            manager._stop_process(managed.process, managed.pgid)


def test_manager_discards_terminal_flows_when_snapshot_is_read():
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, "-c", "pass"],
    )
    manager = AuthFlowManager({"codex": adapter}, retention_s=60)

    flow = manager.start("codex")
    wait_for_state(manager, "codex", flow["flow_id"], "authenticated")
    managed_flow = manager._flows_by_id[flow["flow_id"]]
    with managed_flow.lock:
        managed_flow.completed_at = time.time() - 61

    with pytest.raises(KeyError, match="unknown auth flow"):
        manager.snapshot("codex", flow["flow_id"])


def test_manager_retains_terminal_flow_after_new_flow_starts_for_same_harness():
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, "-c", "pass"],
    )
    manager = AuthFlowManager({"codex": adapter}, retention_s=60)

    first = manager.start("codex")
    completed = wait_for_state(manager, "codex", first["flow_id"], "authenticated")
    second = manager.start("codex")

    assert second["flow_id"] != first["flow_id"]
    assert manager.snapshot("codex", first["flow_id"]) == completed


def test_manager_discards_terminal_flow_without_followup_api_call():
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, "-c", "pass"],
    )
    manager = AuthFlowManager({"codex": adapter}, retention_s=0.2)

    flow = manager.start("codex")
    wait_for_state(manager, "codex", flow["flow_id"], "authenticated")

    deadline = time.time() + 1
    while time.time() < deadline:
        if flow["flow_id"] not in manager._flows_by_id:
            break
        time.sleep(0.01)

    assert flow["flow_id"] not in manager._flows_by_id


# --- Real-CLI regressions -------------------------------------------------
#
# The three cases below were all captured from the actual harness CLIs on a
# Mac mini; each broke sign-in from the iOS app in a different way. Keeping
# the literal CLI bytes here means a future rewrite of the output parser has
# to keep working against what the tools really emit, not a tidied-up
# paraphrase of it.


def test_redaction_keeps_the_oauth_authorize_code_flag():
    """``code=true`` is a request flag, not a credential.

    ``claude auth login`` builds its authorize URL with ``code=true``, which
    is what asks claude.com to display a pasteable code instead of
    completing a redirect. Redacting it turned the URL the app offers into a
    dead end: the browser opened, but no code was ever shown.
    """
    url = (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a"
        "&response_type=code&state=l4XrPFyhWvwEjxoiYHWxBAlMlwLqhZu"
    )

    redacted = redact_auth_text(url)

    assert "code=true" in redacted


def test_redaction_still_hides_a_real_authorization_code():
    text = "callback https://example.test/cb?code=ac_01H9XYZsecretvalue&state=ok"

    redacted = redact_auth_text(text)

    assert "ac_01H9XYZsecretvalue" not in redacted
    assert "code=<redacted>" in redacted


def test_manager_strips_ansi_colour_before_capturing_the_login_url(tmp_path):
    """codex colourises the device URL; the escape tail must not ride along.

    ``codex login --device-auth`` prints the URL wrapped in SGR colour
    codes. The trailing reset used to be captured as part of the URL, and
    iOS percent-encoded it into ``/device%1B%5B0m`` -- which is why tapping
    "Open Browser" landed on an OpenAI "your session has ended" page.
    """
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('   \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    current = wait_for_state(manager, "codex", flow["flow_id"], "authenticated")

    assert current["login_url"] == "https://auth.openai.com/codex/device"


def test_manager_strips_osc8_hyperlinks_before_capturing_the_login_url(tmp_path):
    """claude wraps its URL in an OSC-8 hyperlink, printing the target twice."""
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('visit: \\x1b]8;;https://claude.com/cai/oauth/authorize?code=true\\x1b\\\\"
        "\\x1b[94mhttps://claude.com/cai/oauth/authorize?code=true\\x1b[39m"
        "\\x1b]8;;\\x1b\\\\', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "claude-code",
        status_value=HarnessAuthStatus("claude-code", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"claude-code": adapter})

    flow = manager.start("claude-code")
    current = wait_for_state(manager, "claude-code", flow["flow_id"], "authenticated")

    assert current["login_url"] == "https://claude.com/cai/oauth/authorize?code=true"


def test_manager_extracts_device_codes_with_uneven_groups(tmp_path):
    """codex device codes are ``XXXX-XXXXX``; the matcher assumed 4-and-4.

    A real code from ``codex login --device-auth`` is e.g. ``P1KY-AS6MU``.
    The old pattern required every hyphen-separated group to be exactly four
    characters, so the code was silently never surfaced and the app showed a
    device page with nothing to type into it.
    """
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('   \\x1b[94mP1KY-AS6MU\\x1b[0m', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    current = wait_for_state(manager, "codex", flow["flow_id"], "authenticated")

    assert current["user_code"] == "P1KY-AS6MU"


# --- PTY-backed flows and typed input -------------------------------------


def test_claude_login_runs_under_a_pty_and_accepts_typed_input(tmp_path):
    """``claude auth login`` prompts on a terminal and reads the code back.

    Verified against the real CLI: with a pipe on stdin it prints the
    authorize URL and then hangs indefinitely, because its prompt never
    renders without a terminal. Under a PTY it prompts and completes. The
    pasted code therefore needs a route all the way back to the child.
    """
    script = tmp_path / "login.py"
    script.write_text(
        "import sys\n"
        "print('visit https://example.test/authorize?code=true', flush=True)\n"
        "answer = sys.stdin.readline().strip()\n"
        "sys.exit(0 if answer == 'PASTED-CODE' else 3)\n"
    )
    adapter = StaticAuthAdapter(
        "claude-code",
        status_value=HarnessAuthStatus("claude-code", "authenticated"),
        start_command=[sys.executable, str(script)],
        requires_pty=True,
    )
    manager = AuthFlowManager({"claude-code": adapter}, timeout_s=10, retention_s=60)

    flow = manager.start("claude-code")
    waiting = wait_for_state(
        manager, "claude-code", flow["flow_id"], "waiting_for_user", timeout_s=5
    )
    assert waiting["login_url"] == "https://example.test/authorize?code=true"
    assert waiting["supports_input"] is True

    manager.send_input("claude-code", flow["flow_id"], "PASTED-CODE")

    current = wait_for_state(
        manager, "claude-code", flow["flow_id"], "authenticated", timeout_s=5
    )
    assert current["state"] == "authenticated"


def test_pty_flow_reports_a_wrong_code_as_a_failure(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import sys\n"
        "print('visit https://example.test/authorize', flush=True)\n"
        "sys.stdin.readline()\n"
        "sys.exit(3)\n"
    )
    adapter = StaticAuthAdapter(
        "claude-code",
        status_value=HarnessAuthStatus("claude-code", "unauthenticated"),
        start_command=[sys.executable, str(script)],
        requires_pty=True,
    )
    manager = AuthFlowManager({"claude-code": adapter}, timeout_s=10, retention_s=60)

    flow = manager.start("claude-code")
    wait_for_state(
        manager, "claude-code", flow["flow_id"], "waiting_for_user", timeout_s=5
    )
    manager.send_input("claude-code", flow["flow_id"], "WRONG")

    current = wait_for_state(
        manager, "claude-code", flow["flow_id"], "failed", timeout_s=5
    )
    assert "3" in (current["last_error"] or "")


def test_pipe_backed_flows_do_not_advertise_input(tmp_path):
    """codex's device flow needs no typing; the app should not offer a field."""
    script = tmp_path / "login.py"
    script.write_text(
        "import time\nprint('https://example.test/device', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "authenticated"),
        start_command=[sys.executable, str(script)],
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")

    assert flow["supports_input"] is False
    with pytest.raises(RuntimeError, match="does not accept input"):
        manager.send_input("codex", flow["flow_id"], "anything")


def test_send_input_rejects_a_finished_flow(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import sys\nprint('https://example.test/authorize', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    adapter = StaticAuthAdapter(
        "claude-code",
        status_value=HarnessAuthStatus("claude-code", "authenticated"),
        start_command=[sys.executable, str(script)],
        requires_pty=True,
    )
    manager = AuthFlowManager({"claude-code": adapter}, timeout_s=10, retention_s=60)

    flow = manager.start("claude-code")
    manager.cancel("claude-code", flow["flow_id"])

    with pytest.raises(RuntimeError, match="no longer accepting input"):
        manager.send_input("claude-code", flow["flow_id"], "PASTED-CODE")


# --- Terminal-only harnesses ----------------------------------------------


def test_agy_sign_in_is_terminal_only(monkeypatch, tmp_path):
    """agy ships no login command at all.

    ``agy --help`` lists no ``login``/``auth`` subcommand, and ``agy login``
    is treated as a prompt: the bare binary opens a full-screen bubbletea
    TUI. Scraping cannot drive that, so the app is told to hand the user a
    real terminal instead of starting a flow that can only fail with
    "error opening TTY".
    """
    adapter, _agy, _home = _agy_adapter(monkeypatch, tmp_path)

    assert adapter.sign_in == "terminal"
    assert adapter.status().sign_in == "terminal"


def test_starting_a_terminal_only_flow_is_refused(monkeypatch, tmp_path):
    adapter, _agy, _home = _agy_adapter(monkeypatch, tmp_path)
    manager = AuthFlowManager({"agy": adapter})

    with pytest.raises(TerminalSignInRequired, match="terminal"):
        manager.start("agy")


def test_claude_and_codex_advertise_their_sign_in_modes(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex", "agy"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    adapters = default_auth_adapters()

    assert adapters["claude-code"].requires_pty is True
    assert adapters["claude-code"].sign_in == "flow"
    assert adapters["codex"].requires_pty is False
    assert adapters["codex"].sign_in == "flow"
    assert adapters["agy"].sign_in == "terminal"


def _dsh_in_nvm(tmp_path):
    nvm_bin = tmp_path / ".nvm/versions/node/v24.13.0/bin"
    nvm_bin.mkdir(parents=True)
    dsh = nvm_bin / "dsh"
    dsh.write_text("#!/usr/bin/env node\n")
    dsh.chmod(0o755)
    return dsh


def test_nvm_installed_dsh_is_still_discoverable(monkeypatch, tmp_path):
    """`npm i -g dsh` under nvm must not be hidden by the vendored-install check."""
    dsh = _dsh_in_nvm(tmp_path)
    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)

    assert _resolve_known_versioned_cli("dsh") == str(dsh)


def test_vendored_deepseek_harness_install_wins_over_nvm(monkeypatch, tmp_path):
    _dsh_in_nvm(tmp_path)
    vendored = tmp_path / ".local/share/deepseek-harness/node_modules/.bin/dsh"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("#!/usr/bin/env node\n")
    vendored.chmod(0o755)
    monkeypatch.setattr("drover.server.harness.auth.Path.home", lambda: tmp_path)

    assert _resolve_known_versioned_cli("dsh") == str(vendored)


def test_env_node_probe_reads_a_real_script(tmp_path):
    script = tmp_path / "cli"
    script.write_text("#!/usr/bin/env node\nconsole.log(1)\n")

    assert _uses_env_node(script) is True


def test_env_node_probe_rejects_another_interpreter(tmp_path):
    script = tmp_path / "cli"
    script.write_text('#!/bin/sh\nexec node "$@"\n')

    assert _uses_env_node(script) is False


def test_env_node_probe_rejects_a_binary_that_is_not_a_script(tmp_path):
    binary = tmp_path / "cli"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 4096)

    assert _uses_env_node(binary) is False


def test_env_node_probe_stops_at_the_shebang_limit(tmp_path):
    """A shebang is the first line, and kernels cap it far below this limit
    (Linux at 127 bytes). A file whose "env node" only appears past the probe
    window is not a node script, and reading far enough to find it is the
    defect: harness CLIs are hundreds of megabytes, and this runs on every
    HarnessDaemonState construction. See drover#238.
    """
    padded = tmp_path / "cli"
    padded.write_bytes(b"#!" + b"x" * (_SHEBANG_PROBE_BYTES * 2) + b"env node\n")

    assert _uses_env_node(padded) is False


def test_env_node_probe_does_not_read_the_whole_file(tmp_path, monkeypatch):
    """Correctness alone cannot catch a full read, so count the bytes."""
    binary = tmp_path / "cli"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * (4 * 1024 * 1024))

    read_sizes: list[int] = []
    real_open = Path.open

    class _CountingHandle:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            chunk = self._handle.read(size)
            read_sizes.append(len(chunk))
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

    def counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return _CountingHandle(handle) if self == binary else handle

    monkeypatch.setattr(Path, "open", counting_open)

    assert _uses_env_node(binary) is False
    assert sum(read_sizes) <= _SHEBANG_PROBE_BYTES
