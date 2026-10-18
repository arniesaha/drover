"""Public GET-query contracts that require no daemon implementation imports."""

from __future__ import annotations

import pytest

from .test_harnessd_contract import request


@pytest.mark.parametrize(
    ("path", "status", "error"),
    [
        ("/model-catalog", 400, "invalid model catalog query"),
        ("/model-catalog?harness=", 400, "invalid model catalog query"),
        (
            "/model-catalog?harness=codex&harness=agy",
            400,
            "invalid model catalog query",
        ),
        ("/model-catalog?harness=codex&refresh=2", 400, "invalid model catalog query"),
        (
            "/model-catalog?harness=codex&refresh=0&refresh=1",
            400,
            "invalid model catalog query",
        ),
        (
            "/model-catalog?harness=codex&executable=/tmp/untrusted",
            400,
            "invalid model catalog query",
        ),
        (
            "/model-catalog?harness=unknown",
            404,
            "unknown or disabled harness preset: unknown",
        ),
        (
            "/model-catalog?harness=missing",
            404,
            "unknown or disabled harness preset: missing",
        ),
        ("/sessions?archived_cursor=forged", 400, "invalid archived_cursor"),
        ("/sessions/nope", 404, "unknown terminal session: nope"),
        ("/sessions/nope/turns", 404, "unknown structured session: nope"),
        ("/sessions/nope/interrupt", 404, "unknown structured session: nope"),
        ("/sessions/nope/permission", 404, "unknown structured session: nope"),
        ("/auth/unknown/status", 404, "'unknown harness: unknown'"),
        ("/auth/codex/flows/nope", 404, "'unknown auth flow: codex/nope'"),
        ("/not-a-route", 404, "not found"),
    ],
)
def test_public_query_error_contract(daemon, path, status, error):
    method = (
        "POST"
        if path.rsplit("/", 1)[-1] in {"turns", "interrupt", "permission"}
        else None
    )
    observed_status, body = request(
        daemon, path, payload={} if method else None, method=method
    )
    assert (observed_status, body) == (status, {"error": error})
