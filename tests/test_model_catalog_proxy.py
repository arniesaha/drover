from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from drover.schema import bootstrap
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector
from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings

CENTRAL_TOKEN = "central-secret"
HOST_TOKEN = "host-secret"


def _catalog(
    host_id: str = "mac mini",
    harness: str = "codex beta",
    scope_id: str = "scope-a",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "host_id": host_id,
        "harness": harness,
        "account_scope_id": scope_id,
        "harness_version": "1.2.3",
        "discovered_at": "2026-08-14T12:00:00+00:00",
        "stale": False,
        "stale_reason": None,
        "models": [
            {
                "id": "gpt-5",
                "display_name": "GPT-5",
                "description": None,
                "is_default": True,
                "reasoning": None,
            }
        ],
    }


class _CatalogHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str | None]] = []
    response_status = 200
    response_body = json.dumps(_catalog()).encode()

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        body = self.__class__.response_body
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.fixture
def upstream_server():
    _CatalogHandler.requests = []
    _CatalogHandler.response_status = 200
    _CatalogHandler.response_body = json.dumps(_catalog()).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _collector(
    tmp_path,
    *,
    upstream_url: str | None,
    host_id: str = "mac mini",
    harness: str = "codex beta",
    enabled: bool = True,
    connection_kind: str = "direct",
) -> MetricsCollector:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    HarnessRegistry(duckdb_path).register_host(
        host_id=host_id,
        display_name="Mac Mini",
        kind="macos",
        local_url=upstream_url,
        connection_kind=connection_kind,
        capabilities={"harnesses": [{"name": harness, "enabled": enabled}]},
    )
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        api_token=HOST_TOKEN,
    )


def _start_central(collector: MetricsCollector):
    return start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(enabled=True, api_token=CENTRAL_TOKEN),
    )


def _request(server, path: str, *, authenticated: bool = True):
    headers = {"Authorization": f"Bearer {CENTRAL_TOKEN}"} if authenticated else {}
    request = Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", headers=headers
    )
    return urlopen(request, timeout=5)


def test_direct_proxy_forwards_auth_refresh_and_persists_valid_catalog(
    tmp_path, upstream_server
):
    collector = _collector(
        tmp_path,
        upstream_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
    )
    central = _start_central(collector)
    try:
        with _request(
            central,
            "/harness/hosts/mac%20mini/model-catalog" "?harness=codex%20beta&refresh=1",
        ) as response:
            payload = json.loads(response.read())
    finally:
        central.shutdown()
        central.server_close()

    assert response.status == 200
    assert payload == _catalog()
    assert _CatalogHandler.requests == [
        {
            "path": "/model-catalog?harness=codex+beta&refresh=1",
            "authorization": f"Bearer {HOST_TOKEN}",
        }
    ]
    assert (
        HarnessRegistry(collector.duckdb_path).latest_model_catalog(
            "mac mini", "codex beta"
        )
        == _catalog()
    )


def test_proxy_returns_stale_lkg_offline_without_leaking_upstream_body(
    tmp_path, upstream_server
):
    collector = _collector(
        tmp_path,
        upstream_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
    )
    assert collector.proxy_harness_model_catalog("mac mini", "codex beta")[0] == 200
    _CatalogHandler.response_status = 502
    _CatalogHandler.response_body = b'{"error":"secret upstream detail"}'

    status, body = collector.proxy_harness_model_catalog("mac mini", "codex beta")
    payload = json.loads(body)

    assert status == 200
    assert payload == {**_catalog(), "stale": True, "stale_reason": "offline"}
    assert "secret upstream detail" not in body


def test_first_proxy_failure_returns_exact_empty_null_metadata_envelope(tmp_path):
    collector = _collector(tmp_path, upstream_url=None)

    status, body = collector.proxy_harness_model_catalog("mac mini", "codex beta")

    assert status == 200
    assert json.loads(body) == {
        "schema_version": 1,
        "host_id": "mac mini",
        "harness": "codex beta",
        "account_scope_id": None,
        "harness_version": None,
        "discovered_at": None,
        "stale": True,
        "stale_reason": "offline",
        "models": [],
    }


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, '{"error":"token rejected"}', "not_authenticated"),
        (403, '{"error":"forbidden"}', "not_authenticated"),
        (404, '{"error":"unknown route"}', "unsupported"),
        (502, '{"error":"request timed out after 7s"}', "timeout"),
        (502, '{"error":"response exceeds byte limit"}', "protocol_error"),
        (200, "not-json", "protocol_error"),
    ],
)
def test_proxy_maps_failures_to_safe_reasons(
    tmp_path, monkeypatch, status, body, reason
):
    collector = _collector(tmp_path, upstream_url="http://127.0.0.1:1")
    monkeypatch.setattr(
        collector, "_harness_request", lambda *args, **kwargs: (status, body)
    )

    returned_status, returned_body = collector.proxy_harness_model_catalog(
        "mac mini", "codex beta"
    )

    assert returned_status == 200
    assert json.loads(returned_body)["stale_reason"] == reason
    assert body not in returned_body


