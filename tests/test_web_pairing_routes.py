"""Tests for the pairing HTTP surface on drover-server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CREDENTIALS_FILENAME, CredentialStore
from drover.server.web.pairing import PairingCodes


class _Collector:
    """The pairing routes never touch the collector; this satisfies the type."""

    relay_manager = None


@pytest.fixture()
def server(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    codes = PairingCodes()
    auth = AuthSettings(enabled=True, api_token="cluster-token", credentials=store)
    httpd = start_metrics_server(
        host="127.0.0.1", port=0, collector=_Collector(), auth=auth, pairing=codes
    )
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, store, codes
    finally:
        httpd.shutdown()


def _call(base, method, path, payload=None, token=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        return error.code, json.loads(raw) if raw.strip() else {}


def test_minting_a_code_requires_auth(server):
    base, _, _ = server
    status, _ = _call(base, "POST", "/auth/pair-codes", {"scope": "device"})
    assert status == 401


def test_mint_then_redeem_issues_a_working_token(server):
    base, store, _ = server
    status, minted = _call(
        base,
        "POST",
        "/auth/pair-codes",
        {"scope": "device", "label": "Phone"},
        token="cluster-token",
    )
    assert status == 201
    assert minted["code"][4] == "-"
    assert minted["expires_in_seconds"] > 0

    status, paired = _call(
        base, "POST", "/auth/pair", {"code": minted["code"], "device_name": "My Phone"}
    )
    assert status == 201
    assert paired["scope"] == "device"
    assert paired["server_id"] == store.server_id
    assert store.find_active(paired["token"]) is not None
    assert store.list_all()[0].label == "My Phone"


def test_redeeming_twice_is_gone(server):
    base, _, _ = server
    _, minted = _call(
        base, "POST", "/auth/pair-codes", {"scope": "device"}, token="cluster-token"
    )
    _call(base, "POST", "/auth/pair", {"code": minted["code"]})
    status, _ = _call(base, "POST", "/auth/pair", {"code": minted["code"]})
    assert status == 410


def test_unknown_code_is_gone_not_unauthorized(server):
    base, _, _ = server
    status, _ = _call(base, "POST", "/auth/pair", {"code": "XXXX-XXXX"})
    assert status == 410


def test_repeated_failures_are_throttled(server):
    base, _, _ = server
    for _ in range(5):
        _call(base, "POST", "/auth/pair", {"code": "XXXX-XXXX"})
    status, _ = _call(base, "POST", "/auth/pair", {"code": "XXXX-XXXX"})
    assert status == 429


def test_host_code_mints_a_host_credential(server):
    base, store, _ = server
    _, minted = _call(
        base,
        "POST",
        "/auth/pair-codes",
        {"scope": "host", "label": "build-mac", "host_id": "build-mac"},
        token="cluster-token",
    )
    status, paired = _call(base, "POST", "/auth/pair", {"code": minted["code"]})
    assert status == 201
    assert paired["scope"] == "host"
    assert store.list_all()[0].host_id == "build-mac"


def test_a_device_code_cannot_be_upgraded_by_the_request_body(server):
    base, store, _ = server
    _, minted = _call(
        base, "POST", "/auth/pair-codes", {"scope": "device"}, token="cluster-token"
    )
    _, paired = _call(
        base, "POST", "/auth/pair", {"code": minted["code"], "scope": "host"}
    )
    assert paired["scope"] == "device"


def test_paired_token_then_works_as_credentials(server):
    base, _, _ = server
    _, minted = _call(
        base, "POST", "/auth/pair-codes", {"scope": "device"}, token="cluster-token"
    )
    _, paired = _call(base, "POST", "/auth/pair", {"code": minted["code"]})
    status, listing = _call(base, "GET", "/auth/credentials", token=paired["token"])
    assert status == 200
    assert len(listing["credentials"]) == 1
    assert "verifier" not in listing["credentials"][0]


def test_revoking_a_credential_stops_its_token(server):
    base, _, _ = server
    _, minted = _call(
        base, "POST", "/auth/pair-codes", {"scope": "device"}, token="cluster-token"
    )
    _, paired = _call(base, "POST", "/auth/pair", {"code": minted["code"]})

    status, _ = _call(
        base,
        "DELETE",
        f"/auth/credentials/{paired['credential_id']}",
        token="cluster-token",
    )
    assert status == 204

    status, _ = _call(base, "GET", "/auth/credentials", token=paired["token"])
    assert status == 401


def test_revoking_an_unknown_credential_is_not_found(server):
    base, _, _ = server
    status, _ = _call(base, "DELETE", "/auth/credentials/nope", token="cluster-token")
    assert status == 404
