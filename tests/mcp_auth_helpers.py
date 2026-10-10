"""Explicit bearer context for in-process MCP tests."""

from contextlib import contextmanager

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken


@contextmanager
def bearer_context(token):
    value = (
        AuthenticatedUser(AccessToken(token=token, client_id="test", scopes=[]))
        if token
        else None
    )
    handle = auth_context_var.set(value)
    try:
        yield
    finally:
        auth_context_var.reset(handle)
