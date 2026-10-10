"""Tests for device-owned APNs registration HTTP routes."""

from __future__ import annotations

import json as jsonlib
import urllib.error
import urllib.request

import pytest

from drover.server.push import set_sender
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings, mint_session
from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore


class _Collector:
    """The APNs registration routes never touch the collector."""

    relay_manager = None


class _AvailablePushSender:
    is_available = True


@pytest.fixture()
def server(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    auth = AuthSettings(enabled=True, api_token="cluster-token", credentials=store)
    set_sender(_AvailablePushSender())
    httpd = start_metrics_server(
        host="127.0.0.1", port=0, collector=_Collector(), auth=auth
    )
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, store, auth
    finally:
        httpd.shutdown()
        set_sender(None)


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


def test_registration_fails_closed_without_hub_push_and_does_not_store_token(server):
    _, store, _ = server
    device, device_token = store.issue(scope="device", label="Phone")
    store.set_apns_registration(device.id, token="old-apns", environment="sandbox")
    set_sender(None)

    status, body = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "apns-1", "environment": "sandbox"},
    )

    assert status == 503
    assert body == b'{"error": "hub push is unavailable"}\n'
    stored = store.get(device.id)
    assert (stored.apns_token, stored.apns_environment) == (None, None)


def test_token_apple_rejected_is_refused_until_a_new_one_registers(server):
    _, store, _ = server
    device, device_token = store.issue(scope="device", label="Phone")
    store.set_apns_registration(device.id, token="dead-apns", environment="production")
    # What APNsSender records on a BadDeviceToken / Unregistered rejection.
    assert store.mark_apns_registration_failed(
        device.id, expected_token="dead-apns", reason="BadDeviceToken"
    )

    # The app re-sends the same token on its next launch or foreground: it
    # must get #439's push-unavailable answer and keep local notifications.
    status, body = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "dead-apns", "environment": "production"},
    )
    assert (status, body) == (503, b'{"error": "hub push is unavailable"}\n')
    assert store.get(device.id).apns_token is None
    # Asking again does not wear the rejection out.
    assert (
        request(
            server,
            "PUT",
            "/auth/device/apns",
            token=device_token,
            json={"token": "dead-apns", "environment": "production"},
        )[0]
        == 503
    )

    # A reinstall mints a fresh token, which is a fresh promise.
    assert request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "fresh-apns", "environment": "production"},
    ) == (204, b"")
    stored = store.get(device.id)
    assert (stored.apns_token, stored.apns_failure_reason) == ("fresh-apns", None)


def test_registration_for_an_environment_apple_refused_returns_push_unavailable(
    server,
):
    _, store, _ = server
    device, device_token = store.issue(scope="device", label="TestFlight phone")
    dev, dev_token = store.issue(scope="device", label="Dev phone")

    class _SandboxOnlySender:
        is_available = True

        def environment_available(self, environment):
            # A sandbox-restricted key: production said BadEnvironmentKeyInToken.
            return environment == "sandbox"

    set_sender(_SandboxOnlySender())

    status, body = request(
        server,
        "PUT",
        "/auth/device/apns",
        token=device_token,
        json={"token": "testflight-apns", "environment": "production"},
    )
    assert (status, body) == (503, b'{"error": "hub push is unavailable"}\n')
    assert store.get(device.id).apns_token is None
    assert request(
        server,
        "PUT",
        "/auth/device/apns",
        token=dev_token,
        json={"token": "dev-apns", "environment": "sandbox"},
    ) == (204, b"")
    assert store.get(dev.id).apns_token == "dev-apns"


