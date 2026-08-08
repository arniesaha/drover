"""Anthropic API backend — wraps the existing call_claude_summary client."""

from __future__ import annotations

import logging
from typing import Any, Optional

from drover.server.summarizer.backends.types import BackendError
from drover.server.summarizer.client import (
    DEFAULT_MODEL,
    NoApiKeyError,
    SummarizerClientError,
    call_claude_summary,
)

log = logging.getLogger("drover.summarizer.backends.anthropic")


class AnthropicBackend:
    """Thin LLMBackend implementation backed by Anthropic Messages API.

    Auth precedence: ``auth_token`` (OAuth Bearer) wins over ``api_key``
    if both are supplied. ``base_url`` is forwarded to the SDK so traffic
    can be routed through a configured compatible proxy.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        auth_token: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 800,
        required_keys: Optional[tuple] = None,
        optional_keys: Optional[tuple] = None,
        _client: Any = None,
    ) -> None:
        if not api_key and not auth_token:
            raise BackendError("AnthropicBackend requires an api_key or auth_token")
        self.api_key = api_key
        self.auth_token = auth_token
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.required_keys = required_keys
        self.optional_keys = optional_keys
        self._client = _client

    def summarize(self, prompt: str) -> dict:
        kwargs: dict = dict(
            api_key=self.api_key,
            auth_token=self.auth_token,
            base_url=self.base_url,
            model=self.model,
            max_tokens=self.max_tokens,
            _client=self._client,
        )
        if self.required_keys is not None:
            kwargs["required_keys"] = self.required_keys
        if self.optional_keys is not None:
            kwargs["optional_keys"] = self.optional_keys
        try:
            return call_claude_summary(prompt, **kwargs)
        except NoApiKeyError as e:
            raise BackendError(f"anthropic: no credentials ({e})") from e
        except SummarizerClientError as e:
            raise BackendError(f"anthropic: {e}") from e
