"""Backend configuration glue — what the worker reads to pick a backend."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from drover.server.wol import GpuRig

DEFAULT_API_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_LOCAL_MODEL = "qwen3.5:35b-a3b"
DEFAULT_LOCAL_OLLAMA_LAUNCHD_LABEL = "com.drover.mac-ollama-embeddings"
DEFAULT_LOCAL_OLLAMA_LAUNCHD_PLIST = (
    "~/Library/LaunchAgents/com.drover.mac-ollama-embeddings.plist"
)

DEFAULT_CLAUDE_CREDENTIALS_PATH = "~/.claude/.credentials.json"
SummarizerBackendPolicy = Literal["hybrid", "cloud", "local"]

_log = logging.getLogger("drover.summarizer.backends.config")


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

    Either Anthropic credentials (``api_key`` OR ``auth_token``) or an
    Ollama backend (``gpu_rig``) must be set; ``select_backend`` raises if
    neither path is configured. ``wake_on_first_call`` controls whether the
    Ollama backend should wake a remote GPU rig before the first request;
    Mac-local Ollama keeps it enabled so the backend can kickstart the
    user LaunchAgent on demand before the first request.
    """

    backend_policy: SummarizerBackendPolicy = "hybrid"
    api_key: Optional[str] = None
    auth_token: Optional[str] = None  # Claude.ai Pro/Max OAuth (Bearer)
    base_url: Optional[str] = None  # e.g. AgentWeave proxy
    api_model: str = DEFAULT_API_MODEL
    gpu_rig: Optional[GpuRig] = None
    local_model: str = DEFAULT_LOCAL_MODEL
    wake_on_first_call: bool = True
    local_ollama_launchd_label: Optional[str] = None
    local_ollama_launchd_plist: Optional[str] = None

    @property
    def has_anthropic_creds(self) -> bool:
        return bool(self.api_key or self.effective_auth_token())

    @property
    def has_local_backend(self) -> bool:
        return self.gpu_rig is not None

    @property
    def allows_anthropic(self) -> bool:
        return self.backend_policy in ("hybrid", "cloud")

    @property
    def allows_local_backend(self) -> bool:
        return self.backend_policy in ("hybrid", "local")

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
            or "hybrid"
        )
        if policy not in ("hybrid", "cloud", "local"):
            raise ValueError(
                "summarizer backend_policy must be one of: hybrid, cloud, local"
            )

        return cls(
            backend_policy=policy,  # type: ignore[arg-type]
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY") or None,
            auth_token=resolved_auth_token,
            base_url=base_url or os.environ.get("ANTHROPIC_BASE_URL") or None,
            api_model=api_model or DEFAULT_API_MODEL,
            gpu_rig=rig,
            local_model=local_model or DEFAULT_LOCAL_MODEL,
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
