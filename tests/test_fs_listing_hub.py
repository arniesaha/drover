"""Exercise listing through authenticated hub HTTP and the real daemon."""

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from test_fs_completion import TOKEN, base_url
from test_fs_listing import root
from test_metrics import _collector_for_proxy

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore


@contextmanager
def hub(tmp_path, base_url, *, enabled=True):
    collector = _collector_for_proxy(tmp_path)
    collector.api_token = TOKEN
    collector._harness_host = lambda host_id, **kwargs: SimpleNamespace(
        host_id=host_id,
        local_url=base_url,
        tailscale_url=None,
        connection_kind="direct",
    )
    store = CredentialStore(tmp_path / "credentials.json")
    server = start_metrics_server(
        host="localhost",
        port=0,
        collector=collector,
        auth=AuthSettings(
            enabled=enabled, api_token="operator-token", credentials=store
        ),
    )
    try:
        yield f"http://localhost:{server.server_address[1]}", store
    finally:
        server.shutdown()
        server.server_close()


def get(url, token="operator-token"):
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"} if token else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def test_hub_routes_listing_with_selected_host_identity(tmp_path, base_url, root):
    with hub(tmp_path, base_url) as (url, store):
        status, body = get(f"{url}/harness/hosts/test-host/fs/list")
        assert status == 200
        assert body["roots"][0]["path"] == str(root)
        # Registry URL pointing at another daemon must fail closed.
        assert get(f"{url}/harness/hosts/other-host/fs/list")[0] == 403
        assert get(f"{url}/harness/hosts/test-host/fs/list", token=None)[0] == 401


@pytest.mark.parametrize(
    "scope,host_id,expected",
    [
        ("host", "test-host", 200),
        ("host", "other-host", 403),
        ("host", None, 403),
        ("device", None, 200),
        ("profile", None, 401),
        ("preflight", None, 401),
    ],
)
def test_hub_credential_scope_and_binding(
    tmp_path, base_url, root, scope, host_id, expected
):
    with hub(tmp_path, base_url) as (url, store):
        _, token = store.issue(scope=scope, label="test", host_id=host_id)
        assert get(f"{url}/harness/hosts/test-host/fs/list", token)[0] == expected


def test_auth_disabled_hub_refuses_listing(tmp_path, base_url, root):
    with hub(tmp_path, base_url, enabled=False) as (url, store):
        assert get(f"{url}/harness/hosts/test-host/fs/list", token=None)[0] == 401


def test_hub_filter_and_path_are_forwarded_without_host_override(
    tmp_path, base_url, root
):
    (root / "project & tools").mkdir()
    with hub(tmp_path, base_url) as (url, _):
        params = urlencode(
            {"path": str(root), "filter": " & ", "host_id": "other-host"}
        )
        status, body = get(f"{url}/harness/hosts/test-host/fs/list?{params}")
        assert status == 200
        assert [entry["name"] for entry in body["entries"]] == ["project & tools"]
