# Harness Auth Flows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build app-driven interactive authentication repair for Claude Code, Codex, and Gemini harness CLIs.

**Architecture:** `drover-harnessd` owns host-local provider auth adapters and short-lived flow state. The central server proxies host auth endpoints using the existing harness host registry. The iOS app adds typed models, client calls, and a small auth sheet that opens provider URLs without ever collecting provider secrets.

**Tech Stack:** Python 3.12 stdlib HTTP server, subprocess/threading, pytest, Swift 6, SwiftUI, Swift Testing.

## Global Constraints

- Interactive-flow first; no provider password entry, API-key paste flows, OAuth token storage, or provider OAuth reimplementation in v1.
- Provider credentials stay on the harness host and are managed by provider CLIs.
- Central and app must use existing Drover bearer-token auth; no direct app-to-harnessd calls.
- Flow runtime defaults to 10 minutes; terminal flow snapshots are retained in memory for 10 minutes.
- Redact likely secrets from all CLI output before serializing to central/app.
- Gemini status and start behavior is best-effort and must not claim Claude/Codex-grade machine-readable guarantees unless local CLI evidence supports it during implementation.
- Stage and commit only files touched by each task; do not stage the pre-existing `src/drover/server/harness/structured/claude.py`, `tests/test_structured_claude.py`, or `apps/drover/NexusKit/.build/` changes unless the user explicitly expands scope.

---

## File Structure

- Create `src/drover/server/harness/auth.py`: auth dataclasses, JSON helpers, redaction, provider adapters, process-backed flow manager.
- Modify `src/drover/server/harness/daemon.py`: add `auth` manager to `HarnessDaemonState` and expose harnessd auth routes.
- Modify `src/drover/server/metrics.py`: add central proxy methods for host auth status/start/poll/cancel.
- Modify `src/drover/server/web/app.py`: add central HTTP routes for `/harness/hosts/{host_id}/auth/...`.
- Test `tests/test_harness_auth.py`: unit tests for redaction, status parsing, flow lifecycle, fake provider flows.
- Test `tests/test_harness_daemon.py`: harnessd auth endpoint tests.
- Test `tests/test_harness_auth_proxy.py`: central proxy route tests.
- Modify `apps/drover/NexusKit/Sources/NexusKit/Models.swift`: add `HarnessAuthStatus` and `HarnessAuthFlow`.
- Modify `apps/drover/NexusKit/Sources/NexusKit/NexusClient.swift`: add four auth endpoint methods.
- Create `apps/drover/NexusKit/Sources/NexusKit/AuthFlowModel.swift`: observable polling/start/cancel state.
- Modify `apps/drover/NexusKitTests/ModelsTests.swift`, `ClientTests.swift`: model/client tests.
- Create `apps/drover/NexusKitTests/AuthFlowModelTests.swift`: flow model tests.
- Create `apps/drover/Drover/Screens/Auth/HarnessAuthSheet.swift`: provider auth UI.
- Modify `apps/drover/Drover/Screens/Launch/LaunchView.swift`: add auth entry point for selected host/harness.

---

### Task 1: Core Auth Types, Redaction, And Flow Manager

**Files:**
- Create: `src/drover/server/harness/auth.py`
- Test: `tests/test_harness_auth.py`

**Interfaces:**
- Produces: `HarnessAuthStatus`, `HarnessAuthFlowSnapshot`, `AuthFlowManager`, `StaticAuthAdapter`, `redact_auth_text(text: str) -> str`.
- Consumes: only Python stdlib.

- [ ] **Step 1: Write failing tests for redaction and manager basics**

Add `tests/test_harness_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_auth.py -q`

Expected: import failure for `drover.server.harness.auth`.

- [ ] **Step 3: Implement core module**

Create `src/drover/server/harness/auth.py` with these public pieces:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Protocol
from uuid import uuid4


_SECRET_QUERY_KEYS = {
    "token", "code", "access_token", "refresh_token", "id_token",
    "client_secret", "api_key", "key", "secret",
}
_URL_RE = re.compile(r"https?://[^\\s)'\\\"]+")
_USER_CODE_RE = re.compile(r"\\b[A-Z0-9]{4}(?:-[A-Z0-9]{4})+\\b")


@dataclass(frozen=True)
class HarnessAuthStatus:
    harness: str
    state: str
    label: str | None = None
    detail: str | None = None

    def as_json(self, *, host_id: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "harness": self.harness,
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
        }
        if host_id is not None:
            data["host_id"] = host_id
        return data


@dataclass(frozen=True)
class HarnessAuthFlowSnapshot:
    flow_id: str
    harness: str
    state: str
    login_url: str | None = None
    device_code: str | None = None
    user_code: str | None = None
    message: str | None = None
    expires_at: str | None = None
    last_error: str | None = None

    def as_json(self, *, host_id: str | None = None) -> dict[str, Any]:
        data = {
            "flow_id": self.flow_id,
            "harness": self.harness,
            "state": self.state,
            "login_url": self.login_url,
            "device_code": self.device_code,
            "user_code": self.user_code,
            "message": self.message,
            "expires_at": self.expires_at,
            "last_error": self.last_error,
        }
        if host_id is not None:
            data["host_id"] = host_id
        return data


