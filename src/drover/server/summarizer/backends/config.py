"""Backend configuration glue — what the worker reads to pick a backend."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from drover.server.summarizer.backends.harness import DEFAULT_HARNESS_MODEL
from drover.server.wol import GpuRig

DEFAULT_API_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_LOCAL_MODEL = "qwen3.5:35b-a3b"
DEFAULT_LOCAL_OLLAMA_LAUNCHD_LABEL = "com.drover.mac-ollama-embeddings"
DEFAULT_LOCAL_OLLAMA_LAUNCHD_PLIST = (
    "~/Library/LaunchAgents/com.drover.mac-ollama-embeddings.plist"
)

DEFAULT_CLAUDE_CREDENTIALS_PATH = "~/.claude/.credentials.json"
DEFAULT_HARNESS_POLICY = "harness"
SummarizerBackendPolicy = Literal["harness", "hybrid", "cloud", "local"]
SUMMARIZER_BACKEND_POLICIES = ("harness", "hybrid", "cloud", "local")

_log = logging.getLogger("drover.summarizer.backends.config")


def resolve_summarizer_policy(policy: str) -> str:
    """Map a configured policy onto one the router still implements.

    ``local`` is retired: the Ollama summarizer it named is gone. Hosts that
    still carry it in ``~/.drover/config.toml`` fall through to ``harness``
    rather than failing every job, because the alternative on an unattended
    box is 5 wasted attempts and a dead letter per session. It is a warning,
    not a silent rename: unlike Ollama, claude-code sends the transcript to
    Anthropic under the machine's existing Claude Code login.
    """
    if policy == "local":
        _log.warning(
            "summarizer backend_policy=local is retired (the local Ollama "
            "summarizer was removed); using the claude-code harness instead. "
            'Set backend_policy = "harness" in ~/.drover/config.toml.'
        )
        return DEFAULT_HARNESS_POLICY
    return policy


def _read_oauth_token_from_credentials_file(
    path: Optional[str] = None,
) -> Optional[str]:
    """Return a fresh OAuth access token from Claude Code's credentials file.

    Claude Code maintains ``~/.claude/.credentials.json`` and refreshes the
    access token in place. Re-reading on demand lets a long-running server
    pick up rotations without restart. Returns ``None`` if the file is
    missing, unparseable, has no token, or the token has expired.
    """
    resolved = (
        path
        or (
            os.environ.get("DROVER_CLAUDE_CREDENTIALS_PATH")
            or os.environ.get("NEXUS_CLAUDE_CREDENTIALS_PATH")
        )
        or DEFAULT_CLAUDE_CREDENTIALS_PATH
    )
    p = Path(resolved).expanduser()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None
    expires_at_ms = oauth.get("expiresAt")
    if isinstance(expires_at_ms, (int, float)) and expires_at_ms > 0:
        if expires_at_ms / 1000.0 <= time.time():
            return None
    return token


@dataclass(frozen=True)
class SummarizerBackendConfig:
    """All knobs the backend layer needs in one struct.

    Summaries need either Anthropic credentials (``api_key`` OR
    ``auth_token``) or an installed ``claude-code`` CLI; ``select_backend``
    raises if neither exists.

    ``gpu_rig`` no longer feeds the summarizer. It stays because two other
    consumers read it from here: the embeddings worker (which has no
    claude-code equivalent) and advisory local content analysis.
    ``wake_on_first_call`` still controls whether those wake a remote GPU rig
    before the first request; Mac-local Ollama keeps it enabled so the
    LaunchAgent can be kickstarted on demand.
    """

    backend_policy: SummarizerBackendPolicy = DEFAULT_HARNESS_POLICY
    api_key: Optional[str] = None
    auth_token: Optional[str] = None  # Claude.ai Pro/Max OAuth (Bearer)
    base_url: Optional[str] = None  # e.g. AgentWeave proxy
    api_model: str = DEFAULT_API_MODEL
    gpu_rig: Optional[GpuRig] = None
    local_model: str = DEFAULT_LOCAL_MODEL
    harness_model: str = DEFAULT_HARNESS_MODEL
    wake_on_first_call: bool = True
    local_ollama_launchd_label: Optional[str] = None
    local_ollama_launchd_plist: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalize once, at construction, so the router and every
        # availability check read a policy that is actually implemented —
        # and so a retired value warns once per config, not once per poll.
        if self.backend_policy not in SUMMARIZER_BACKEND_POLICIES:
            raise ValueError(
                "summarizer backend_policy must be one of: "
                + ", ".join(SUMMARIZER_BACKEND_POLICIES)
            )
        resolved = resolve_summarizer_policy(self.backend_policy)
        if resolved != self.backend_policy:
            object.__setattr__(self, "backend_policy", resolved)

    @property
    def has_anthropic_creds(self) -> bool:
        return bool(self.api_key or self.effective_auth_token())

    @property
    def has_local_backend(self) -> bool:
        """Whether an Ollama host is configured (embeddings/advisory, not summaries)."""
        return self.gpu_rig is not None

    @property
    def has_harness_backend(self) -> bool:
        from drover.server.harness.structured.claude import resolve_binary

        return resolve_binary() is not None

    @property
    def allows_anthropic(self) -> bool:
        return self.backend_policy in ("hybrid", "cloud")

    @property
    def allows_harness_backend(self) -> bool:
        return self.backend_policy in ("harness", "hybrid")

    def effective_auth_token(self) -> Optional[str]:
        """Return the freshest OAuth token, re-reading the credentials file.

        Precedence: credentials.json (live, refreshed by Claude Code) >
        ``ANTHROPIC_OAUTH_TOKEN`` env > token captured at construction.
        Skipped entirely if ``api_key`` is set (API-key path bypasses OAuth).
        """
        if self.api_key:
            return None
        file_token = _read_oauth_token_from_credentials_file()
        if file_token:
            return file_token
        env_token = os.environ.get("ANTHROPIC_OAUTH_TOKEN")
        if env_token:
            return env_token
        return self.auth_token

    @classmethod
    def from_runtime(
        cls,
        *,
        api_key: Optional[str] = None,
        auth_token: Optional[str] = None,
        base_url: Optional[str] = None,
        backend_policy: Optional[str] = None,
        api_model: Optional[str] = None,
        local_model: Optional[str] = None,
        harness_model: Optional[str] = None,
        local_ollama_url: Optional[str] = None,
        gpu_relay_url: Optional[str] = None,
        gpu_ollama_url: Optional[str] = None,
        wake_timeout_s: float = 120.0,
        local_ollama_launchd_label: Optional[str] = None,
        local_ollama_launchd_plist: Optional[str] = None,
    ) -> "SummarizerBackendConfig":
        """Build a config from explicit runtime args, applying env fallbacks.

        Precedence: explicit arg > env var > module default.
        """
        rig: Optional[GpuRig] = None
        wake_on_first_call = True
        local_ollama = local_ollama_url or (
            os.environ.get("DROVER_LOCAL_OLLAMA_URL")
            or os.environ.get("NEXUS_LOCAL_OLLAMA_URL")
        )
        relay = gpu_relay_url or (
            os.environ.get("DROVER_GPU_RELAY_URL")
            or os.environ.get("NEXUS_GPU_RELAY_URL")
        )
        ollama = gpu_ollama_url or (
            os.environ.get("DROVER_GPU_OLLAMA_URL")
            or os.environ.get("NEXUS_GPU_OLLAMA_URL")
        )
        if local_ollama:
            rig = GpuRig(
                relay_url="", ollama_url=local_ollama, wake_timeout_s=wake_timeout_s
            )
            wake_on_first_call = True
        elif relay and ollama:
            rig = GpuRig(
                relay_url=relay, ollama_url=ollama, wake_timeout_s=wake_timeout_s
            )

        resolved_auth_token = (
            auth_token
            or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
            or _read_oauth_token_from_credentials_file()
            or None
        )

        policy = (
            backend_policy
            or (
                os.environ.get("DROVER_SUMMARIZER_BACKEND_POLICY")
                or os.environ.get("NEXUS_SUMMARIZER_BACKEND_POLICY")
            )
            or DEFAULT_HARNESS_POLICY
        )

        return cls(
            backend_policy=policy,  # type: ignore[arg-type]
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY") or None,
            auth_token=resolved_auth_token,
            base_url=base_url or os.environ.get("ANTHROPIC_BASE_URL") or None,
            api_model=api_model or DEFAULT_API_MODEL,
            gpu_rig=rig,
            local_model=local_model or DEFAULT_LOCAL_MODEL,
            harness_model=harness_model
            or os.environ.get("DROVER_SUMMARIZER_HARNESS_MODEL")
            or DEFAULT_HARNESS_MODEL,
            wake_on_first_call=wake_on_first_call,
            local_ollama_launchd_label=local_ollama_launchd_label
            or (
                os.environ.get("DROVER_LOCAL_OLLAMA_LAUNCHD_LABEL")
                or os.environ.get("NEXUS_LOCAL_OLLAMA_LAUNCHD_LABEL")
            )
            or (DEFAULT_LOCAL_OLLAMA_LAUNCHD_LABEL if local_ollama else None),
            local_ollama_launchd_plist=local_ollama_launchd_plist
            or (
                os.environ.get("DROVER_LOCAL_OLLAMA_LAUNCHD_PLIST")
                or os.environ.get("NEXUS_LOCAL_OLLAMA_LAUNCHD_PLIST")
            )
            or (DEFAULT_LOCAL_OLLAMA_LAUNCHD_PLIST if local_ollama else None),
        )
