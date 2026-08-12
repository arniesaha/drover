"""The join-mode reachability probe: code-gated, non-burning, SSRF-bounded.

A joining machine has no credential at the moment it needs this answer, so the
route is gated by an unburned host code instead of a bearer token. That makes
it the second unauthenticated route in the API, and the only one that causes
the server to make an outbound request, so its address bounds matter.
"""

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
    relay_manager = None


@pytest.fixture()
def server(tmp_path):
    store = CredentialStore(tmp_path / CREDENTIALS_FILENAME)
    codes = PairingCodes()
    auth = AuthSettings(enabled=True, api_token="cluster-token", credentials=store)
    httpd = start_metrics_server(
        host="127.0.0.1", port=0, collector=_Collector(), auth=auth, pairing=codes
    )
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", codes
    finally:
        httpd.shutdown()


def _post(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, {}


def test_probe_requires_a_valid_host_code(server):
    base, _ = server
    status, _ = _post(
        base, "/harness/probe", {"code": "XXXX-XXXX", "url": "http://127.0.0.1:1"}
    )
    assert status == 410


def test_probe_does_not_burn_the_code(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    _post(
        base, "/harness/probe", {"code": entry.formatted, "url": "http://127.0.0.1:1"}
    )
    assert codes.redeem(entry.code, source="1.2.3.4").scope == "host"


def test_unreachable_url_reports_false(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    # Port 1 on loopback: private, so not refused, and reliably closed.
    status, body = _post(
        base, "/harness/probe", {"code": entry.formatted, "url": "http://127.0.0.1:1"}
    )
    assert status == 200
    assert body["reachable"] is False


def test_reachable_url_reports_true(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    status, body = _post(base, "/harness/probe", {"code": entry.formatted, "url": base})
    assert status == 200
    assert body["reachable"] is True


def test_a_404_still_counts_as_reachable(server):
    """It answered. The status says nothing about whether a route exists."""
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    status, body = _post(
        base,
        "/harness/probe",
        {"code": entry.formatted, "url": base + "/definitely-not-a-route"},
    )
    assert status == 200
    assert body["reachable"] is True


def test_public_urls_are_refused(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    status, _ = _post(
        base, "/harness/probe", {"code": entry.formatted, "url": "http://example.com"}
    )
    assert status == 400


def test_tailscale_cgnat_is_not_treated_as_public(server):
    """ipaddress.is_private is False for 100.64/10, which would break every
    tailnet join if the guard relied on it alone."""
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    # Unroutable here, so it answers reachable=False rather than 400. The
    # point is that it is not rejected as public.
    status, body = _post(
        base,
        "/harness/probe",
        {"code": entry.formatted, "url": "http://100.64.0.10:7081"},
    )
    assert status == 200
    assert body["reachable"] is False


def test_non_http_schemes_are_refused(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    for url in ("file:///etc/passwd", "ftp://127.0.0.1", "https://127.0.0.1"):
        status, _ = _post(base, "/harness/probe", {"code": entry.formatted, "url": url})
        assert status == 400, url


def test_a_device_code_cannot_drive_the_probe(server):
    base, codes = server
    entry = codes.mint(scope="device", label="Phone")
    status, _ = _post(
        base, "/harness/probe", {"code": entry.formatted, "url": "http://127.0.0.1:1"}
    )
    assert status == 400


def test_malformed_body_is_rejected(server):
    base, codes = server
    entry = codes.mint(scope="host", label="build-mac")
    status, _ = _post(base, "/harness/probe", {"code": entry.formatted})
    assert status == 400