class HarnessAuthAdapter(Protocol):
    harness: str

    def status(self) -> HarnessAuthStatus: ...
    def command(self) -> list[str]: ...


@dataclass(frozen=True)
class StaticAuthAdapter:
    harness: str
    status_value: HarnessAuthStatus | None = None
    start_command: list[str] | None = None

    def status(self) -> HarnessAuthStatus:
        return self.status_value or HarnessAuthStatus(
            self.harness,
            "unavailable",
            detail=f"auth is not supported for {self.harness}",
        )

    def command(self) -> list[str]:
        if not self.start_command:
            raise RuntimeError(f"auth is not supported for {self.harness}")
        return self.start_command


def redact_auth_text(text: str) -> str:
    redacted = text
    for key in sorted(_SECRET_QUERY_KEYS, key=len, reverse=True):
        redacted = re.sub(
            rf"(?i)({re.escape(key)}=)[^&\\s]+",
            rf"\\1<redacted>",
            redacted,
        )
    return redacted
```

Then add an internal `_AuthFlow` class and `AuthFlowManager`:

```python
@dataclass
class _AuthFlow:
    flow_id: str
    harness: str
    process: subprocess.Popen[str]
    started_at: float
    timeout_s: float
    state: str = "starting"
    login_url: str | None = None
    user_code: str | None = None
    device_code: str | None = None
    message: str | None = None
    last_error: str | None = None
    output_tail: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> HarnessAuthFlowSnapshot:
        with self.lock:
            expires_at = datetime.fromtimestamp(
                self.started_at + self.timeout_s,
                timezone.utc,
            ).isoformat()
            return HarnessAuthFlowSnapshot(
                flow_id=self.flow_id,
                harness=self.harness,
                state=self.state,
                login_url=self.login_url,
                device_code=self.device_code,
                user_code=self.user_code,
                message=self.message,
                expires_at=expires_at,
                last_error=self.last_error,
            )
```

Implement `AuthFlowManager` with `status(harness)`, `start(harness)`,
`snapshot(harness, flow_id)`, and `cancel(harness, flow_id)`. `start()` must
return an existing non-terminal flow for that harness instead of spawning a
duplicate. Spawn with `stdout=PIPE`, `stderr=STDOUT`, `text=True`, `bufsize=1`.
The worker thread reads lines, redacts them, extracts the first URL with
`_URL_RE`, extracts the first user code with `_USER_CODE_RE`, updates message,
and marks the flow `authenticated` on return code 0 or `failed` on nonzero.
If the timeout elapses, terminate the process and mark `expired`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_auth.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/auth.py tests/test_harness_auth.py
git commit -m "feat(harness): add auth flow manager"
```

---

### Task 2: Claude, Codex, And Gemini Auth Adapters

**Files:**
- Modify: `src/drover/server/harness/auth.py`
- Modify: `tests/test_harness_auth.py`

**Interfaces:**
- Consumes: `HarnessAuthStatus`, `HarnessAuthAdapter`.
- Produces: `default_auth_adapters() -> dict[str, HarnessAuthAdapter]`, `CommandAuthAdapter`.

- [ ] **Step 1: Add failing tests for provider status parsing and commands**

Append tests:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_auth.py -q`

Expected: import/name failures for `CommandAuthAdapter` and
`default_auth_adapters`.

- [ ] **Step 3: Implement command adapter**

Add imports `json` and `shutil`. Add:

```python
@dataclass(frozen=True)
class CommandAuthAdapter:
    harness: str
    status_command: list[str]
    login_command: list[str]

    def status(self) -> HarnessAuthStatus:
        try:
            result = subprocess.run(
                self.status_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
                check=False,
            )
        except FileNotFoundError:
            return HarnessAuthStatus(self.harness, "unavailable", detail="CLI not found")
        except subprocess.TimeoutExpired:
            return HarnessAuthStatus(self.harness, "unknown", detail="status timed out")

        output = redact_auth_text(result.stdout or "").strip()
        if self.harness == "claude-code":
            return _parse_claude_status(output, result.returncode)
        if self.harness == "codex":
            return _parse_codex_status(output, result.returncode)
        if self.harness == "gemini":
            return _parse_gemini_status(output, result.returncode)
        return HarnessAuthStatus(self.harness, "unknown", detail=output or None)

    def command(self) -> list[str]:
        return self.login_command
```

Add parser helpers:

```python
def _parse_claude_status(output: str, returncode: int) -> HarnessAuthStatus:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        data = {}
    if data.get("loggedIn") is True:
        return HarnessAuthStatus(
            "claude-code",
            "authenticated",
            label=data.get("email"),
            detail=data.get("subscriptionType") or data.get("authMethod"),
        )
    if data.get("loggedIn") is False or returncode != 0:
        return HarnessAuthStatus("claude-code", "unauthenticated", detail=output or None)
    return HarnessAuthStatus("claude-code", "unknown", detail=output or None)