def test_testflight_install_against_sandbox_only_key_falls_back(server, tmp_path):
    """The 2026-09-30 TestFlight acceptance failure, end to end."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from drover.server.push import APNsConfig, APNsSender, AwaitingTransition

    key_path = tmp_path / "AuthKey_SANDBOX.p8"
    key_path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class _Response:
        status_code = 403
        text = '{"reason":"BadEnvironmentKeyInToken"}'

    class _Apple:
        def post(self, url, content=None, headers=None):
            return _Response()

        def close(self):
            pass

    _, store, _ = server
    device, device_token = store.issue(scope="device", label="TestFlight phone")
    sender = APNsSender(
        APNsConfig(
            enabled=True,
            key_path=key_path,
            key_id="SANDBOXKEY",
            team_id="TEAMID1234",
            bundle_id="com.arnab.drover",
        ),
        store,
        client=_Apple(),
    )
    set_sender(sender)
    try:
        registration = {"token": "testflight-apns", "environment": "production"}
        # Before Apple has said anything, the hub honestly believes it can push.
        assert request(
            server, "PUT", "/auth/device/apns", token=device_token, json=registration
        ) == (204, b"")

        sender._deliver(
            AwaitingTransition(
                session_id="s", harness="codex", cwd="/p", awaiting="input"
            )
        )

        status, body = request(
            server, "PUT", "/auth/device/apns", token=device_token, json=registration
        )
        assert (status, body) == (503, b'{"error": "hub push is unavailable"}\n')
        assert store.get(device.id).apns_token is None
    finally:
        sender.close()


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


def test_deletion_rejects_legacy_cookie_and_host_credentials(server):
    _, store, auth = server
    device, _ = store.issue(scope="device", label="Phone")
    _, host_token = store.issue(scope="host", label="Mac", host_id="mac")
    store.set_apns_registration(device.id, token="apns-1", environment="sandbox")

    assert (
        request(server, "DELETE", "/auth/device/apns", token="cluster-token")[0] == 401
    )
    assert (
        request(
            server,
            "DELETE",
            "/auth/device/apns",
            cookie=f"{auth.cookie_name}={mint_session(auth)}",
        )[0]
        == 401
    )
    assert request(server, "DELETE", "/auth/device/apns", token=host_token)[0] == 403
    assert store.get(device.id).apns_token == "apns-1"


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


def test_self_revocation_is_idempotent_and_clears_only_callers_push(server, caplog):
    _, store, _ = server
    device, token = store.issue(scope="device", label="Phone")
    other, _ = store.issue(scope="device", label="Tablet")
    store.set_apns_registration(device.id, token="secret-apns", environment="sandbox")
    store.set_apns_registration(other.id, token="other-apns", environment="production")
    caplog.set_level("DEBUG")

    # Target selectors have no authority: only the bearer identifies the device.
    assert request(
        server,
        "DELETE",
        f"/auth/device/credential?credential_id={other.id}",
        token=token,
        json={"credential_id": other.id},
    ) == (204, b"")
    revoked = store.get(device.id)
    assert not revoked.is_active
    assert (revoked.apns_token, revoked.apns_environment) == (None, None)
    assert store.find_active(token) is None
    assert request(server, "DELETE", "/auth/device/credential", token=token) == (
        204,
        b"",
    )
    assert store.get(device.id).revoked_at == revoked.revoked_at
    assert store.get(other.id).is_active
    assert store.get(other.id).apns_token == "other-apns"
    assert request(server, "DELETE", "/auth/device/apns", token=token)[0] == 401
    assert (
        request(
            server,
            "PUT",
            "/auth/device/apns",
            token=token,
            json={"token": "new-apns", "environment": "sandbox"},
        )[0]
        == 401
    )
    assert token not in caplog.text
    assert "secret-apns" not in caplog.text


def test_self_revocation_requires_device_bearer_even_without_push(server):
    _, store, auth = server
    device, _ = store.issue(scope="device", label="Phone")
    _, host_token = store.issue(scope="host", label="Mac", host_id="mac")
    _, preflight_token = store.issue(scope="preflight", label="Probe")
    set_sender(None)
    for token in (None, "unknown"):
        assert (
            request(server, "DELETE", "/auth/device/credential", token=token)[0] == 401
        )
    assert (
        request(
            server,
            "DELETE",
            "/auth/device/credential",
            cookie=f"{auth.cookie_name}={mint_session(auth)}",
        )[0]
        == 401
    )
    for token in (host_token, preflight_token, "cluster-token"):
        assert (
            request(server, "DELETE", "/auth/device/credential", token=token)[0] == 403
        )
    assert store.get(device.id).is_active


def test_device_cannot_revoke_another_via_credential_id_route(server):
    _, store, _ = server
    _, token = store.issue(scope="device", label="Phone")
    other, _ = store.issue(scope="device", label="Tablet")
    assert (
        request(server, "DELETE", f"/auth/credentials/{other.id}", token=token)[0]
        == 403
    )
    assert store.get(other.id).is_active


def test_revocation_without_push_survives_store_restart(server):
    _, store, _ = server
    device, token = store.issue(scope="device", label="Phone")
    store.set_apns_registration(device.id, token="apns", environment="sandbox")
    set_sender(None)
    assert request(server, "DELETE", "/auth/device/credential", token=token) == (
        204,
        b"",
    )
    reopened = CredentialStore(store._path)
    assert reopened.find_active(token) is None
    assert reopened.find_for_revocation(token).id == device.id
    assert reopened.get(device.id).apns_token is None


def test_notification_preferences_are_device_owned_and_persisted(server, tmp_path):
    _, store, _ = server
    device, token = store.issue(scope="device", label="Phone")
    other, _ = store.issue(scope="device", label="Tablet")
    assert jsonlib.loads(
        request(server, "GET", "/auth/device/notifications", token=token)[1]
    ) == {"mode": "action"}
    assert request(
        server,
        "PUT",
        "/auth/device/notifications",
        token=token,
        json={"mode": "digest"},
    ) == (204, b"")
    assert jsonlib.loads(
        request(server, "GET", "/auth/device/notifications", token=token)[1]
    ) == {"mode": "digest"}
    assert store.get(other.id).notification_mode == "action"
    reopened = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    assert reopened.get(device.id).notification_mode == "digest"


@pytest.mark.parametrize("mode", ["unknown", None, [], {}])
def test_invalid_notification_preferences_are_rejected(server, mode):
    _, store, _ = server
    device, token = store.issue(scope="device", label="Phone")
    assert (
        request(
            server,
            "PUT",
            "/auth/device/notifications",
            token=token,
            json={"mode": mode},
        )[0]
        == 400
    )
    assert store.get(device.id).notification_mode == "action"


def test_notification_preferences_require_device_bearer(server):
    _, store, _ = server
    host, host_token = store.issue(scope="host", label="Host", host_id="synthetic-host")
    for method in ("GET", "PUT"):
        assert (
            request(
                server,
                method,
                "/auth/device/notifications",
                token="cluster-token",
                json={"mode": "all"},
            )[0]
            == 401
        )
        assert request(
            server,
            method,
            "/auth/device/notifications",
            token=host_token,
            json={"mode": "all"},
        )[0] in {401, 403}
    assert store.get(host.id).notification_mode == "action"
