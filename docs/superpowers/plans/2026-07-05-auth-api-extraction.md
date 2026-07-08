# Auth + API Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add token/cookie authentication to every nexus-server and harnessd HTTP/WebSocket surface, and extract the client-facing JSON API and UI pages out of the `metrics.py` monolith into a `nexus.server.web` package.

**Architecture:** One shared cluster API token (env `NEXUS_API_TOKEN` > `~/.nexus/config.toml [auth]` > auto-generated `~/.nexus/api_token` file). Machine clients send `Authorization: Bearer <token>`; the browser exchanges the token once at `/auth/login` for an HMAC-signed HttpOnly cookie, which also rides the WebSocket upgrade for free. The central server validates on every route (allowlist: `/healthz`, `/readyz`, `/auth/login`) and forwards the bearer on both proxy hops to harnessd, which validates everything except `/healthz`. The three embedded HTML pages move to real `.html` package-data files; the request handler, WS proxy, and auth logic move to `nexus/server/web/`, leaving `MetricsCollector` (business logic) in `metrics.py`.

**Tech Stack:** Python 3.11 stdlib only (`http.server`, `hmac`, `hashlib`, `secrets`, `http.cookies`, `importlib.resources`) — no new dependencies. pytest for tests.

## Global Constraints

- No new runtime dependencies (server is stdlib `ThreadingHTTPServer`; keep it that way).
- Python `>=3.11` (pyproject `requires-python`).
- Auth is **on by default** once deployed; `[auth] enabled = false` in config.toml is the explicit escape hatch.
- Token comparisons MUST use `hmac.compare_digest` (constant-time).
- Cookie MUST be `HttpOnly; SameSite=Strict; Path=/` (no `Secure` flag — the LAN hop is plain HTTP behind Tailscale; revisit if TLS lands).
- `~/.nexus/api_token` file MUST be created with mode `0600`.
- Backward-compat shims: `nexus.server.metrics.start_metrics_server` must keep importing/working (tests and `__main__.py` use it).
- Never log the token value; log only the *path* it was loaded from.
- Existing behavior of every route (status codes, JSON shapes, cache headers) is unchanged for authenticated callers.
- Do not touch the OTLP gRPC receiver (port 4317) — out of scope this pass.
- Line length 88 (black), py311 target.

## Rollout note (read before deploying)

Once merged and deployed, **every host needs the shared token before harnessd registration and heartbeats will succeed**:
1. Generate once: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Central (Mac Mini): put `NEXUS_API_TOKEN=<token>` in the systemd/launchd environment (systemd template already reads `EnvironmentFile=-~/.nexus/env`), or `[auth] api_token` in `~/.nexus/config.toml`.
3. Each harnessd host (Mac Mini, NAS): same env var, or pass `--host-token <token>`.
4. Prometheus scrapers of `/metrics` need `authorization: credentials: <token>` in their scrape config.
5. Browser users log in once at `http://<host>:7080/auth/login`; cookie lasts 30 days.

---

### Task 0: Commit the in-flight WebSocket-reconnect work

The working tree has pre-existing user changes in `src/nexus/server/metrics.py`, `src/nexus/server/harness/daemon.py`, and `tests/test_metrics.py` (WS auto-reconnect, `submitSuffix()`, `_path_matches` hardening). Tasks 2–3 rewrite the same regions, so this must land first as its own commit.

**Files:**
- Modify: none (commit only)

- [ ] **Step 1: Confirm the tests for the in-flight work pass**

Run: `python3 -m pytest tests/test_metrics.py tests/test_harness_daemon.py -q`
Expected: PASS (all tests green)

- [ ] **Step 2: Commit the pre-existing changes verbatim**

```bash
git add src/nexus/server/metrics.py src/nexus/server/harness/daemon.py tests/test_metrics.py
git commit -m "fix: harden terminal websocket reconnect and native-session path matching"
```

- [ ] **Step 3: Verify clean tree**

Run: `git status --porcelain`
Expected: empty output

---

### Task 1: Auth core module + config fields

**Files:**
- Create: `src/nexus/server/web/__init__.py`
- Create: `src/nexus/server/web/auth.py`
- Modify: `src/nexus/config.py` (add `[auth]` section: fields at :23-65, `_DEFAULTS` at :68-118, `_from_dict` at :128-169)
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Consumes: `nexus.config.NexusConfig` (frozen dataclass), `nexus.config._home_nexus()` pattern.
- Produces (later tasks rely on these exact names):
  - `AuthSettings(enabled: bool, api_token: str, session_ttl_seconds: int = 2_592_000, cookie_name: str = "nexus_session")` — frozen dataclass
  - `load_auth(cfg: NexusConfig, nexus_home: Path | None = None) -> AuthSettings`
  - `mint_session(auth: AuthSettings, now: float | None = None) -> str`
  - `verify_session(value: str, auth: AuthSettings, now: float | None = None) -> bool`
  - `request_authorized(auth: AuthSettings, headers) -> bool` (headers = `email.message.Message`-like, i.e. `self.headers`)
  - `session_cookie_value(auth: AuthSettings) -> str` (full `Set-Cookie` header value)
  - `DISABLED = AuthSettings(enabled=False, api_token="")` module constant
  - New `NexusConfig` fields: `auth_enabled: bool`, `auth_api_token: str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_auth.py`:

