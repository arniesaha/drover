"""Bounded, host-local Antigravity (agy) account and capacity probe.

Reads this host's account status and returns a ProviderAccountSnapshot.
Must never raise an exception.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from drover.server.providers.types import ProviderAccountSnapshot, ProviderUsageWindow

log = logging.getLogger(__name__)

_ACCOUNT_LABEL = "Antigravity"
_SOURCE = "agy-usage"


class AgyUsageProbe:
    def __init__(
        self,
        accounts_path: str | Path | None = None,
    ):
        self.accounts_path = (
            Path(accounts_path)
            if accounts_path is not None
            else Path.home() / ".gemini" / "google_accounts.json"
        )

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        account_label = self._account_label()

        try:
            # Probe capacity or return supported status snapshot
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="ok",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category=None,
            )
        except Exception:
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category="probe_failed",
            )

    def _account_label(self) -> str:
        try:
            if self.accounts_path.exists():
                raw = json.loads(self.accounts_path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping) and isinstance(raw.get("active"), str):
                    active = raw["active"].strip()
                    if active:
                        return active
        except Exception:
            pass
        return _ACCOUNT_LABEL


def _snapshot(
    *,
    host_id: str,
    account_label: str,
    status: str,
    observed_at: datetime,
    windows: tuple[ProviderUsageWindow, ...],
    plan_label: str | None,
    error_category: str | None,
) -> ProviderAccountSnapshot:
    fingerprint = {
        "provider": "google",
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
        "source": _SOURCE,
        "error_category": error_category,
    }
    dedup_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ProviderAccountSnapshot(
        snapshot_id=str(uuid4()),
        dedup_key=dedup_key,
        provider="google",
        account_label=account_label,
        plan_label=plan_label,
        host_id=host_id,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        windows=windows,
        source=_SOURCE,
        error_category=error_category,
    )