def _parse_codex_status(output: str, returncode: int) -> HarnessAuthStatus:
    lowered = output.lower()
    if "logged in" in lowered:
        return HarnessAuthStatus("codex", "authenticated", detail=output or None)
    if "not logged in" in lowered or "logged out" in lowered or returncode != 0:
        return HarnessAuthStatus("codex", "unauthenticated", detail=output or None)
    return HarnessAuthStatus("codex", "unknown", detail=output or None)


def _parse_gemini_status(output: str, returncode: int) -> HarnessAuthStatus:
    if os.environ.get("GEMINI_API_KEY"):
        return HarnessAuthStatus("gemini", "authenticated", detail="GEMINI_API_KEY set")
    settings = Path.home() / ".gemini/settings.json"
    accounts = Path.home() / ".gemini/google_accounts.json"
    if settings.exists() or accounts.exists():
        return HarnessAuthStatus("gemini", "unknown", detail="Gemini config present")
    return HarnessAuthStatus("gemini", "unknown", detail=output or None)
```

Add:

```python
def default_auth_adapters() -> dict[str, HarnessAuthAdapter]:
    adapters: dict[str, HarnessAuthAdapter] = {}
    claude = shutil.which("claude")
    if claude:
        adapters["claude-code"] = CommandAuthAdapter(
            "claude-code",
            [claude, "auth", "status", "--json"],
            [claude, "auth", "login"],
        )
    codex = shutil.which("codex")
    if codex:
        adapters["codex"] = CommandAuthAdapter(
            "codex",
            [codex, "login", "status"],
            [codex, "login", "--device-auth"],
        )
    gemini = shutil.which("gemini")
    if gemini:
        adapters["gemini"] = CommandAuthAdapter(
            "gemini",
            [gemini, "--version"],
            [gemini],
        )
    return adapters
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_auth.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/drover/server/harness/auth.py tests/test_harness_auth.py
git commit -m "feat(harness): add provider auth adapters"
```

---

### Task 3: Harnessd Auth Endpoints

**Files:**
- Modify: `src/drover/server/harness/daemon.py`
- Modify: `tests/test_harness_daemon.py`

**Interfaces:**
- Consumes: `AuthFlowManager`, `default_auth_adapters`.
- Produces host-local routes `/auth/{harness}/status`, `/auth/{harness}/start`, `/auth/{harness}/flows/{flow_id}`, `/auth/{harness}/flows/{flow_id}/cancel`.

- [ ] **Step 1: Add failing harnessd route tests**

In `tests/test_harness_daemon.py`, import:

```python
from drover.server.harness.auth import AuthFlowManager, HarnessAuthStatus, StaticAuthAdapter
```

Add tests:

```python
def test_harnessd_auth_status_route(tmp_path):
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager({
        "claude-code": StaticAuthAdapter(
            "claude-code",
            status_value=HarnessAuthStatus("claude-code", "unauthenticated"),
        )
    })
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/claude-code/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert response.status == 200
    assert body["host_id"] == "test-host"
    assert body["harness"] == "claude-code"
    assert body["state"] == "unauthenticated"