```python
"""Tests for nexus.server.web.auth — token resolution, sessions, request checks."""

from __future__ import annotations

import dataclasses
import time

import pytest

from nexus.config import default_config
from nexus.server.web import auth as web_auth
from nexus.server.web.auth import (
    AuthSettings,
    load_auth,
    mint_session,
    request_authorized,
    session_cookie_value,
    verify_session,
)


class _Headers(dict):
    """Minimal stand-in for BaseHTTPRequestHandler.headers (get with default)."""


def _auth(token: str = "test-token", **kw) -> AuthSettings:
    return AuthSettings(enabled=True, api_token=token, **kw)


def test_config_has_auth_defaults():
    cfg = default_config()
    assert cfg.auth_enabled is True
    assert cfg.auth_api_token == ""


def test_load_auth_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_API_TOKEN", "from-env")
    cfg = default_config()
    settings = load_auth(cfg, nexus_home=tmp_path)
    assert settings.api_token == "from-env"
    assert not (tmp_path / "api_token").exists()


def test_load_auth_generates_and_persists_token(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    cfg = default_config()  # auth_api_token == ""
    settings = load_auth(cfg, nexus_home=tmp_path)
    token_file = tmp_path / "api_token"
    assert token_file.exists()
    assert token_file.read_text().strip() == settings.api_token
    assert len(settings.api_token) >= 32
    assert (token_file.stat().st_mode & 0o777) == 0o600
    # Second load reuses the same token.
    again = load_auth(cfg, nexus_home=tmp_path)
    assert again.api_token == settings.api_token


def test_load_auth_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    cfg = dataclasses.replace(default_config(), auth_enabled=False)
    settings = load_auth(cfg, nexus_home=tmp_path)
    assert settings.enabled is False


def test_session_roundtrip():
    a = _auth()
    value = mint_session(a)
    assert verify_session(value, a) is True


def test_session_expired():
    a = _auth(session_ttl_seconds=10)
    value = mint_session(a, now=time.time() - 100)
    assert verify_session(value, a) is False


def test_session_tampered():
    a = _auth()
    value = mint_session(a)
    expires, sig = value.split(".", 1)
    assert verify_session(f"{int(expires) + 999}.{sig}", a) is False
    assert verify_session("garbage", a) is False
    assert verify_session("", a) is False


def test_session_wrong_token():
    value = mint_session(_auth("token-a"))
    assert verify_session(value, _auth("token-b")) is False


def test_request_authorized_bearer():
    a = _auth()
    assert request_authorized(a, _Headers({"Authorization": "Bearer test-token"}))
    assert not request_authorized(a, _Headers({"Authorization": "Bearer wrong"}))
    assert not request_authorized(a, _Headers({}))


def test_request_authorized_cookie():
    a = _auth()
    cookie = f"{a.cookie_name}={mint_session(a)}"
    assert request_authorized(a, _Headers({"Cookie": cookie}))
    assert not request_authorized(a, _Headers({"Cookie": f"{a.cookie_name}=bogus"}))


def test_request_authorized_disabled():
    assert request_authorized(web_auth.DISABLED, _Headers({}))


def test_session_cookie_value_flags():
    a = _auth()
    header = session_cookie_value(a)
    assert header.startswith("nexus_session=")
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header
    assert "Path=/" in header
    assert f"Max-Age={a.session_ttl_seconds}" in header
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_web_auth.py -q`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'nexus.server.web'` (and `auth_enabled` attribute errors once the module exists).

- [ ] **Step 3: Add `[auth]` to config.py**

In `src/nexus/config.py`, add two fields to `NexusConfig` (after `redis_jobs_high_water`, line 65):

```python
    # Central API auth (see nexus.server.web.auth). Token resolution order:
    # NEXUS_API_TOKEN env > this field > auto-generated ~/.nexus/api_token.
    auth_enabled: bool
    auth_api_token: str
```

Add to `_DEFAULTS` (after the `redis_jobs` section, line 117):

```python
    "auth": {
        "enabled": True,
        "api_token": "",
    },
```

Add to `_from_dict` return (after `redis_jobs_high_water=...`, line 168):

```python
        auth_enabled=bool(d["auth"]["enabled"]),
        auth_api_token=d["auth"]["api_token"],
