"""LLM backend abstraction for the summarizer.

Backends share one interface: take a prompt, return the parsed JSON dict the
prompt template asks for. Summaries are produced either by the Anthropic API
(when a key or OAuth token exists) or by the ``claude-code`` CLI already
installed and authenticated on the host.

Ollama is no longer one of them. A 7B local model could not hold the response
schema — 1,306 "missing required keys" and 473 "not JSON" failures in a single
server log — and each retry reloaded ~5GB on a 16GB host. It survives here only
as ``local_analysis_backend``, the deliberately on-box transport for advisory
content analysis, which is a privacy contract rather than a quality one, and
in the embeddings client, which has no claude-code equivalent.

The shared contract: ``backend.summarize(prompt) -> dict`` with required
keys ``summary_md``, ``next_steps_md``, ``open_questions``. Any backend
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
    """Anthropic-first backend, falling back on runtime auth failures."""

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
                "anthropic auth failed under hybrid policy; falling back to %s: %s",
                self.fallback.name,
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

    - ``harness``: always use the local ``claude-code`` CLI. The default,
      because it needs no API key and reuses auth the box already has.
    - ``hybrid``: prefer the Anthropic API, fall back to ``claude-code`` when
      Anthropic auth is unavailable or rejected at runtime.
    - ``cloud``: require the Anthropic API and never fall back.
    - ``local``: retired. Ollama no longer summarizes; the value is accepted
      and routed to ``harness`` with a warning so an unattended host keeps
      producing summaries instead of dead-lettering every session.

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
    from drover.server.summarizer.backends.harness import ClaudeCodeBackend
    from drover.server.summarizer.client import (
        ACTIVE_BRIEF_OPTIONAL_KEYS,
        ACTIVE_BRIEF_REQUIRED_KEYS,
        BRIEF_OPTIONAL_KEYS,
        BRIEF_REQUIRED_KEYS,
        LIVE_RECAP_REQUIRED_KEYS,
    )

    # Already normalized by SummarizerBackendConfig.__post_init__, so a
    # retired "local" never reaches the branches below.
    policy = config.backend_policy
    api_ready = policy in ("hybrid", "cloud") and config.has_anthropic_creds
    harness_ready = config.has_harness_backend

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

    def _new_harness() -> "ClaudeCodeBackend":
        return ClaudeCodeBackend(
            model=config.harness_model,
            required_keys=required_keys,
            optional_keys=optional_keys,
        )

    if api_ready:
        if policy == "hybrid" and harness_ready:
            return HybridFallbackBackend(_new_anthropic(), _new_harness())
        return _new_anthropic()

    if policy == "cloud":
        raise BackendError(
            "summarizer backend_policy=cloud but no Anthropic credentials are available"
        )

    if harness_ready:
        if policy == "hybrid":
            log.warning(
                "backend selected by fallback: claude-code (anthropic unavailable)"
            )
        return _new_harness()

    if policy == "harness":
        raise BackendError(
            "summarizer backend_policy=harness but the claude-code CLI was not found "
            "on PATH or in ~/.local/share/claude/versions"
        )

    raise BackendError(
        "no backend configured for summarizer backend_policy="
        f"{policy} (need ANTHROPIC_API_KEY/ANTHROPIC_OAUTH_TOKEN/Claude credentials, "
        "or an authenticated claude-code CLI on PATH)"
    )


def local_analysis_backend(config: "SummarizerBackendConfig") -> LLMBackend:
    """Return the on-box Ollama transport for advisory content analysis.

    This is not part of summarizer routing and never sees a summarize job.
    ``advisory_content.backend_policy = "local"`` is a disclosure — the bundle
    stays on this machine — so it must keep resolving to a local model even
    though summaries no longer do. Removing it needs a different decision
    (and a different consent) than replacing a bad summarizer.
    """
    from drover.server.summarizer.backends.ollama import OllamaBackend

    if config.gpu_rig is None:
        raise BackendError(
            "local content analysis requires [summarizer] local_ollama_url or gpu_*_url"
        )
    return OllamaBackend(
        rig=config.gpu_rig,
        model=config.local_model,
        wake_on_first_call=config.wake_on_first_call,
        launchd_label=config.local_ollama_launchd_label,
        launchd_plist=config.local_ollama_launchd_plist,
    )


# Re-export for convenience
from drover.server.summarizer.backends.config import (  # noqa: E402
    SummarizerBackendConfig,
    resolve_summarizer_policy,
)

__all__ = [
    "LLMBackend",
    "BackendError",
    "BackendReadinessError",
    "SummarizerBackendConfig",
    "local_analysis_backend",
    "resolve_summarizer_policy",
    "select_backend",
]
