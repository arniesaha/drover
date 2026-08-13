"""Tests for device-owned APNs registration HTTP routes."""

from __future__ import annotations

import json as jsonlib
import urllib.error
import urllib.request

import pytest

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings, mint_session
from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore


class _Collector:
    """The APNs registration routes never touch the collector."""

    relay_manager = None


@pytest.fixture()
def server(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    auth = AuthSettings(enabled=True, api_token="cluster-token", credentials=store)
    httpd = start_metrics_server(
        host="127.0.0.1", port=0, collector=_Collector(), auth=auth
    )
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, store, auth
    finally:
        httpd.shutdown()


def request(server, method, path, *, token=None, json=None, cookie=None, raw=None):
    base, _, _ = server
    data = raw
    if data is None and json is not None:
        data = jsonlib.dumps(json).encode("utf-8")
    http_request = urllib.request.Request(base + path, data=data, method=method)
    if json is not None:
        http_request.add_header("Content-Type", "application/json")
    if token is not None:
        http_request.add_header("Authorization", f"Bearer {token}")
    if cookie is not None:
        http_request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(http_request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_device_bearer_registers_and_replaces_its_apns_token(server):
    _, store, _ = server
    device, device_token = store.issue(scope="device", label="Phone")
    other, _ = store.issue(scope="device", label="Tablet")

    status, body = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "apns-1", "environment": "sandbox"},
    )
    assert (status, body) == (204, b"")
    stored = store.get(device.id)
    assert (stored.apns_token, stored.apns_environment) == ("apns-1", "sandbox")
    assert store.get(other.id).apns_token is None

    status, body = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "apns-2", "environment": "production"},
    )
    assert (status, body) == (204, b"")
    stored = store.get(device.id)
    assert (stored.apns_token, stored.apns_environment) == ("apns-2", "production")


def test_device_bearer_deletes_its_apns_token_idempotently(server):
    _, store, _ = server
    device, device_token = store.issue(scope="device", label="Phone")
    store.set_apns_registration(device.id, token="apns-1", environment="sandbox")

    assert request(server, "DELETE", "/auth/device/apns", token=device_token) == (
        204,
        b"",
    )
    stored = store.get(device.id)
    assert (stored.apns_token, stored.apns_environment) == (None, None)
    assert request(server, "DELETE", "/auth/device/apns", token=device_token) == (
        204,
        b"",
    )


@pytest.mark.parametrize(
    "payload, raw",
    [
        ({}, None),
        ([], None),
        ({"token": "   ", "environment": "sandbox"}, None),
        ({"token": "apns-1", "environment": "staging"}, None),
        (None, b"{"),
    ],
)
def test_registration_rejects_malformed_or_invalid_fields(server, payload, raw):
    _, store, _ = server
    _, device_token = store.issue(scope="device", label="Phone")

    status, _ = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json=payload,
        raw=raw,
    )
    assert status == 400


def test_registration_rejects_legacy_cookie_host_and_revoked_credentials(server):
    _, store, auth = server
    host, host_token = store.issue(scope="host", label="Mac", host_id="mac")
    revoked, revoked_token = store.issue(scope="device", label="Old Phone")
    store.revoke(revoked.id)
    payload = {"token": "apns-1", "environment": "sandbox"}

    assert request(server, "PUT", "/auth/device/apns", json=payload)[0] == 401
    assert (
        request(
            server,
            "PUT",
            "/auth/device/apns",
            token="cluster-token",
            json=payload,
        )[0]
        == 401
    )
    assert (
        request(
            server,
            "PUT",
            "/auth/device/apns",
            cookie=f"{auth.cookie_name}={mint_session(auth)}",
            json=payload,
        )[0]
        == 401
    )
    assert (
        request(server, "PUT", "/auth/device/apns", token=host_token, json=payload)[0]
        == 403
    )
    assert (
        request(server, "PUT", "/auth/device/apns", token=revoked_token, json=payload)[
            0
        ]
        == 401
    )
    assert store.get(host.id).apns_token is None


def test_device_bearer_cannot_mutate_another_device_registration(server):
    _, store, _ = server
    first, first_token = store.issue(scope="device", label="Phone")
    second, second_token = store.issue(scope="device", label="Tablet")

    assert request(
        server,
        "PUT",
        "/auth/device/apns",
        token=first_token,
        json={"token": "phone-token", "environment": "sandbox"},
    ) == (204, b"")
    assert request(
        server,
        "PUT",
        "/auth/device/apns",
        token=second_token,
        json={"token": "tablet-token", "environment": "production"},
    ) == (204, b"")
    assert store.get(first.id).apns_token == "phone-token"
    assert store.get(second.id).apns_token == "tablet-token"