```

- [ ] **Step 4: Create the web package and auth module**

Create `src/nexus/server/web/__init__.py`:

```python
"""HTTP surface for nexus-server: auth, UI pages, and the request handler."""
```

Create `src/nexus/server/web/auth.py`:

```python
"""Authentication for the nexus-server HTTP/WebSocket surface.

One shared cluster token. Machine clients send ``Authorization: Bearer
<token>``; browsers exchange the token at /auth/login for an HMAC-signed
HttpOnly cookie (stateless: value is ``<expiry-epoch>.<hmac>``). Token
resolution order: NEXUS_API_TOKEN env var, then ``[auth] api_token`` in
config.toml, then an auto-generated ``~/.nexus/api_token`` file (0600).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from nexus.config import NexusConfig

_TOKEN_FILENAME = "api_token"


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    api_token: str
    session_ttl_seconds: int = 30 * 86400
    cookie_name: str = "nexus_session"


DISABLED = AuthSettings(enabled=False, api_token="")


def _home_nexus() -> Path:
    return Path(os.path.expanduser("~/.nexus"))


def _load_or_create_token_file(nexus_home: Path) -> str:
    token_file = nexus_home / _TOKEN_FILENAME
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    nexus_home.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    return token


def load_auth(cfg: NexusConfig, nexus_home: Path | None = None) -> AuthSettings:
    if not cfg.auth_enabled:
        return DISABLED
    home = nexus_home if nexus_home is not None else _home_nexus()
    token = (
        os.environ.get("NEXUS_API_TOKEN", "").strip()
        or cfg.auth_api_token.strip()
        or _load_or_create_token_file(home)
    )
    return AuthSettings(enabled=True, api_token=token)


def _cookie_key(api_token: str) -> bytes:
    return hashlib.sha256(f"nexus-session:{api_token}".encode("utf-8")).digest()


def mint_session(auth: AuthSettings, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + auth.session_ttl_seconds)
    payload = str(expires)
    sig = hmac.new(
        _cookie_key(auth.api_token), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session(value: str, auth: AuthSettings, now: float | None = None) -> bool:
    if not auth.api_token or "." not in value:
        return False
    payload, _, sig = value.partition(".")
    if not payload.isdigit():
        return False
    expected = hmac.new(
        _cookie_key(auth.api_token), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return int(payload) > (now if now is not None else time.time())


def token_matches(auth: AuthSettings, candidate: str) -> bool:
    return bool(auth.api_token) and hmac.compare_digest(candidate, auth.api_token)


def request_authorized(auth: AuthSettings, headers) -> bool:
    """Accept either a bearer token or a valid session cookie."""
    if not auth.enabled:
        return True
    authorization = headers.get("Authorization", "") or ""
    if authorization.startswith("Bearer ") and token_matches(
        auth, authorization.removeprefix("Bearer ").strip()
    ):
        return True
    raw_cookie = headers.get("Cookie", "") or ""
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:  # noqa: BLE001 - malformed cookie header
            return False
        morsel = jar.get(auth.cookie_name)
        if morsel is not None and verify_session(morsel.value, auth):
            return True
    return False


def session_cookie_value(auth: AuthSettings) -> str:
    """Full Set-Cookie header value for a freshly minted session."""
    return (
        f"{auth.cookie_name}={mint_session(auth)}; Path=/; HttpOnly; "
        f"SameSite=Strict; Max-Age={auth.session_ttl_seconds}"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_web_auth.py -q`
Expected: PASS. Also run `python3 -m pytest tests/ -q -x -k "config"` to confirm nothing depending on `NexusConfig` broke.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/web/ src/nexus/config.py tests/test_web_auth.py
git commit -m "feat: add shared-token auth core (bearer + signed session cookie)"
```

---

### Task 2: Extract embedded HTML pages to package-data files

Pure move — no behavior change. The three module-level constants in `metrics.py` (`_OBSERVATORY_HTML` ≈ line 49, `_HARNESS_HTML` ≈ 472, `_HARNESS_TERMINAL_HTML` ≈ 1106; re-locate exact lines after Task 0's commit) become `.html` files loaded via `importlib.resources`.

**Files:**
- Create: `src/nexus/server/web/static/observatory.html`
- Create: `src/nexus/server/web/static/harness.html`
- Create: `src/nexus/server/web/static/harness_terminal.html`
- Create: `src/nexus/server/web/ui.py`
- Modify: `src/nexus/server/metrics.py` (delete the three constants; call `load_page`)
- Modify: `pyproject.toml` (`[tool.setuptools.package-data]`)
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Produces: `nexus.server.web.ui.load_page(name: str) -> str` (cached; raises `FileNotFoundError` for unknown names). Page names: `"observatory.html"`, `"harness.html"`, `"harness_terminal.html"` (Task 5 adds `"login.html"`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_ui.py`:

```python
"""Tests for nexus.server.web.ui page loading."""

import pytest

from nexus.server.web.ui import load_page


@pytest.mark.parametrize(
    "name, marker",
    [
        ("observatory.html", "<!doctype html>"),
        ("harness.html", "<!doctype html>"),
        ("harness_terminal.html", "xterm"),
    ],
)
def test_load_page_returns_content(name, marker):
    content = load_page(name)
    assert marker.lower() in content.lower()
    # Cached: same object back on second call.
    assert load_page(name) is content


def test_load_page_unknown_name():
    with pytest.raises(FileNotFoundError):
        load_page("nope.html")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_web_ui.py -q`
Expected: FAIL with `ImportError: cannot import name 'load_page'`.

- [ ] **Step 3: Create ui.py and move the HTML content**

Create `src/nexus/server/web/ui.py`:

```python
"""Serve the embedded UI pages from package data files."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

_ALLOWED = {
    "observatory.html",
    "harness.html",
    "harness_terminal.html",
    "login.html",
}


@lru_cache(maxsize=None)
def load_page(name: str) -> str:
    if name not in _ALLOWED:
        raise FileNotFoundError(f"unknown ui page: {name}")
    return (
        resources.files("nexus.server.web")
        .joinpath("static", name)
        .read_text(encoding="utf-8")
    )
