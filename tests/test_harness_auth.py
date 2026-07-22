from __future__ import annotations

import sys
import time

from drover.server.harness.auth import (
    AuthFlowManager,
    HarnessAuthStatus,
    StaticAuthAdapter,
    redact_auth_text,
)


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
