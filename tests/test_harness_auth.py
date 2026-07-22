from __future__ import annotations

import sys
import time

import pytest

from drover.server.harness.auth import (
    AuthFlowLaunchError,
    AuthFlowManager,
    CommandAuthAdapter,
    HarnessAuthStatus,
    StaticAuthAdapter,
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
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature", "eyJhbGciOiJIUzI1NiJ9"),
        ('{"OPENAI_API_KEY":"openai-secret"}', "openai-secret"),
        ("ANTHROPIC_API_KEY: anthropic-secret", "anthropic-secret"),
        ("CLAUDE_CODE_OAUTH_TOKEN = oauth-secret", "oauth-secret"),
    ],
)
def test_redact_auth_text_removes_common_auth_secret_formats(text, secret):
    redacted = redact_auth_text(text)

    assert secret not in redacted
    assert "<redacted>" in redacted


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
    }


def test_claude_status_parses_logged_in_json(tmp_path):
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"loggedIn\":true,\"email\":\"a@example.test\",\"subscriptionType\":\"max\"}'\n"
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
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"loggedIn\":true}'\n"
        "exit 1\n"
    )
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
    cli.write_text(
        "#!/bin/sh\n"
        "printf 'token: super-secret '\n"
        "printf '\\377'\n"
    )
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
    for name in ("claude", "codex", "gemini"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    adapters = default_auth_adapters()

    assert sorted(adapters) == ["claude-code", "codex", "gemini"]


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

    adapter = adapters["codex"]
    assert adapter.status_command == [
        "/bin/zsh",
        "-lc",
        f"export PATH={nvm_bin}:$PATH; exec {codex} login status",
    ]
    assert adapter.command() == [
        "/bin/zsh",
        "-lc",
        f"export PATH={nvm_bin}:$PATH; exec {codex} login --device-auth",
    ]


def test_gemini_auth_is_non_authoritative_and_non_startable(monkeypatch, tmp_path):
    gemini = tmp_path / "gemini"
    gemini.write_text("#!/bin/sh\nexit 0\n")
    gemini.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")

    adapter = default_auth_adapters()["gemini"]

    status = adapter.status()

    assert status.state == "unknown"
    assert status.detail == "GEMINI_API_KEY set"
    with pytest.raises(RuntimeError, match="auth is not supported for gemini"):
        adapter.command()


def test_manager_starts_and_polls_successful_flow(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('Open https://example.test/device and enter ABCD-EFGH', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    adapter = StaticAuthAdapter(
        "codex",
        status_value=HarnessAuthStatus("codex", "unauthenticated"),
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
        status_value=HarnessAuthStatus("codex", "unauthenticated"),
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
    adapter = StaticAuthAdapter(
        "codex", start_command=[sys.executable, str(script)]
    )
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
    adapter = StaticAuthAdapter(
        "codex", start_command=[sys.executable, str(script)]
    )
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
        "import subprocess, sys, textwrap\n"
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
        "print('Open https://example.test/device and enter ORPH-0001', flush=True)\n"
    )
    adapter = StaticAuthAdapter(
        "codex", start_command=[sys.executable, str(script)]
    )
    manager = AuthFlowManager({"codex": adapter})

    flow = manager.start("codex")
    deadline = time.time() + 2
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert heartbeat.exists()

    manager._stop_process(
        manager._flows_by_id[flow["flow_id"]].process,
        manager._flows_by_id[flow["flow_id"]].pgid,
    )
    heartbeat.unlink(missing_ok=True)
    time.sleep(0.2)

    assert not heartbeat.exists()


def test_manager_expires_timed_out_flow():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    manager = AuthFlowManager({"codex": adapter}, timeout_s=0.01)

    flow = manager.start("codex")
    expired = wait_for_state(manager, "codex", flow["flow_id"], "expired")

    assert expired["last_error"] == "authentication flow expired"


def test_manager_expires_descendant_output_after_launcher_exits(tmp_path):
    heartbeat = tmp_path / "timeout-heartbeat"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, textwrap\n"
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
    )
    adapter = StaticAuthAdapter(
        "codex", start_command=[sys.executable, str(script)]
    )
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
