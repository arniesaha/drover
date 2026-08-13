"""Tests for drover.server.web.auth — token resolution, sessions, request checks."""

from __future__ import annotations

import dataclasses
import time

import pytest

from drover.config import default_config
from drover.server.web import auth as web_auth
from drover.server.web.auth import (
    AuthSettings,
    bearer_credential,
    load_auth,
    mint_session,
    request_authorized,
    session_cookie_value,
    verify_session,
)
from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore


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


def test_config_has_legacy_token_and_advertised_url_defaults():
    cfg = default_config()
    assert cfg.auth_legacy_token_enabled is True
    assert cfg.server_advertised_url == ""


def test_load_auth_attaches_a_credential_store(monkeypatch, tmp_path):
    monkeypatch.delenv("DROVER_API_TOKEN", raising=False)
    settings = load_auth(default_config(), token_home=tmp_path)
    assert settings.credentials is not None
    assert (tmp_path / "credentials.json").exists()


def test_device_token_authorizes_like_the_cluster_token(tmp_path):
    from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore

    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    _, token = store.issue(scope="device", label="Phone")
    settings = _auth(credentials=store)

    assert request_authorized(settings, _Headers({"Authorization": f"Bearer {token}"}))


def test_revoked_device_token_is_refused(tmp_path):
    from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore

    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    credential, token = store.issue(scope="device", label="Phone")
    store.revoke(credential.id)
    settings = _auth(credentials=store)

    assert not request_authorized(
        settings, _Headers({"Authorization": f"Bearer {token}"})
    )


def test_legacy_token_can_be_switched_off(tmp_path):
    from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore

    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    _, token = store.issue(scope="device", label="Phone")
    settings = _auth(credentials=store, legacy_token_enabled=False)

    assert not request_authorized(
        settings, _Headers({"Authorization": "Bearer test-token"})
    )
    assert request_authorized(settings, _Headers({"Authorization": f"Bearer {token}"}))


def test_using_a_device_token_records_last_used(tmp_path):
    from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore

    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    _, token = store.issue(scope="device", label="Phone")
    settings = _auth(credentials=store)

    request_authorized(settings, _Headers({"Authorization": f"Bearer {token}"}))
    assert store.list_all()[0].last_used_at is not None


def test_bearer_credential_returns_active_credential(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    device, device_token = store.issue(scope="device", label="Phone")
    host, host_token = store.issue(scope="host", label="build-mac", host_id="build-mac")
    settings = _auth(credentials=store)

    device_result = bearer_credential(
        settings, _Headers({"Authorization": f"Bearer {device_token}"})
    )
    host_result = bearer_credential(
        settings, _Headers({"Authorization": f"Bearer {host_token}"})
    )

    assert device_result is not None
    assert device_result.id == device.id
    assert device_result.scope == "device"
    assert host_result is not None
    assert host_result.id == host.id
    assert host_result.scope == "host"
    assert store.get(device.id).last_used_at is not None


def test_bearer_credential_rejects_legacy_cookie_and_revoked(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    credential, token = store.issue(scope="device", label="Phone")
    settings = _auth(credentials=store)

    assert (
        bearer_credential(settings, _Headers({"Authorization": "Bearer test-token"}))
        is None
    )
    assert (
        bearer_credential(
            settings,
            _Headers({"Cookie": f"{settings.cookie_name}={mint_session(settings)}"}),
        )
        is None
    )
    assert store.revoke(credential.id)
    assert (
        bearer_credential(settings, _Headers({"Authorization": f"Bearer {token}"}))
        is None
    )
