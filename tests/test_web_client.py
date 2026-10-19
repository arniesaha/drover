"""HTTP boundary tests for the unauthenticated P0 web-client shell."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.metrics import MetricsCollector, start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.ui import client_assets

_AUTH = AuthSettings(enabled=True, api_token="web-client-test-token")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.fixture
def hub(tmp_path):
    duckdb_path = Path(tmp_path / "drover.duckdb")
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    collector = MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1", port=0, collector=collector, auth=_AUTH
    )
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_shell_is_public_and_has_the_declared_security_headers(hub):
    with urllib.request.urlopen(f"{hub}/ui", timeout=5) as response:
        body = response.read()
        headers = response.headers

    assert response.status == 200
    assert b'<div id="app"></div>' in body
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob: data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_manifest_assets_are_public_and_cache_immutably(hub):
    asset_name = next(name for name in client_assets() if name.startswith("assets/"))
    with urllib.request.urlopen(f"{hub}/ui/{asset_name}", timeout=5) as response:
        body = response.read()
        headers = response.headers

    assert response.status == 200
    assert body
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "path",
    [
        "/ui/assets/missing.js",
        "/ui/assets/%2e%2e/app.py",
        "/ui/assets/%2e%2e%2fapp.py",
    ],
)
def test_unknown_or_traversal_client_assets_are_not_public(hub, path):
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"{hub}{path}", timeout=5)
    assert raised.value.code == 404


def test_observatory_and_apis_stay_authenticated(hub):
    opener = urllib.request.build_opener(_NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as observatory:
        opener.open(f"{hub}/ui/observatory", timeout=5)
    assert observatory.value.code == 302
    assert observatory.value.headers["Location"] == "/auth/login"

    with pytest.raises(urllib.error.HTTPError) as api:
        urllib.request.urlopen(f"{hub}/harness", timeout=5)
    assert api.value.code == 401

    request = urllib.request.Request(
        f"{hub}/ui/observatory",
        headers={"Authorization": "Bearer web-client-test-token"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        page = response.read().decode("utf-8")
    assert "Drover Observatory" in page