def test_proxy_rejects_non_string_upstream_body_as_protocol_error(
    tmp_path, monkeypatch
):
    collector = _collector(tmp_path, upstream_url="http://127.0.0.1:1")
    monkeypatch.setattr(
        collector,
        "_harness_request",
        lambda *args, **kwargs: (200, {"schema_version": 1}),
    )

    status, body = collector.proxy_harness_model_catalog("mac mini", "codex beta")

    assert status == 200
    assert json.loads(body)["stale_reason"] == "protocol_error"


def test_malformed_or_oversized_upstream_cannot_replace_lkg(tmp_path, upstream_server):
    collector = _collector(
        tmp_path,
        upstream_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
    )
    collector.proxy_harness_model_catalog("mac mini", "codex beta")
    expected = _catalog()

    _CatalogHandler.response_body = json.dumps(
        {**_catalog(scope_id="scope-b"), "schema_version": 2}
    ).encode()
    _, malformed_body = collector.proxy_harness_model_catalog("mac mini", "codex beta")
    _CatalogHandler.response_body = b"x" * (256 * 1024 + 1)
    _, oversized_body = collector.proxy_harness_model_catalog("mac mini", "codex beta")

    assert json.loads(malformed_body)["stale_reason"] == "protocol_error"
    assert json.loads(oversized_body)["stale_reason"] == "protocol_error"
    assert (
        HarnessRegistry(collector.duckdb_path).latest_model_catalog(
            "mac mini", "codex beta"
        )
        == expected
    )


def test_proxy_rejects_unknown_host_and_disabled_harness(tmp_path):
    collector = _collector(tmp_path, upstream_url="http://127.0.0.1:1", enabled=False)

    unknown_status, _ = collector.proxy_harness_model_catalog("missing", "codex beta")
    disabled_status, _ = collector.proxy_harness_model_catalog("mac mini", "codex beta")

    assert unknown_status == 404
    assert disabled_status == 404


def test_proxy_uses_bounded_relay_contract_and_persists(tmp_path):
    collector = _collector(
        tmp_path,
        upstream_url=None,
        host_id="laptop",
        harness="codex",
        connection_kind="relay",
    )
    catalog = _catalog("laptop", "codex")

    class _Relay:
        calls: list[tuple[object, ...]] = []

        def is_live(self, host_id: str) -> bool:
            return host_id == "laptop"

        def request(
            self,
            host_id,
            method,
            path,
            body,
            timeout_s=15,
            max_response_bytes=None,
        ):
            self.calls.append(
                (host_id, method, path, body, timeout_s, max_response_bytes)
            )
            return 200, json.dumps(catalog)

    relay = _Relay()
    collector.relay_manager = relay

    status, body = collector.proxy_harness_model_catalog(
        "laptop", "codex", refresh=True
    )

    assert status == 200
    assert json.loads(body) == catalog
    assert relay.calls == [
        (
            "laptop",
            "GET",
            "/model-catalog?harness=codex&refresh=1",
            {},
            7.0,
            256 * 1024,
        )
    ]
    assert (
        HarnessRegistry(collector.duckdb_path).latest_model_catalog("laptop", "codex")
        == catalog
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?harness=codex%20beta&harness=codex%20beta",
        "?harness=codex%20beta&refresh=2",
        "?harness=codex%20beta&refresh=0&refresh=1",
        "?harness=codex%20beta&unexpected=1",
        f"?harness={quote('x' * 257)}",
    ],
)
def test_public_route_requires_auth_and_strict_query_cardinality(tmp_path, query):
    collector = _collector(tmp_path, upstream_url="http://127.0.0.1:1")
    central = _start_central(collector)
    path = f"/harness/hosts/mac%20mini/model-catalog{query}"
    try:
        with pytest.raises(HTTPError) as unauthenticated:
            _request(central, path, authenticated=False)
        with pytest.raises(HTTPError) as invalid:
            _request(central, path)
    finally:
        central.shutdown()
        central.server_close()

    assert unauthenticated.value.code == 401
    assert invalid.value.code == 400