```

(`login.html` is allowed now but only created in Task 5 — `load_page` raises until then, which is fine.)

Move the HTML content mechanically. Each constant in `metrics.py` is a triple-quoted string `_NAME = """..."""`. For each of the three constants, copy everything **between** the triple quotes verbatim into the corresponding new file:
- `_OBSERVATORY_HTML` → `src/nexus/server/web/static/observatory.html`
- `_HARNESS_HTML` → `src/nexus/server/web/static/harness.html`
- `_HARNESS_TERMINAL_HTML` → `src/nexus/server/web/static/harness_terminal.html`

Watch for: (a) a leading newline right after `"""` — drop it so the file starts with `<!doctype html>`; (b) if any constant is an f-string or uses `.format(...)`/`%` interpolation or doubled `{{ }}` braces, STOP and un-escape/inline accordingly (as of the last review they are plain string constants with real `${...}` JS template literals, which need no change).

Then in `metrics.py`: delete the three constants and replace their three usages in `_MetricsHandler.do_GET` (currently lines 3045, 3048, 3061):

```python
from nexus.server.web.ui import load_page
...
        if path in {"/", "/ui"}:
            self._send(200, "text/html; charset=utf-8", load_page("observatory.html"))
            return
        if path == "/ui/harness":
            self._send(200, "text/html; charset=utf-8", load_page("harness.html"))
            return
        # inside the /ui/harness/sessions/ branch:
            self._send(
                200, "text/html; charset=utf-8", load_page("harness_terminal.html")
            )
```

- [ ] **Step 4: Add package data to pyproject.toml**

Change the existing section (pyproject.toml:43):

```toml
[tool.setuptools.package-data]
nexus = ["prompts/*.md", "server/web/static/*.html"]
```

- [ ] **Step 5: Run the full metrics test suite**

Run: `python3 -m pytest tests/test_web_ui.py tests/test_metrics.py -q`
Expected: PASS — the HTTP tests that fetch `/ui*` prove the pages still serve byte-identical content.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/web/ src/nexus/server/metrics.py pyproject.toml tests/test_web_ui.py
git commit -m "refactor: move embedded UI pages to web/static package data"
```

---

### Task 3: Move the request handler and WS proxy to web/app.py

Pure move, no behavior change. `MetricsCollector` and all business logic stay in `metrics.py`.

**Files:**
- Create: `src/nexus/server/web/app.py`
- Modify: `src/nexus/server/metrics.py` (delete `_MetricsHandler` + `start_metrics_server`; add re-export)
- Test: existing `tests/test_metrics.py` (no edits — it must pass unchanged)

**Interfaces:**
- Consumes: `nexus.server.metrics.MetricsCollector`, `nexus.server.web.ui.load_page`, websocket helpers from `nexus.server.harness.websocket` (`accept_key`, `client_handshake`, `client_send_json`, `recv_json`, `recv_frame`, `send_frame`).
- Produces: `nexus.server.web.app.start_metrics_server(*, host: str, port: int, collector: MetricsCollector, auth: AuthSettings | None = None) -> ThreadingHTTPServer` and class `_MetricsHandler` with class attrs `collector: MetricsCollector` and `auth: AuthSettings`. `metrics.py` re-exports `start_metrics_server` so existing imports keep working.

- [ ] **Step 1: Create web/app.py with the moved code**

Move these from `metrics.py` into `src/nexus/server/web/app.py`, unchanged except imports:
- class `_MetricsHandler` (metrics.py:3035) — all methods: `do_GET`, `do_POST`, `_proxy_terminal_websocket`, `_mirror_harness_event_frame`, `_read_json`, `_send`, and any `log_message` override.
- `start_metrics_server` (metrics.py:3376).

Module header for `app.py`:

```python
"""nexus-server HTTP entry point: routing, auth gate, and the WS terminal proxy.

Business logic lives on MetricsCollector in nexus.server.metrics; this module
is the thin HTTP shim around it.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from nexus.server.harness.websocket import (
    accept_key,
    client_handshake,
    client_send_json,
    recv_frame,
    recv_json,
    send_frame,
)
from nexus.server.web.auth import DISABLED, AuthSettings
from nexus.server.web.ui import load_page
```

(Import `MetricsCollector` only under `typing.TYPE_CHECKING`, or use a string annotation, to avoid a circular import — `metrics.py` will import `app.py`'s `start_metrics_server`. If `_mirror_harness_event_frame` references collector helpers, access them via `self.collector`.)

Add `auth` to the handler and factory (gate is enforced in Task 4; here it only plumbs through):

```python
class _MetricsHandler(BaseHTTPRequestHandler):
    collector: "MetricsCollector"
    auth: AuthSettings = DISABLED
    ...


def start_metrics_server(
    *,
    host: str,
    port: int,
    collector: "MetricsCollector",
    auth: AuthSettings | None = None,
) -> ThreadingHTTPServer:
    """Start the Nexus metrics HTTP server in a daemon thread."""
    handler = type(
        "NexusMetricsHandler",
        (_MetricsHandler,),
        {"collector": collector, "auth": auth or DISABLED},
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="nexus-metrics", daemon=True
    )
    thread.start()
    return server
```

- [ ] **Step 2: Slim metrics.py and add the compat re-export**

In `metrics.py`: delete the moved code and the now-unused imports (check each of `BaseHTTPRequestHandler`, `ThreadingHTTPServer`, websocket helpers, `load_page` — keep anything `MetricsCollector` still uses, e.g. `client_handshake` is NOT used by the collector but `urlparse` is). At the bottom of `metrics.py` add:

```python
from nexus.server.web.app import start_metrics_server  # noqa: E402,F401 - compat re-export
```

If Python raises a circular-import error at startup, place the re-export inside a `def start_metrics_server(**kwargs)` wrapper that imports lazily:

```python
def start_metrics_server(**kwargs):  # compat shim; canonical home is web.app
    from nexus.server.web.app import start_metrics_server as _impl

    return _impl(**kwargs)
```

- [ ] **Step 3: Run the full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS with zero test-file edits — this is the proof the move was pure.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/server/web/app.py src/nexus/server/metrics.py
git commit -m "refactor: extract HTTP handler and WS proxy into nexus.server.web.app"
```

