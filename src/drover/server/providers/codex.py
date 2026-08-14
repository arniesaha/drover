"""Bounded, host-local Codex account and rate-limit probe."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Mapping, Sequence
from uuid import uuid4

from drover.server.providers.codex_app_server import (
    CodexAppServerError,
    CodexAppServerSession,
    _stop_process,
)
from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)


class _ProbeFailure(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CodexUsageProbe:
    """Read the authenticated Codex account's native ChatGPT rate limits."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        timeout_s: float = 5.0,
    ):
        # No default command: the caller resolves the CLI through the user's
        # login shell. Spawning a bare "codex" would search this process's PATH,
        # which under launchd omits the prefix the CLI is installed in.
        self.command = tuple(command) if command else None
        self.timeout_s = timeout_s

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        if self.command is None:
            return _error_snapshot(host_id, observed_at, "cli_not_found")
        try:
            with CodexAppServerSession(self.command, self.timeout_s) as client:
                account_response = client.request(
                    "account/read", {"refreshToken": False}
                )
                rate_limit_response = client.request("account/rateLimits/read", None)
            return _snapshot_from_responses(
                account_response,
                rate_limit_response,
                host_id=host_id,
                observed_at=observed_at,
            )
        except CodexAppServerError as exc:
            return _error_snapshot(host_id, observed_at, exc.category)
        except _ProbeFailure as exc:
            return _error_snapshot(host_id, observed_at, exc.category)
        except (TypeError, ValueError, OverflowError):
            return _error_snapshot(host_id, observed_at, "protocol_error")


def _snapshot_from_responses(
    account_response: Mapping[str, Any],
    rate_limit_response: Mapping[str, Any],
    *,
    host_id: str,
    observed_at: datetime,
) -> ProviderAccountSnapshot:
    account = account_response.get("account")
    if not isinstance(account, Mapping):
        raise _ProbeFailure("protocol_error")
    account_label = _optional_text(account.get("email")) or "Codex"
    plan_label = _optional_text(account.get("planType"))
    rate_limits = rate_limit_response.get("rateLimits")
    if not isinstance(rate_limits, Mapping):
        raise _ProbeFailure("protocol_error")
    windows = tuple(
        window
        for kind in ("primary", "secondary")
        if (window := _usage_window(kind, rate_limits.get(kind))) is not None
    )
    return _snapshot(
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status="ok",
        observed_at=observed_at,
        windows=windows,
        error_category=None,
    )


def _usage_window(kind: str, value: Any) -> ProviderUsageWindow | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _ProbeFailure("protocol_error")
    used_percent = value.get("usedPercent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        raise _ProbeFailure("protocol_error")
    reset = value.get("resetsAt")
    if reset is not None and (
        isinstance(reset, bool) or not isinstance(reset, (int, float))
    ):
        raise _ProbeFailure("protocol_error")
    duration = value.get("windowDurationMins")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int)
    ):
        raise _ProbeFailure("protocol_error")
    return ProviderUsageWindow(
        kind=kind,
        used_percent=float(used_percent),
        window_minutes=duration,
        resets_at=(
            datetime.fromtimestamp(reset, timezone.utc) if reset is not None else None
        ),
    )


def _error_snapshot(
    host_id: str, observed_at: datetime, error_category: str
) -> ProviderAccountSnapshot:
    # Logged here rather than at each raise site so no path can fail silently.
    # A card that flips to "error" and recovers on the next refresh otherwise
    # leaves nothing behind, and the category is the whole diagnosis: it
    # separates a slow CLI (timeout) from a broken one (process_error) from a
    # changed wire format (protocol_error).
    log.warning("codex usage probe failed on %s: %s", host_id, error_category)
    return _snapshot(
        account_label="Codex",
        plan_label=None,
        host_id=host_id,
        status="error",
        observed_at=observed_at,
        windows=(),
        error_category=error_category,
    )


def _snapshot(
    *,
    account_label: str,
    plan_label: str | None,
    host_id: str,
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    error_category: str | None,
) -> ProviderAccountSnapshot:
    fingerprint = {
        "provider": "openai",
        "account_label": account_label,
        "plan_label": plan_label,
        "host_id": host_id,
        "status": status,
        "windows": [
            {
                "kind": window.kind,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
            }
            for window in windows
        ],
        "source": "codex-app-server",
        "error_category": error_category,
    }
    dedup_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ProviderAccountSnapshot(
        snapshot_id=str(uuid4()),
        dedup_key=dedup_key,
        provider="openai",
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source="codex-app-server",
        error_category=error_category,
    )


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
