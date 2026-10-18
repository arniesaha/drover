"""Public catalog and auth error contracts for a launched daemon."""

from __future__ import annotations

import pytest

from .test_harnessd_contract import request


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?harness=",
        "?harness=codex&harness=agy",
        "?harness=codex&refresh=2",
        "?harness=codex&refresh=0&refresh=1",
        "?harness=codex&executable=/tmp/untrusted",
    ],
)
def test_model_catalog_rejects_malformed_query(daemon, query):
    status, body = request(daemon, f"/model-catalog{query}")
    assert status == 400
    assert isinstance(body["error"], str)


def test_model_catalog_refuses_unknown_harness(daemon):
    status, body = request(daemon, "/model-catalog?harness=unknown")
    assert status == 404
    assert isinstance(body["error"], str)


def test_model_catalog_requires_bearer_token(daemon):
    status, body = request(daemon, "/model-catalog?harness=codex", token=False)
    assert (status, body) == (401, {"error": "authentication required"})


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/auth/unknown/status", None),
        ("/auth/codex/flows/missing", None),
        ("/auth/agy/start", "POST"),
    ],
)
def test_auth_public_error_routes(daemon, path, method):
    status, body = request(daemon, path, payload={} if method else None, method=method)
    assert status in {400, 404, 409}
    assert isinstance(body["error"], str)


def test_auth_routes_require_bearer_token(daemon):
    status, body = request(daemon, "/auth/codex/status", token=False)
    assert (status, body) == (401, {"error": "authentication required"})