def test_harnessd_auth_start_poll_and_cancel(tmp_path):
    script = tmp_path / "login.py"
    script.write_text(
        "import time\n"
        "print('Open https://example.test/device and enter WXYZ-1234', flush=True)\n"
        "time.sleep(5)\n"
    )
    server, state, base_url = _start_test_server(tmp_path, api_token="secret")
    state.auth = AuthFlowManager({
        "codex": StaticAuthAdapter(
            "codex",
            status_value=HarnessAuthStatus("codex", "unauthenticated"),
            start_command=[sys.executable, str(script)],
        )
    }, timeout_s=30)
    try:
        req = urllib.request.Request(
            f"{base_url}/auth/codex/start",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            started = json.loads(response.read().decode("utf-8"))
        flow_id = started["flow_id"]

        poll_req = urllib.request.Request(
            f"{base_url}/auth/codex/flows/{flow_id}",
            headers={"Authorization": "Bearer secret"},
        )
        _wait_until(
            lambda: "login_url" in json.loads(
                urllib.request.urlopen(poll_req, timeout=5).read().decode("utf-8")
            )
        )

        cancel_req = urllib.request.Request(
            f"{base_url}/auth/codex/flows/{flow_id}/cancel",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(cancel_req, timeout=5) as response:
            cancelled = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        state.pty.close_all()

    assert started["host_id"] == "test-host"
    assert started["harness"] == "codex"
    assert cancelled["state"] == "cancelled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_daemon.py::test_harnessd_auth_status_route tests/test_harness_daemon.py::test_harnessd_auth_start_poll_and_cancel -q`

Expected: `HarnessDaemonState` has no `auth` field or routes return 404.

- [ ] **Step 3: Wire auth manager into daemon state**

In `src/drover/server/harness/daemon.py`, import:

```python
from drover.server.harness.auth import AuthFlowManager, default_auth_adapters
```

Add to `HarnessDaemonState`:

```python
auth: AuthFlowManager = field(
    default_factory=lambda: AuthFlowManager(default_auth_adapters())
)
```

- [ ] **Step 4: Add route parsing helpers and handlers**

In `HarnessRequestHandler.do_GET`, before session routes:

```python
auth_route = _parse_auth_route(parsed.path)
if auth_route and auth_route[2] == "status":
    self._auth_status(auth_route[0])
    return
if auth_route and auth_route[2] == "flow":
    self._auth_flow(auth_route[0], auth_route[1] or "")
    return
```

In `do_POST`, before session routes:

```python
auth_route = _parse_auth_route(parsed.path)
if auth_route and auth_route[2] == "start":
    self._auth_start(auth_route[0])
    return
if auth_route and auth_route[2] == "cancel":
    self._auth_cancel(auth_route[0], auth_route[1] or "")
    return
```

Add handlers:

```python
def _auth_status(self, harness: str) -> None:
    status = self.server.state.auth.status(harness)
    code = HTTPStatus.NOT_FOUND if status.state == "unavailable" else HTTPStatus.OK
    self._write_json(status.as_json(host_id=self.server.state.host_id), status=code)

def _auth_start(self, harness: str) -> None:
    snapshot = self.server.state.auth.start(harness)
    code = HTTPStatus.NOT_FOUND if snapshot.get("state") == "unavailable" else HTTPStatus.ACCEPTED
    snapshot["host_id"] = self.server.state.host_id
    self._write_json(snapshot, status=code)

def _auth_flow(self, harness: str, flow_id: str) -> None:
    snapshot = self.server.state.auth.snapshot(harness, flow_id)
    code = HTTPStatus.NOT_FOUND if snapshot.get("error") else HTTPStatus.OK
    snapshot.setdefault("host_id", self.server.state.host_id)
    self._write_json(snapshot, status=code)

def _auth_cancel(self, harness: str, flow_id: str) -> None:
    snapshot = self.server.state.auth.cancel(harness, flow_id)
    code = HTTPStatus.NOT_FOUND if snapshot.get("error") else HTTPStatus.OK
    snapshot.setdefault("host_id", self.server.state.host_id)
    self._write_json(snapshot, status=code)
```

Add module helper:

```python
def _parse_auth_route(path: str) -> tuple[str, str | None, str] | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) == 3 and parts[0] == "auth" and parts[2] in {"status", "start"}:
        return parts[1], None, parts[2]
    if len(parts) == 4 and parts[0] == "auth" and parts[2] == "flows":
        return parts[1], parts[3], "flow"
    if len(parts) == 5 and parts[0] == "auth" and parts[2] == "flows" and parts[4] == "cancel":
        return parts[1], parts[3], "cancel"
    return None
```

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_harness_daemon.py::test_harnessd_auth_status_route tests/test_harness_daemon.py::test_harnessd_auth_start_poll_and_cancel -q`

Expected: both tests pass.

- [ ] **Step 6: Run adjacent tests**

Run: `uv run pytest tests/test_harness_auth.py tests/test_harness_daemon.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/drover/server/harness/daemon.py tests/test_harness_daemon.py
git commit -m "feat(harness): expose auth flow endpoints"
```

---

### Task 4: Central Auth Proxy Routes

**Files:**
- Modify: `src/drover/server/metrics.py`
- Modify: `src/drover/server/web/app.py`
- Test: create `tests/test_harness_auth_proxy.py`

**Interfaces:**
- Consumes: harnessd auth routes from Task 3.
- Produces: central routes under `/harness/hosts/{host_id}/auth/{harness}`.

- [ ] **Step 1: Write failing proxy tests**

Create `tests/test_harness_auth_proxy.py`:

```python
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server


class _HarnessAuthHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802
        self.__class__.requests.append({
            "method": "GET",
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        body = json.dumps({"harness": "codex", "state": "unauthenticated"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.__class__.requests.append({
            "method": "POST",
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        body = json.dumps({
            "harness": "codex",
            "flow_id": "auth-flow-1",
            "state": "waiting_for_user",
        }).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _start_central(tmp_path, upstream_url: str):
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    registry = HarnessRegistry(duckdb_path)
    registry.register_host(
        host_id="mac-mini",
        display_name="Mac Mini",
        kind="mac",
        local_url=upstream_url,
        status="online",
        capabilities={"harnesses": [{"name": "codex", "enabled": True}]},
    )
    collector = MetricsCollector(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector.api_token = "secret"
    from drover.server.web.auth import AuthSettings

    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token="secret"),
    )
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_central_proxies_auth_status(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _HarnessAuthHandler)
    _HarnessAuthHandler.requests = []
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    central, base = _start_central(tmp_path, f"http://127.0.0.1:{upstream.server_address[1]}")
    try:
        req = urllib.request.Request(
            f"{base}/harness/hosts/mac-mini/auth/codex/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode())
    finally:
        central.shutdown(); central.server_close()
        upstream.shutdown(); upstream.server_close()

    assert response.status == 200
    assert body["host_id"] == "mac-mini"
    assert body["state"] == "unauthenticated"
    assert _HarnessAuthHandler.requests[0]["path"] == "/auth/codex/status"
    assert _HarnessAuthHandler.requests[0]["authorization"] == "Bearer secret"
```

Add a second test for `POST /start` mirroring the route and asserting status
202 and `host_id`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_auth_proxy.py -q`

Expected: route 404 because the central auth proxy routes do not exist yet.

- [ ] **Step 3: Add proxy method in metrics**

In `MetricsCollector`, add:

```python
def proxy_harness_auth(
    self,
    host_id: str,
    harness: str,
    action: str,
    *,
    flow_id: str | None = None,
) -> tuple[int, str]:
    host = self._harness_host(host_id)
    if host is None:
        return _json_response(404, {"error": f"unknown harness host: {host_id}"})
    endpoint = _harness_endpoint(host)
    if not endpoint:
        return _json_response(502, {"error": f"harness host has no registered endpoint: {host_id}"})

    if action in {"status", "start"}:
        path = f"/auth/{quote(harness, safe='')}/{action}"
    elif action in {"flow", "cancel"} and flow_id:
        suffix = "" if action == "flow" else "/cancel"
        path = f"/auth/{quote(harness, safe='')}/flows/{quote(flow_id, safe='')}{suffix}"
    else:
        return _json_response(400, {"error": "invalid auth action"})

    status, body = self._proxy_harness_request(
        f"{endpoint}{path}",
        method="GET" if action in {"status", "flow"} else "POST",
        payload={},
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return status, body
    if isinstance(payload, dict):
        payload.setdefault("host_id", host_id)
        payload.setdefault("harness", harness)
        return _json_response(status, payload)
    return status, body
```

Import `quote` from `urllib.parse` if not already present.

- [ ] **Step 4: Add central HTTP routes**

In `src/drover/server/web/app.py`, add GET cases before `/harness/hosts/{id}/native-sessions`:

```python
auth_route = _parse_host_auth_route(path)
if auth_route and auth_route["method"] == "GET":
    status, body = self.collector.proxy_harness_auth(
        auth_route["host_id"],
        auth_route["harness"],
        auth_route["action"],
        flow_id=auth_route.get("flow_id"),
    )
    self._send(status, "application/json", body)
    return
```

Add POST cases in `do_POST` before `/harness/hosts/{id}/sessions`:

```python
auth_route = _parse_host_auth_route(path)
if auth_route and auth_route["method"] == "POST":
    status, body = self.collector.proxy_harness_auth(
        auth_route["host_id"],
        auth_route["harness"],
        auth_route["action"],
        flow_id=auth_route.get("flow_id"),
    )
    self._send(status, "application/json", body)
    return
```

Add helper near other module helpers:

```python
def _parse_host_auth_route(path: str) -> dict[str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if len(parts) == 6 and parts[:2] == ["harness", "hosts"] and parts[3] == "auth" and parts[5] in {"status", "start"}:
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "action": parts[5],
            "method": "GET" if parts[5] == "status" else "POST",
        }
    if len(parts) == 7 and parts[:2] == ["harness", "hosts"] and parts[3] == "auth" and parts[5] == "flows":
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "flow",
            "method": "GET",
        }
    if len(parts) == 8 and parts[:2] == ["harness", "hosts"] and parts[3] == "auth" and parts[5] == "flows" and parts[7] == "cancel":
        return {
            "host_id": parts[2],
            "harness": parts[4],
            "flow_id": parts[6],
            "action": "cancel",
            "method": "POST",
        }
    return None
```

- [ ] **Step 5: Run proxy tests**

Run: `uv run pytest tests/test_harness_auth_proxy.py -q`

Expected: all proxy tests pass.

- [ ] **Step 6: Run server/harness tests**

Run: `uv run pytest tests/test_harness_auth.py tests/test_harness_daemon.py tests/test_harness_auth_proxy.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/drover/server/metrics.py src/drover/server/web/app.py tests/test_harness_auth_proxy.py
git commit -m "feat(server): proxy harness auth flows"
```

---

### Task 5: Swift Models And Client Methods

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/Models.swift`
- Modify: `apps/drover/NexusKit/Sources/NexusKit/NexusClient.swift`
- Modify: `apps/drover/NexusKitTests/ModelsTests.swift`
- Modify: `apps/drover/NexusKitTests/ClientTests.swift`

**Interfaces:**
- Produces: `HarnessAuthStatus`, `HarnessAuthFlow`, `HarnessAuthState`, `NexusClient.authStatus`, `NexusClient.startAuthFlow`, `NexusClient.authFlow`, `NexusClient.cancelAuthFlow`.

- [ ] **Step 1: Add failing model tests**

Append to `ModelsTests.swift`:

```swift
@Test func harnessAuthStatusDecodes() throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"codex","state":"unauthenticated",
     "label":null,"detail":"Not logged in"}
    """.utf8)
    let status = try JSONDecoder().decode(HarnessAuthStatus.self, from: data)
    #expect(status.hostID == "mac-mini")
    #expect(status.harness == "codex")
    #expect(status.state == .unauthenticated)
    #expect(status.detail == "Not logged in")
}

@Test func harnessAuthFlowDecodesUnknownStateLeniently() throws {
    let data = Data("""
    {"host_id":"mac-mini","harness":"gemini","flow_id":"auth-flow-1",
     "state":"provider_weird","login_url":"https://example.test",
     "user_code":"ABCD-EFGH","message":"Open browser"}
    """.utf8)
    let flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: data)
    #expect(flow.state == .unknown)
    #expect(flow.loginURL?.absoluteString == "https://example.test")
    #expect(flow.userCode == "ABCD-EFGH")
}
```

- [ ] **Step 2: Add failing client tests**

Append to `ClientTests.swift`:

```swift
@Test func authStatusRouteShape() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/status")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","state":"unauthenticated"}"#.utf8))
    }
    let status = try await client().authStatus(hostID: "mac-mini", harness: "codex")
    #expect(status.state == .unauthenticated)
}

@Test func startAuthFlowPostsAndDecodes() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
        #expect(request.httpMethod == "POST")
        return (202, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","user_code":"ABCD-EFGH"}"#.utf8))
    }
    let flow = try await client().startAuthFlow(hostID: "mac-mini", harness: "codex")
    #expect(flow.flowID == "auth-flow-1")
    #expect(flow.state == .waitingForUser)
}

@Test func pollAndCancelAuthFlowRoutes() async throws {
    var seen: [String] = []
    MockURLProtocol.handler = { request in
        seen.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
    }
    _ = try await client().authFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    _ = try await client().cancelAuthFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    #expect(seen == [
        "GET /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1",
        "POST /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel",
    ])
}
```

- [ ] **Step 3: Run Swift tests to verify failure**

Run: `cd apps/drover/NexusKit && swift test --filter ModelsTests`

Expected: missing `HarnessAuthStatus`/`HarnessAuthFlow`.

Run: `cd apps/drover/NexusKit && swift test --filter ClientTests`

Expected: missing `NexusClient` auth methods.

- [ ] **Step 4: Implement models**

Add to `Models.swift`:

```swift
public enum HarnessAuthState: String, Sendable, Equatable, Decodable {
    case authenticated
    case unauthenticated
    case unknown
    case unavailable
    case starting
    case waitingForUser = "waiting_for_user"
    case failed
    case cancelled
    case expired

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = HarnessAuthState(rawValue: raw) ?? .unknown
    }
}

public struct HarnessAuthStatus: Sendable, Equatable, Decodable {
    public var hostID: String
    public var harness: String
    public var state: HarnessAuthState
    public var label: String?
    public var detail: String?

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case harness
        case state
        case label
        case detail
    }
}

public struct HarnessAuthFlow: Sendable, Equatable, Decodable {
    public var hostID: String
    public var harness: String
    public var flowID: String
    public var state: HarnessAuthState
    public var loginURL: URL?
    public var deviceCode: String?
    public var userCode: String?
    public var message: String?
    public var expiresAt: Date?
    public var lastError: String?

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case harness
        case flowID = "flow_id"
        case state
        case loginURL = "login_url"
        case deviceCode = "device_code"
        case userCode = "user_code"
        case message
        case expiresAt = "expires_at"
        case lastError = "last_error"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hostID = (try? container.decode(String.self, forKey: .hostID)) ?? ""
        harness = try container.decode(String.self, forKey: .harness)
        flowID = try container.decode(String.self, forKey: .flowID)
        state = try container.decode(HarnessAuthState.self, forKey: .state)
        if let raw = try? container.decode(String.self, forKey: .loginURL) {
            loginURL = URL(string: raw)
        }
        deviceCode = try? container.decode(String.self, forKey: .deviceCode)
        userCode = try? container.decode(String.self, forKey: .userCode)
        message = try? container.decode(String.self, forKey: .message)
        let rawExpires = try? container.decode(String.self, forKey: .expiresAt)
        expiresAt = WireDate.parse(rawExpires)
        lastError = try? container.decode(String.self, forKey: .lastError)
    }

    public var isTerminal: Bool {
        state == .authenticated || state == .failed || state == .cancelled || state == .expired
    }
}
```

- [ ] **Step 5: Implement client methods**

Add to `NexusClient` public API:

```swift
public func authStatus(hostID: String, harness: String) async throws -> HarnessAuthStatus {
    let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/status"
    let data = try await request(path: path, method: "GET", body: nil)
    return try decode(HarnessAuthStatus.self, from: data)
}

public func startAuthFlow(hostID: String, harness: String) async throws -> HarnessAuthFlow {
    let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/start"
    let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
    return try decode(HarnessAuthFlow.self, from: data)
}

public func authFlow(hostID: String, harness: String, flowID: String) async throws -> HarnessAuthFlow {
    let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/flows/\(encodePathComponent(flowID))"
    let data = try await request(path: path, method: "GET", body: nil)
    return try decode(HarnessAuthFlow.self, from: data)
}

public func cancelAuthFlow(hostID: String, harness: String, flowID: String) async throws -> HarnessAuthFlow {
    let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/flows/\(encodePathComponent(flowID))/cancel"
    let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
    return try decode(HarnessAuthFlow.self, from: data)
}
```

- [ ] **Step 6: Run Swift tests**

Run: `cd apps/drover/NexusKit && swift test --filter ModelsTests`

Expected: pass.

Run: `cd apps/drover/NexusKit && swift test --filter ClientTests`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add apps/drover/NexusKit/Sources/NexusKit/Models.swift \
  apps/drover/NexusKit/Sources/NexusKit/NexusClient.swift \
  apps/drover/NexusKitTests/ModelsTests.swift \
  apps/drover/NexusKitTests/ClientTests.swift
git commit -m "feat(ios): add harness auth client models"
```

---

### Task 6: Swift Auth Flow Model And Auth Sheet

**Files:**
- Create: `apps/drover/NexusKit/Sources/NexusKit/AuthFlowModel.swift`
- Create: `apps/drover/NexusKitTests/AuthFlowModelTests.swift`
- Create: `apps/drover/Drover/Screens/Auth/HarnessAuthSheet.swift`
- Modify: `apps/drover/Drover/Screens/Launch/LaunchView.swift`

**Interfaces:**
- Consumes: `NexusClient` auth methods and `HarnessAuthFlow`.
- Produces: `AuthFlowModel`, `HarnessAuthSheet`.

- [ ] **Step 1: Write failing flow model tests**

Create `AuthFlowModelTests.swift`:

```swift
import Foundation
import Testing
@testable import NexusKit

@Suite(.serialized)
struct AuthFlowModelTests {

@Test @MainActor func startLoadsWaitingFlow() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
        return (202, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","login_url":"https://example.test","user_code":"ABCD-EFGH"}"#.utf8))
    }
    let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")

    await model.start()

    #expect(model.flow?.flowID == "auth-flow-1")
    #expect(model.flow?.state == .waitingForUser)
    #expect(model.errorMessage == nil)
}

@Test @MainActor func cancelUpdatesTerminalFlow() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
    }
    let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
    model.flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))

    await model.cancel()

    #expect(model.flow?.state == .cancelled)
}

}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd apps/drover/NexusKit && swift test --filter AuthFlowModelTests`

Expected: missing `AuthFlowModel`.

- [ ] **Step 3: Implement `AuthFlowModel`**

Create:

```swift
import Foundation
import Observation

@MainActor
@Observable
public final class AuthFlowModel {
    private let client: NexusClient
    public let hostID: String
    public let harness: String
    private var pollTask: Task<Void, Never>?

    public var status: HarnessAuthStatus?
    public var flow: HarnessAuthFlow?
    public var isStarting = false
    public var errorMessage: String?

    public init(client: NexusClient, hostID: String, harness: String) {
        self.client = client
        self.hostID = hostID
        self.harness = harness
    }

    deinit {
        pollTask?.cancel()
    }

    public func refreshStatus() async {
        do {
            status = try await client.authStatus(hostID: hostID, harness: harness)
            errorMessage = nil
        } catch {
            errorMessage = Self.errorMessage(for: error)
        }
    }

    public func start() async {
        isStarting = true
        defer { isStarting = false }
        do {
            flow = try await client.startAuthFlow(hostID: hostID, harness: harness)
            errorMessage = nil
            startPolling()
        } catch {
            errorMessage = Self.errorMessage(for: error)
        }
    }

    public func cancel() async {
        guard let flow else { return }
        do {
            self.flow = try await client.cancelAuthFlow(
                hostID: hostID, harness: harness, flowID: flow.flowID)
            stopPolling()
        } catch {
            errorMessage = Self.errorMessage(for: error)
        }
    }

    public func startPolling(every seconds: Double = 1.5) {
        stopPolling()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, let flow = await self.flow, !flow.isTerminal else { return }
                do {
                    let fresh = try await self.client.authFlow(
                        hostID: self.hostID, harness: self.harness, flowID: flow.flowID)
                    await MainActor.run {
                        self.flow = fresh
                        self.errorMessage = nil
                    }
                    if fresh.isTerminal { return }
                } catch {
                    await MainActor.run { self.errorMessage = Self.errorMessage(for: error) }
                    return
                }
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    public func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    private static func errorMessage(for error: Error) -> String {
        switch error {
        case NexusError.badRequest(let message), NexusError.conflict(let message):
            return message
        case NexusError.unauthorized:
            return "token rejected - check Settings"
        default:
            return "\(error)"
        }
    }
}
```

- [ ] **Step 4: Run model tests**

Run: `cd apps/drover/NexusKit && swift test --filter AuthFlowModelTests`

Expected: pass.

- [ ] **Step 5: Add SwiftUI auth sheet**

Create `HarnessAuthSheet.swift`:

```swift
import SwiftUI
import NexusKit

struct HarnessAuthSheet: View {
    @State private var model: AuthFlowModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    init(client: NexusClient, hostID: String, harness: String) {
        _model = State(initialValue: AuthFlowModel(client: client, hostID: hostID, harness: harness))
    }

    var body: some View {
        Form {
            Section("Harness") {
                LabeledContent("Host", value: model.hostID)
                LabeledContent("Harness", value: model.harness)
                LabeledContent("Status", value: model.flow?.state.rawValue ?? model.status?.state.rawValue ?? "unknown")
            }

            if let flow = model.flow {
                Section("Sign In") {
                    if let url = flow.loginURL {
                        Button {
                            openURL(url)
                        } label: {
                            Label("Open Browser", systemImage: "safari")
                        }
                    }
                    if let code = flow.userCode ?? flow.deviceCode {
                        LabeledContent("Code", value: code)
                    }
                    if let message = flow.message {
                        Text(message)
                    }
                    if let error = flow.lastError {
                        Text(error).foregroundStyle(.red)
                    }
                }
            }

            if let error = model.errorMessage {
                Section {
                    Text(error).foregroundStyle(.red)
                }
            }

            Section {
                Button {
                    Task { await model.start() }
                } label: {
                    if model.isStarting {
                        ProgressView()
                    } else {
                        Label("Sign In", systemImage: "person.badge.key")
                    }
                }
                .disabled(model.isStarting)

                if model.flow?.isTerminal == false {
                    Button(role: .destructive) {
                        Task { await model.cancel() }
                    } label: {
                        Label("Cancel", systemImage: "xmark.circle")
                    }
                }
            }
        }
        .navigationTitle("Harness Auth")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Close") { dismiss() }
            }
        }
        .task { await model.refreshStatus() }
    }
}
```

- [ ] **Step 6: Add launch sheet entry point**

In `LaunchView`, add state:

```swift
@State private var showAuth = false
```

In the `Section("Harness")`, add an auth button below the picker:

```swift
Button {
    showAuth = true
} label: {
    Label("Sign in to \(model.harness)", systemImage: "person.badge.key")
}
.disabled(model.hostID.isEmpty || model.harness == "shell")
```

Add sheet:

```swift
.sheet(isPresented: $showAuth) {
    NavigationStack {
        HarnessAuthSheet(client: client, hostID: model.hostID, harness: model.harness)
    }
}
```

To make this compile, store `client` as a private property in `LaunchView`:

```swift
private let client: NexusClient
```

and set it in `init`.

- [ ] **Step 7: Run Swift package tests**

Run: `cd apps/drover/NexusKit && swift test`

Expected: all package tests pass.

- [ ] **Step 8: Build iOS project**

Run: `cd apps/drover && xcodegen generate && xcodebuild -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 16' build`

Expected: build succeeds. If the simulator name is unavailable, run
`xcrun simctl list devices available` and use an installed iPhone simulator.

- [ ] **Step 9: Commit**

```bash
git add apps/drover/NexusKit/Sources/NexusKit/AuthFlowModel.swift \
  apps/drover/NexusKitTests/AuthFlowModelTests.swift \
  apps/drover/Drover/Screens/Auth/HarnessAuthSheet.swift \
  apps/drover/Drover/Screens/Launch/LaunchView.swift
git commit -m "feat(ios): add harness auth sheet"
```

---

### Task 7: End-To-End Verification And Polish

**Files:**
- Modify only files needed to fix issues found by verification.
- Test: all affected Python and Swift tests.

**Interfaces:**
- Consumes all prior tasks.
- Produces verified feature and final cleanup.

- [ ] **Step 1: Run Python verification**

Run:

```bash
uv run pytest tests/test_harness_auth.py tests/test_harness_daemon.py tests/test_harness_auth_proxy.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full Python test suite**

Run:

```bash
uv run pytest tests/
```

Expected: all pass.

- [ ] **Step 3: Run Swift tests**

Run:

```bash
cd apps/drover/NexusKit && swift test
```

Expected: all pass.

- [ ] **Step 4: Run app build**

Run:

```bash
cd apps/drover && xcodegen generate && xcodebuild -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 16' build
```

Expected: build succeeds. If the named simulator does not exist, use an
available simulator and record the exact destination in the final note.

- [ ] **Step 5: Manual local auth smoke without logging out real accounts**

Use a fake adapter route in tests or a temporary harnessd with `StaticAuthAdapter`
instead of running real provider logout/login. Verify:

```bash
curl -s -H "Authorization: Bearer $DROVER_API_TOKEN" \
  http://127.0.0.1:7080/harness/hosts/mac-mini/auth/codex/status
```

Expected shape:

```json
{"host_id":"mac-mini","harness":"codex","state":"authenticated","label":null,"detail":"..."}
```

Do not log out Claude, Codex, or Gemini during verification unless the user
explicitly asks for a real provider login drill.

- [ ] **Step 6: Check worktree and commits**

Run:

```bash
git status --short --branch
git log --oneline -8
```

Expected: only intentional task commits are ahead. Pre-existing unrelated files
remain unstaged unless explicitly included by the user.

- [ ] **Step 7: Final commit if verification required small fixes**

If Step 1-4 required fixes:

```bash
git add <only files changed for the fix>
git commit -m "fix(harness): polish auth flow verification"
```

If no fixes were required, skip this commit.

---

## Plan Self-Review Notes

- Spec coverage: server auth authority, central proxy, app-only-central access,
  no secret collection, provider adapters, redaction, cancel/timeout, and
  testing are all covered by Tasks 1-7.
- Scope: Gemini remains best-effort as required; deep OAuth/API-key storage is
  excluded.
- Type consistency: Python states and Swift `HarnessAuthState` cases use the
  same wire strings.
- Dirty-worktree constraint: every commit command stages only files from that
  task.
