"""Tests for drover.server.web.auth — token resolution, sessions, request checks."""

from __future__ import annotations

import dataclasses
import time

import pytest

from drover.config import default_config
from drover.server.web import auth as web_auth
from drover.server.web.auth import (
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
    monkeypatch.setenv("DROVER_API_TOKEN", "from-env")
    cfg = default_config()
    settings = load_auth(cfg, token_home=tmp_path)
    assert settings.api_token == "from-env"
    assert not (tmp_path / "api_token").exists()


def test_load_auth_generates_and_persists_token(monkeypatch, tmp_path):
    monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    cfg = default_config()  # auth_api_token == ""
    settings = load_auth(cfg, token_home=tmp_path)
    token_file = tmp_path / "api_token"
    assert token_file.exists()
    assert token_file.read_text().strip() == settings.api_token
    assert len(settings.api_token) >= 32
    assert (token_file.stat().st_mode & 0o777) == 0o600
    # Second load reuses the same token.
    again = load_auth(cfg, token_home=tmp_path)
    assert again.api_token == settings.api_token


def test_load_auth_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    cfg = dataclasses.replace(default_config(), auth_enabled=False)
    settings = load_auth(cfg, token_home=tmp_path)
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
    assert header.startswith("drover_session=")
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header
    assert "Path=/" in header
    assert f"Max-Age={a.session_ttl_seconds}" in header
