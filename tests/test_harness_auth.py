from __future__ import annotations

import sys
import time

import pytest

from drover.server.harness.auth import (
    AuthFlowManager,
    HarnessAuthStatus,
    StaticAuthAdapter,
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


def test_manager_expires_timed_out_flow():
    adapter = StaticAuthAdapter(
        "codex",
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    manager = AuthFlowManager({"codex": adapter}, timeout_s=0.01)

    flow = manager.start("codex")
    expired = wait_for_state(manager, "codex", flow["flow_id"], "expired")

    assert expired["last_error"] == "authentication flow expired"


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