---

### Task 4: Enforce auth on the central server

**Files:**
- Modify: `src/nexus/server/web/app.py` (auth gate in `do_GET`/`do_POST`)
- Modify: `tests/test_metrics.py` (pass `auth=` to server starts; send bearer in request helpers; new auth tests)

**Interfaces:**
- Consumes: `request_authorized`, `AuthSettings`, `DISABLED` from Task 1; handler/factory from Task 3.
- Produces: gate behavior later tasks rely on — unauthenticated JSON/API request → `401 {"error": "authentication required"}`; unauthenticated browser page (`/`, `/ui`, `/ui/...`) → `302 Location: /auth/login`; `/healthz`, `/readyz`, `/auth/login` always open. Test fixture names other tasks reuse: `_TEST_TOKEN = "test-token"`, `_TEST_AUTH`, `_AUTH_HEADERS`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_metrics.py` (top-level, near the existing helpers around line 210):

```python
from nexus.server.web.auth import AuthSettings, mint_session

_TEST_TOKEN = "test-token"
_TEST_AUTH = AuthSettings(enabled=True, api_token=_TEST_TOKEN)
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


def _authed_get(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers={**_AUTH_HEADERS, **(headers or {})})
    return urllib.request.urlopen(request, timeout=5)
```

New tests (start a server with `auth=_TEST_AUTH`, mirroring the existing loopback pattern at test_metrics.py:302-323):

```python
def test_auth_rejects_missing_token(tmp_path):
    collector = _make_collector(tmp_path)  # reuse the existing collector fixture helper
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_TEST_AUTH
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/harness", timeout=5)
        assert exc.value.code == 401
        assert "authentication required" in exc.value.read().decode()
    finally:
        server.shutdown()


def test_auth_healthz_open(tmp_path):
    ...  # same setup; GET /healthz with NO headers -> 200 "ok"


def test_auth_accepts_bearer(tmp_path):
    ...  # same setup; _authed_get(f".../harness") -> 200, JSON body


def test_auth_accepts_session_cookie(tmp_path):
    ...  # GET /harness with Cookie: nexus_session=<mint_session(_TEST_AUTH)> -> 200


def test_auth_ui_redirects_to_login(tmp_path):
    ...  # GET /ui with no creds and a no-redirect opener -> 302, Location == "/auth/login"
```

For the redirect test, use a non-following opener:

```python
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(_NoRedirect)
try:
    opener.open(f"http://127.0.0.1:{port}/ui", timeout=5)
except urllib.error.HTTPError as err:
    assert err.code == 302
    assert err.headers["Location"] == "/auth/login"
```

Write each `...` body out fully in the test file — the sketches above define setup, request, and assertion; expand them with the same server-start/teardown boilerplate as `test_auth_rejects_missing_token`.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python3 -m pytest tests/test_metrics.py -q -k auth`
Expected: FAIL — requests succeed (200) where 401/302 is expected, because no gate exists yet.

- [ ] **Step 3: Implement the gate in web/app.py**

```python
_PUBLIC_PATHS = {"/healthz", "/readyz", "/auth/login"}


class _MetricsHandler(BaseHTTPRequestHandler):
    ...

    def _gate(self, path: str) -> bool:
        """Authorize the request or write the refusal response.

        Returns True when handling may proceed.
        """
        if path in _PUBLIC_PATHS or not self.auth.enabled:
            return True
        if request_authorized(self.auth, self.headers):
            return True
        if path in {"/", "/ui"} or path.startswith("/ui/"):
            self.send_response(302)
            self.send_header("Location", "/auth/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(
                401, "application/json", '{"error": "authentication required"}\n'
            )
        return False
```

At the very top of `do_GET` and `do_POST`, right after `path = parsed.path`:

```python
        if not self._gate(path):
            return
```

Note: `/harness/sessions/<id>/terminal` (the WS upgrade) flows through `do_GET`, so the gate covers hop 1 automatically — cookies ride the upgrade request.

- [ ] **Step 4: Update existing HTTP tests to authenticate**

Every existing `start_metrics_server(...)` call in `tests/test_metrics.py` gets `auth=_TEST_AUTH`; the shared `_json_request` helper (test_metrics.py:220-228) adds `_AUTH_HEADERS`; bare `urllib.request.urlopen(...)` GETs switch to `_authed_get(...)`; the WS connect helper `_connect_ws` (test_metrics.py:211-217) must send the header on the upgrade — extend it to inject `Authorization: Bearer test-token` into the handshake request (if it uses `client_handshake`, this lands properly in Task 6 when that helper grows a `headers` param; until then have `_connect_ws` build the raw upgrade request itself or pass `auth=DISABLED` for pre-existing WS tests and cover authed-WS in Task 6).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/web/app.py tests/test_metrics.py
git commit -m "feat: require bearer token or session cookie on all central API routes"
```

---

### Task 5: Browser login flow (/auth/login + cookie)

**Files:**
- Create: `src/nexus/server/web/static/login.html`
- Modify: `src/nexus/server/web/app.py` (GET + POST `/auth/login`)
- Test: `tests/test_metrics.py` (login-flow tests)

**Interfaces:**
- Consumes: `load_page("login.html")`, `token_matches`, `session_cookie_value` from Task 1, `_gate` from Task 4.
- Produces: `GET /auth/login` → 200 HTML form. `POST /auth/login` with form body `token=<value>`: correct → `302 Location: /ui` + `Set-Cookie: nexus_session=...`; wrong → `302 Location: /auth/login?error=1`.

- [ ] **Step 1: Write the failing tests**

```python
def test_login_page_is_public(tmp_path):
    ...  # GET /auth/login with no creds -> 200, body contains "<form"


