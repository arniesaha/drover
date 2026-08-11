"""LLM backend abstraction for the summarizer.

Two backends share one interface: take a prompt, return the parsed JSON
dict the prompt template asks for. The hybrid router prefers Anthropic
whenever credentials are available, and falls back to Ollama only when
Anthropic is unavailable.

The shared contract: ``backend.summarize(prompt) -> dict`` with required
keys ``summary_md``, ``next_steps_md``, ``open_questions``. Either backend
may raise ``BackendError`` with a human-readable cause; the worker turns
that into a ``last_error`` row.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

from drover.server.summarizer.backends.types import BackendError, BackendReadinessError

log = logging.getLogger("drover.summarizer.backends")


@runtime_checkable
class LLMBackend(Protocol):
    """Anything that can turn a prompt into a parsed summary dict."""

    name: str
    model: str

    def summarize(self, prompt: str) -> dict: ...


def _is_anthropic_auth_error(exc: BackendError) -> bool:
    """Return True when Anthropic failed because credentials are unusable.

    Hybrid routing should fall back to local Ollama for runtime auth failures
    such as expired OAuth tokens. It should not hide schema/prompt/model errors.
    """
    text = str(exc).lower()
    return "anthropic" in text and any(
        marker in text
        for marker in (
            "401",
            "unauthorized",
            "invalid authentication",
            "invalid x-api-key",
            "authentication credentials",
            "no credentials",
            "no api key",
            "no_api_key",
        )
    )


class HybridFallbackBackend:
    """Anthropic-first backend with local fallback for runtime auth failures."""

    name = "hybrid"

    def __init__(self, primary: LLMBackend, fallback: LLMBackend) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model

    def summarize(self, prompt: str) -> dict:
        try:
            out = self.primary.summarize(prompt)
            self.model = self.primary.model
            return out
        except BackendError as exc:
            if not _is_anthropic_auth_error(exc):
                raise
            log.warning(
                "anthropic auth failed under hybrid policy; falling back to local summarizer: %s",
                exc,
            )
            out = self.fallback.summarize(prompt)
            self.model = self.fallback.model
            return out


def select_backend(
    *,
    job_kind: str,
    config: "SummarizerBackendConfig",
) -> LLMBackend:
    """Return the right backend for the given job kind.

    ``backend_policy`` controls routing:

    - ``hybrid``: prefer Anthropic, fall back to Ollama when Anthropic auth is
      unavailable.
    - ``cloud``: require Anthropic and never silently fall back to Ollama.
    - ``local``: require Ollama and never call Anthropic.

    Brief job kinds still configure the backend with their specific response
    schemas.

    ``project_brief`` configures the backend with the brief-prompt schema
    (``brief_md``, ``recent_themes_md``, ``next_steps_md``) so validation
    matches what the brief prompt actually returns.

    ``active_brief`` configures the backend with the active-brief schema
    (``brief_md``, ``last_user_req``, ``current_objective``, ``suggested_next``)
    used for rolling handoff briefs on open sessions.
    """
    from drover.server.summarizer.backends.anthropic import AnthropicBackend
    from drover.server.summarizer.backends.ollama import OllamaBackend
    from drover.server.summarizer.client import (
        ACTIVE_BRIEF_OPTIONAL_KEYS,
        ACTIVE_BRIEF_REQUIRED_KEYS,
        BRIEF_OPTIONAL_KEYS,
        BRIEF_REQUIRED_KEYS,
        LIVE_RECAP_REQUIRED_KEYS,
    )

    api_ready = config.allows_anthropic and config.has_anthropic_creds
    local_ready = config.allows_local_backend and config.has_local_backend

    if job_kind == "project_brief":
        required_keys = BRIEF_REQUIRED_KEYS
        optional_keys = BRIEF_OPTIONAL_KEYS
    elif job_kind == "active_brief":
        required_keys = ACTIVE_BRIEF_REQUIRED_KEYS
        optional_keys = ACTIVE_BRIEF_OPTIONAL_KEYS
    elif job_kind == "live_recap":
        required_keys = LIVE_RECAP_REQUIRED_KEYS
        optional_keys = ()
    else:
        required_keys = None
        optional_keys = None

    def _new_anthropic() -> "AnthropicBackend":
        return AnthropicBackend(
            api_key=config.api_key,
            auth_token=config.effective_auth_token(),
            base_url=config.base_url,
            model=config.api_model,
            required_keys=required_keys,
            optional_keys=optional_keys,
        )

    def _new_ollama() -> "OllamaBackend":
        return OllamaBackend(
            rig=config.gpu_rig,
            model=config.local_model,
            wake_on_first_call=config.wake_on_first_call,
            launchd_label=config.local_ollama_launchd_label,
            launchd_plist=config.local_ollama_launchd_plist,
            required_keys=required_keys,
            optional_keys=optional_keys,
        )

    if api_ready:
        if config.backend_policy == "hybrid" and local_ready:
            return HybridFallbackBackend(_new_anthropic(), _new_ollama())
        return _new_anthropic()

    if config.backend_policy == "cloud":
        raise BackendError(
            "summarizer backend_policy=cloud but no Anthropic credentials are available"
        )

    if config.backend_policy == "local" and not local_ready:
        raise BackendError(
            "summarizer backend_policy=local but no [summarizer] local_ollama_url or gpu_*_url is configured"
        )

    # Hybrid fallback path: use Ollama only when Anthropic is unavailable.
    if local_ready:
        log.warning("backend selected by fallback: ollama (anthropic unavailable)")
        return _new_ollama()

    raise BackendError(
        "no backend configured for summarizer backend_policy="
        f"{config.backend_policy} (need ANTHROPIC_API_KEY/ANTHROPIC_OAUTH_TOKEN/Claude credentials, "
        "[summarizer] local_ollama_url, or [summarizer] gpu_*_url)"
    )


# Re-export for convenience
from drover.server.summarizer.backends.config import (
    SummarizerBackendConfig,
)  # noqa: E402

__all__ = [
    "LLMBackend",
    "BackendError",
    "BackendReadinessError",
    "SummarizerBackendConfig",
    "select_backend",
]
