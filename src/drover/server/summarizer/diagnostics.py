"""Summarizer backend/auth diagnostics that never call an LLM.

The goal is to make launchd/runtime config problems visible without
validating a credential against Anthropic.  Reports only presence/metadata;
never include raw API keys or OAuth tokens.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from drover.server.summarizer.backends.config import (
    DEFAULT_API_MODEL,
    DEFAULT_CLAUDE_CREDENTIALS_PATH,
    DEFAULT_LOCAL_MODEL,
)


def _mask(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def inspect_claude_credentials(path: Optional[str] = None) -> dict[str, Any]:
    """Inspect Claude Code OAuth credentials metadata without exposing tokens."""
    resolved = (
        path
        or (
            os.environ.get("DROVER_CLAUDE_CREDENTIALS_PATH")
            or os.environ.get("NEXUS_CLAUDE_CREDENTIALS_PATH")
        )
        or DEFAULT_CLAUDE_CREDENTIALS_PATH
    )
    p = Path(resolved).expanduser()
    report: dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "readable": False,
        "token_present": False,
        "expires_at_ms": None,
        "expired": None,
        "error": None,
    }
    try:
        raw = p.read_text()
        report["readable"] = True
        data = json.loads(raw)
    except FileNotFoundError:
        return report
    except OSError as exc:
        report["error"] = f"unreadable: {exc.__class__.__name__}"
        return report
    except json.JSONDecodeError as exc:
        report["error"] = f"invalid_json: {exc.msg}"
        return report

    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    report["token_present"] = bool(token)
    expires_at_ms = oauth.get("expiresAt")
    report["expires_at_ms"] = (
        expires_at_ms if isinstance(expires_at_ms, (int, float)) else None
    )
    if isinstance(expires_at_ms, (int, float)) and expires_at_ms > 0:
        report["expired"] = expires_at_ms / 1000.0 <= time.time()
    elif token:
        report["expired"] = False
    return report


def summarize_backend_auth(
    *,
    backend_policy: str = "hybrid",
    api_model: Optional[str] = None,
    local_model: Optional[str] = None,
    local_ollama_url: Optional[str] = None,
    gpu_relay_url: Optional[str] = None,
    gpu_ollama_url: Optional[str] = None,
    base_url: Optional[str] = None,
) -> dict[str, Any]:
    """Return a safe no-network report for summarizer auth/backend config."""
    if backend_policy not in ("hybrid", "cloud", "local"):
        backend_policy = "invalid"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    env_oauth = os.environ.get("ANTHROPIC_OAUTH_TOKEN")
    credentials = inspect_claude_credentials()
    local_ollama = local_ollama_url or (
        os.environ.get("DROVER_LOCAL_OLLAMA_URL")
        or os.environ.get("NEXUS_LOCAL_OLLAMA_URL")
    )
    relay = gpu_relay_url or (
        os.environ.get("DROVER_GPU_RELAY_URL") or os.environ.get("NEXUS_GPU_RELAY_URL")
    )
    ollama = gpu_ollama_url or (
        os.environ.get("DROVER_GPU_OLLAMA_URL")
        or os.environ.get("NEXUS_GPU_OLLAMA_URL")
    )
    resolved_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")

    effective_auth = None
    if api_key:
        effective_auth = "ANTHROPIC_API_KEY"
    elif credentials["token_present"] and credentials["expired"] is not True:
        effective_auth = "claude_credentials"
    elif env_oauth:
        effective_auth = "ANTHROPIC_OAUTH_TOKEN"

    warnings: list[str] = []
    if credentials["exists"] and not credentials["token_present"]:
        warnings.append("Claude credentials file exists but has no access token")
    if credentials["expired"] is True:
        warnings.append("Claude credentials OAuth token appears expired")
    cloud_allowed = backend_policy in ("hybrid", "cloud")
    local_allowed = backend_policy in ("hybrid", "local")

    if cloud_allowed and not effective_auth:
        warnings.append(
            "no Anthropic credentials available (API key, OAuth env token, or valid Claude credentials file)"
        )
    if backend_policy == "cloud" and not effective_auth:
        warnings.append(
            "backend_policy=cloud will leave summarize jobs queued until Anthropic auth is fixed"
        )
    if backend_policy == "local" and effective_auth:
        warnings.append("backend_policy=local ignores available Anthropic credentials")
    if backend_policy == "local" and not (local_ollama or (relay and ollama)):
        warnings.append(
            "backend_policy=local has no local Ollama/GPU backend configured"
        )
    if (
        backend_policy == "hybrid"
        and not effective_auth
        and (local_ollama or (relay and ollama))
    ):
        warnings.append(
            "hybrid policy will fall back to local Ollama because Anthropic auth is unavailable"
        )
    if backend_policy == "invalid":
        warnings.append(
            "invalid summarizer backend_policy; expected hybrid, cloud, or local"
        )
    if bool(relay) ^ bool(ollama):
        warnings.append(
            "GPU wake backend is partially configured; both relay and ollama URLs are required"
        )

    return {
        "backend_policy": backend_policy,
        "api_model": api_model or DEFAULT_API_MODEL,
        "local_model": local_model or DEFAULT_LOCAL_MODEL,
        "base_url_present": bool(resolved_base_url),
        "anthropic_ready": bool(cloud_allowed and effective_auth),
        "local_ready": bool(local_allowed and (local_ollama or (relay and ollama))),
        "anthropic_configured": bool(effective_auth),
        "local_configured": bool(local_ollama or (relay and ollama)),
        "effective_auth": effective_auth,
        "auth_sources": {
            "ANTHROPIC_API_KEY": {"present": bool(api_key), "masked": _mask(api_key)},
            "ANTHROPIC_OAUTH_TOKEN": {
                "present": bool(env_oauth),
                "masked": _mask(env_oauth),
            },
            "claude_credentials": credentials,
        },
        "local_ollama": {"url_present": bool(local_ollama), "wake_relay": False},
        "gpu": {"relay_url_present": bool(relay), "ollama_url_present": bool(ollama)},
        "warnings": warnings,
    }