def test_login_success_sets_cookie_and_cookie_works(tmp_path):
    ...  # POST /auth/login body "token=test-token"
    # (Content-Type: application/x-www-form-urlencoded), no-redirect opener
    # -> 302 to /ui, Set-Cookie header present.
    # Parse cookie value; GET /harness with "Cookie: nexus_session=<value>" -> 200.


def test_login_wrong_token_redirects_with_error(tmp_path):
    ...  # POST token=wrong -> 302 Location /auth/login?error=1, no Set-Cookie
```

Write the bodies fully, reusing the Task 4 server-start boilerplate and `_NoRedirect` opener.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_metrics.py -q -k login`
Expected: FAIL (404 from the login routes).

- [ ] **Step 3: Create login.html**

`src/nexus/server/web/static/login.html` (match the visual style of the existing pages — dark background, system font stack; copy the `:root`/body CSS variables from the top of `harness.html`):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nexus — Sign in</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0d1117; color: #e6edf3; display: grid; place-items: center;
           min-height: 100vh; margin: 0; }
    form { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 2rem; width: min(90vw, 22rem); }
    h1 { font-size: 1.1rem; margin: 0 0 1rem; }
    input { width: 100%; box-sizing: border-box; padding: .6rem; margin: 0 0 1rem;
            background: #0d1117; color: inherit; border: 1px solid #30363d;
            border-radius: 6px; }
    button { width: 100%; padding: .6rem; background: #238636; color: #fff;
             border: 0; border-radius: 6px; font-size: 1rem; cursor: pointer; }
    .error { color: #f85149; font-size: .85rem; margin-bottom: 1rem; display: none; }
  </style>
</head>
<body>
  <form method="post" action="/auth/login">
    <h1>Nexus</h1>
    <p class="error" id="error">Invalid token — check ~/.nexus/api_token on the server.</p>
    <input type="password" name="token" placeholder="API token" autofocus autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
  <script>
    if (new URLSearchParams(location.search).has("error")) {
      document.getElementById("error").style.display = "block";
    }
  </script>
</body>
</html>
```

- [ ] **Step 4: Implement the routes in web/app.py**

In `do_GET`, before the `/`, `/ui` branch:

```python
        if path == "/auth/login":
            self._send(200, "text/html; charset=utf-8", load_page("login.html"))
            return
```

In `do_POST`, before the `/harness/hosts` branch:

```python
        if path == "/auth/login":
            self._handle_login()
            return
```

New handler method:

```python
    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        fields = parse_qs(raw)
        candidate = (fields.get("token") or [""])[0].strip()
        if self.auth.enabled and not token_matches(self.auth, candidate):
            self.send_response(302)
            self.send_header("Location", "/auth/login?error=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/ui")
        if self.auth.enabled:
            self.send_header("Set-Cookie", session_cookie_value(self.auth))
        self.send_header("Content-Length", "0")
        self.end_headers()
```

(Import `token_matches` and `session_cookie_value` from `nexus.server.web.auth`.)

- [ ] **Step 5: Run the suite**

Run: `python3 -m pytest tests/test_metrics.py tests/test_web_ui.py -q`
Expected: PASS (the `login.html` `load_page` test in `_ALLOWED` now resolves too — add a `("login.html", "<form")` case to the `test_load_page_returns_content` parametrize list in `tests/test_web_ui.py`).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/web/static/login.html src/nexus/server/web/app.py tests/test_metrics.py tests/test_web_ui.py
git commit -m "feat: add /auth/login page issuing HttpOnly session cookie"
```

---

### Task 6: Secure the central→harnessd hop (port 7081)

harnessd's `POST /sessions` is arbitrary command execution and currently validates nothing. Same shared token: harnessd validates inbound; central forwards the bearer on proxied REST and WS calls.

**Files:**
- Modify: `src/nexus/server/harness/websocket.py` (`client_handshake` gains `headers` param, :96)
- Modify: `src/nexus/server/harness/daemon.py` (token resolution + inbound gate + WS-attach gate)
- Modify: `src/nexus/server/metrics.py` (`MetricsCollector` gains `api_token`; `_proxy_harness_request` :2928 forwards it)
- Modify: `src/nexus/server/web/app.py` (`_proxy_terminal_websocket` hop-2 handshake forwards it)
- Test: `tests/test_harness_daemon.py`, `tests/test_metrics.py`, `tests/test_harness_websocket.py`

**Interfaces:**
- Consumes: `token_matches` from Task 1; `_TEST_AUTH`/`_AUTH_HEADERS` fixtures from Task 4.
- Produces:
  - `client_handshake(sock, *, host: str, path: str, headers: dict[str, str] | None = None) -> str`
  - `HarnessDaemonState.api_token: str` (resolution: `--host-token` flag > `NEXUS_API_TOKEN` env > `~/.nexus/api_token` file > `""` = auth off, with a startup warning)
  - `MetricsCollector.api_token: str` attribute (default `""`), set by `__main__.py` in Task 7; when non-empty, every proxied request/WS handshake to harnessd carries `Authorization: Bearer <api_token>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_harness_daemon.py` — find the existing daemon-server fixture (it constructs `HarnessDaemonState` and `HarnessHTTPServer`); add:

```python
def test_daemon_rejects_unauthenticated_when_token_set(daemon_server_factory):
    server, port = daemon_server_factory(api_token="host-secret")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=5)
    assert exc.value.code == 401


def test_daemon_accepts_bearer(daemon_server_factory):
    server, port = daemon_server_factory(api_token="host-secret")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/sessions",
        headers={"Authorization": "Bearer host-secret"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_daemon_healthz_open(daemon_server_factory):
    server, port = daemon_server_factory(api_token="host-secret")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
        assert r.status == 200


def test_daemon_terminal_attach_rejects_unauthenticated(daemon_server_factory):
    ...  # raw socket GET /sessions/<id>/terminal with Upgrade headers but no
    # Authorization -> response line contains " 401 "
```

Adapt fixture names to what the file actually uses (there is an existing pattern for standing up the daemon HTTP server; add an `api_token` parameter to it that sets `state.api_token`). Write the `...` body fully: open a raw socket, send the upgrade request the way `client_handshake` does, assert the status line.

`tests/test_metrics.py` — the `_FakeHarnessHandler` (test_metrics.py:125-208) records incoming requests; extend it to also record `self.headers.get("Authorization")`, then:

```python
def test_proxy_forwards_bearer_to_harnessd(tmp_path):
    ...  # collector with api_token="host-secret"; POST /harness/hosts/<id>/sessions
    # via central (authed with _AUTH_HEADERS); assert the fake harnessd captured
    # Authorization == "Bearer host-secret".
```

`tests/test_harness_websocket.py` — add:

```python
def test_client_handshake_sends_extra_headers():
    ...  # socketpair or the file's existing echo-server pattern: call
    # client_handshake(sock, host="x", path="/", headers={"Authorization": "Bearer t"})
    # and assert the raw request bytes contain "Authorization: Bearer t\r\n".
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_harness_daemon.py tests/test_harness_websocket.py -q -k "auth or bearer or handshake"`
Expected: FAIL (TypeError for the new `headers` kwarg; 200s where 401 expected).

- [ ] **Step 3: Extend client_handshake**

In `src/nexus/server/harness/websocket.py` (:96):

```python
def client_handshake(
    sock: socket.socket,
    *,
    host: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> str:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"{extra}"
        "\r\n"
    )
    ...  # rest unchanged
```

- [ ] **Step 4: Gate harnessd inbound**

In `src/nexus/server/harness/daemon.py`:

1. Add `api_token: str = ""` to `HarnessDaemonState` (near `host_token`, :1111).
2. Resolution helper (module level, near `run_harnessd` :1799):

```python
def resolve_daemon_token(host_token: str | None) -> str:
    """--host-token flag > NEXUS_API_TOKEN env > ~/.nexus/api_token file > ''."""
    if host_token:
        return host_token
    env = os.environ.get("NEXUS_API_TOKEN", "").strip()
    if env:
        return env
    token_file = Path(os.path.expanduser("~/.nexus/api_token"))
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""
```

In `run_harnessd`, set `state.api_token = resolve_daemon_token(host_token)` and keep `state.host_token = state.api_token` (so the existing `_post_central_json` bearer, daemon.py:1779, now sends the shared token to central). If the result is `""`, log a prominent warning: `"harnessd running WITHOUT auth: set NEXUS_API_TOKEN or --host-token"`.

3. Gate in `HarnessRequestHandler` (class at :1131):

```python
    def _authorized(self) -> bool:
        token = self.server.state.api_token
        if not token:
            return True  # auth off (warned at startup)
        authorization = self.headers.get("Authorization", "") or ""
        return authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization.removeprefix("Bearer ").strip(), token
        )

    def _gate(self) -> bool:
        if self._authorized():
            return True
        self._write_json(
            {"error": "authentication required"}, status=HTTPStatus.UNAUTHORIZED
        )
        return False
```

(Add `import hmac` to the module imports.) At the top of `do_GET` (:1134), `do_POST` (:1174), and `do_DELETE` (:1189):

```python
        parsed = urlparse(self.path)
        if parsed.path != "/healthz" and not self._gate():
            return
```

`do_OPTIONS` (:1197) stays open (CORS preflight cannot carry credentials). The `/sessions/<id>/terminal` upgrade is inside `do_GET`, so `_attach_terminal` (:1437) is covered by the same gate.

- [ ] **Step 5: Forward the bearer from central**

In `src/nexus/server/metrics.py`:
1. `MetricsCollector.__init__` gains `api_token: str = ""` stored as `self.api_token` (append after existing params; keep it keyword-friendly).
2. `_proxy_harness_request` (:2928): where the outbound `urllib.request.Request` sets `Content-Type` (:2946-2951), add:

```python
        if self.api_token:
            request.add_header("Authorization", f"Bearer {self.api_token}")
```

In `src/nexus/server/web/app.py`, `_proxy_terminal_websocket` hop-2 handshake (the `client_handshake(upstream, host=..., path=path)` call, formerly metrics.py:3260):

```python
            upstream_headers = (
                {"Authorization": f"Bearer {self.collector.api_token}"}
                if self.collector.api_token
                else None
            )
            client_handshake(
                upstream, host=f"{host}:{port}", path=path, headers=upstream_headers
            )
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (daemon tests that don't pass `api_token` keep auth off and are unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/nexus/server/harness/websocket.py src/nexus/server/harness/daemon.py src/nexus/server/metrics.py src/nexus/server/web/app.py tests/test_harness_daemon.py tests/test_harness_websocket.py tests/test_metrics.py
git commit -m "feat: authenticate the central-to-harnessd hop with the shared token"
```

---

### Task 7: Wire-up, config template, and docs

**Files:**
- Modify: `src/nexus/server/__main__.py` (build `AuthSettings`, pass to server + collector; config template; harnessd subcommand)
- Modify: `README.md` (Authentication section)

**Interfaces:**
- Consumes: `load_auth` (Task 1), `start_metrics_server(auth=...)` (Task 3/4), `MetricsCollector.api_token` + `resolve_daemon_token` (Task 6).

- [ ] **Step 1: Wire auth in the run command**

In `src/nexus/server/__main__.py`, where the metrics server starts (~:1190-1217):

```python
from nexus.server.web.auth import load_auth

...
        auth = load_auth(cfg)
        collector.api_token = auth.api_token if auth.enabled else ""
        metrics_server = start_metrics_server(
            host=metrics_host, port=cfg.metrics_http_port, collector=collector, auth=auth
        )
        if auth.enabled:
            click.echo(
                "metrics API auth: enabled "
                "(token from NEXUS_API_TOKEN / config [auth] / ~/.nexus/api_token)"
            )
        else:
            click.echo("metrics API auth: DISABLED via [auth] enabled=false")
```

(Adapt variable names to the surrounding code; if the collector is constructed nearby, pass `api_token=` at construction instead of assigning after.)

- [ ] **Step 2: Update the embedded config template**

In the config template string (`__main__.py:88-178`), after the `[server]` section add:

```toml
[auth]
# Central API auth. Token resolution order: NEXUS_API_TOKEN env var, then
# api_token below, then auto-generated ~/.nexus/api_token (created 0600 on
# first start). Set enabled = false only for local development.
enabled = true
api_token = ""
```

- [ ] **Step 3: Wire the harnessd subcommand**

In `__main__.py`'s harnessd subcommand (:333-371) and `harness/cli.py` (:99-105): update the `--host-token` help text to "Shared Nexus API token (falls back to NEXUS_API_TOKEN, then ~/.nexus/api_token)" — the resolution itself already landed in `run_harnessd` via Task 6's `resolve_daemon_token`.

- [ ] **Step 4: README**

Add an `## Authentication` section to `README.md` documenting: the shared-token model, the three resolution sources, `/auth/login` for browsers, `Authorization: Bearer` for API/scrapers, the harnessd hop, the `[auth] enabled=false` escape hatch, and the rollout note from this plan's header (condensed).

- [ ] **Step 5: Run the full suite + lint**

Run: `python3 -m pytest tests/ -q && python3 -m black --check src/nexus/server/web/ src/nexus/config.py`
Expected: PASS / "would leave unchanged".

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/__main__.py src/nexus/server/harness/cli.py README.md
git commit -m "feat: wire shared-token auth into server startup, config template, docs"
```

---

### Task 8: End-to-end verification (manual, local only)

Read-only against production — do NOT restart the live Mac Mini services here; that is the rollout step for the user.

**Files:** none (verification only)

- [ ] **Step 1: Boot an isolated server**

```bash
export NEXUS_TEST_HOME=$(mktemp -d)
NEXUS_API_TOKEN=e2e-secret python3 - <<'EOF'
# Start a collector + server on 127.0.0.1:7099 against a throwaway DuckDB,
# mirroring the pattern in tests/test_metrics.py, then sleep.
EOF
```

(Or simpler: run the curl matrix against a server started by `python3 -m pytest tests/test_metrics.py -q -k auth -s` breakpoints — the curl matrix below assumes a standalone boot on :7099.)

- [ ] **Step 2: Curl matrix**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7099/healthz          # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7099/harness          # 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer e2e-secret" \
     http://127.0.0.1:7099/harness                                              # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7099/ui               # 302
curl -s -D - -o /dev/null -X POST -d "token=e2e-secret" \
     http://127.0.0.1:7099/auth/login | grep -i "set-cookie"                    # nexus_session=...
COOKIE=$(curl -s -D - -o /dev/null -X POST -d "token=e2e-secret" \
     http://127.0.0.1:7099/auth/login | sed -n 's/^[Ss]et-[Cc]ookie: \([^;]*\).*/\1/p')
curl -s -o /dev/null -w "%{http_code}\n" -H "Cookie: $COOKIE" \
     http://127.0.0.1:7099/ui                                                   # 200
```

- [ ] **Step 3: Browser check**

Open `http://127.0.0.1:7099/ui` → expect redirect to the login page; enter `e2e-secret` → Observatory renders; open the Harness console and confirm the session list loads (cookie riding every fetch) — if a live local harnessd with a matching token is running, attach a shell terminal to confirm the WS path end-to-end.

- [ ] **Step 4: Final commit / merge prep**

Run: `python3 -m pytest tests/ -q` one last time, then hand off per the finishing-a-development-branch skill (merge vs PR is the user's call).

---

## Out of scope (deliberately)

- OTLP gRPC auth (port 4317) — separate surface, separate pass.
- Per-host tokens / rotation — the `harness_hosts.token_hash` design can layer on the same gate later.
- TLS / `Secure` cookie flag, CSRF tokens beyond `SameSite=Strict`, rate-limiting login attempts.
- Tightening harnessd's `Access-Control-Allow-Origin: *` CORS headers (browser never calls harnessd directly; revisit with the API-client work).
- Splitting `MetricsCollector` itself or reworking JSON shapes — this pass only relocates the HTTP shim and pages.
