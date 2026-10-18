"""Authentication and routing responses exposed by every harnessd build."""

from __future__ import annotations

import pytest

from .test_harnessd_contract import request


@pytest.mark.parametrize(
    ("path", "payload", "method", "token", "status", "error"),
    [
        ("/sessions", None, None, False, 401, "authentication required"),
        (
            "/sessions/no-such-session",
            None,
            None,
            False,
            401,
            "authentication required",
        ),
        (
            "/sessions/no-such-session",
            None,
            None,
            True,
            404,
            "unknown terminal session: no-such-session",
        ),
        (
            "/model-catalog?harness=codex",
            None,
            None,
            False,
            401,
            "authentication required",
        ),
        ("/providers/usage", None, None, False, 401, "authentication required"),
        ("/auth/codex/status", None, None, False, 401, "authentication required"),
        ("/auth/codex/flows/nope", None, None, False, 401, "authentication required"),
        ("/auth/codex/start", {}, "POST", False, 401, "authentication required"),
        (
            "/auth/codex/flows/nope/cancel",
            {},
            "POST",
            False,
            401,
            "authentication required",
        ),
        (
            "/advisory/content-bundle",
            {"target_ids": []},
            "POST",
            False,
            401,
            "authentication required",
        ),
        ("/not-a-route", None, None, True, 404, "not found"),
        ("/model-catalog", None, None, True, 400, "invalid model catalog query"),
        (
            "/model-catalog?harness=unknown",
            None,
            None,
            True,
            404,
            "unknown or disabled harness preset: unknown",
        ),
        ("/auth/unknown/status", None, None, True, 404, "'unknown harness: unknown'"),
        (
            "/auth/codex/flows/nope",
            None,
            None,
            True,
            404,
            "'unknown auth flow: codex/nope'",
        ),
    ],
)
def test_public_http_routing_contract(
    daemon, path, payload, method, token, status, error
):
    observed_status, body = request(
        daemon, path, payload=payload, method=method, token=token
    )
    assert observed_status == status
    assert body == {"error": error}
