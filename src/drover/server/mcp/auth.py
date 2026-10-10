"""MCP transport verification and capability checks using HTTP credential policy."""

from __future__ import annotations

from functools import wraps
from inspect import iscoroutinefunction

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from drover.server.web.auth import (
    AuthSettings,
    bearer_credential,
    credential_allows_request,
    request_authorized,
)


class HTTPTokenVerifier:
    """Adapt the existing bearer store to the MCP SDK, without issuing tokens."""

    def __init__(self, auth: AuthSettings):
        self.auth = auth

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self.auth.enabled:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        credential = bearer_credential(self.auth, headers)
        if credential is not None:
            # Unknown scopes fail closed, including at protocol initialization.
            if not (
                credential_allows_request(credential, method="GET", path="/profile")
                or credential_allows_request(credential, method="GET", path="/readyz")
            ):
                return None
            return AccessToken(
                token=token, client_id=credential.id, scopes=[credential.scope]
            )
        if request_authorized(self.auth, headers, method="GET", path="/profile"):
            return AccessToken(token=token, client_id="operator", scopes=[])
        return None


def current_headers(server) -> dict[str, str]:
    # Stateful SDK sessions can retain the initializer's context variable.
    # Message request metadata carries the current transport-verified user.
    context = server.get_context()
    try:
        request_context = context.request_context
    except ValueError:
        # Explicit in-process calls have no protocol request context.
        token = get_access_token()
    else:
        request = request_context.request
        user = request.scope.get("user") if request is not None else None
        token = user.access_token if isinstance(user, AuthenticatedUser) else None
    if token is None:
        raise PermissionError("MCP requires an active bearer credential")
    return {"Authorization": f"Bearer {token.token}"}


def authorize(auth: AuthSettings, *, server, method: str, path: str):
    """Recheck revocation and HTTP capability policy at every tool invocation."""

    def decorator(fn):
        def check():
            if not request_authorized(
                auth, current_headers(server), method=method, path=path
            ):
                raise PermissionError(
                    "MCP credential does not authorize this capability"
                )

        if iscoroutinefunction(fn):

            @wraps(fn)
            async def guarded(*args, **kwargs):
                check()
                return await fn(*args, **kwargs)

        else:

            @wraps(fn)
            def guarded(*args, **kwargs):
                check()
                return fn(*args, **kwargs)

        return guarded

    return decorator
