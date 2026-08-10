"""Bounded, host-local Antigravity (agy) account and capacity probe.

Reads this host's signed-in account and returns a ProviderAccountSnapshot.

``~/.gemini`` is agy's own state directory, not a Gemini CLI leftover: it
holds ``antigravity-cli/`` beside ``oauth_creds.json`` and
``google_accounts.json``, and a signed-in agy writes it directly (verified
on the Mac mini 2026-08-09, with no ``~/.agy``, ``~/.antigravity`` or
``~/.codeium`` present at all).

Capacity itself is not readable yet. ``FetchQuotaStatus`` is in the agy
binary as ``google.cloud.businessaicode.{v1beta,v1main}.PredictionService``
with the REST route ``POST /{$api_version}/{parent=projects/*/locations/*}
:fetchQuotaStatus``, but that route is not served on any reachable public
host: ``cloudcode-pa.googleapis.com`` and ``businessaicode.googleapis.com``
both answer the Google frontend's HTML 404 for every combination of
{v1beta, v1main, v1internal} x {global, us-central1}, while
``v1internal:loadCodeAssist`` on that same host answers a JSON 401 -- so
those 404s are routing, not authentication. The quota call reaches Google
over agy's own gRPC transport (or the language server's
``RetrieveUserQuotaSummary``), neither of which is a plain REST request
Drover can make today. Nothing local caches the answer either: agy's
``cache/`` holds only project, onboarding and conversation state.

So this probe reports the account honestly and says capacity is
unavailable, rather than showing a healthy card with nothing behind it. The
window plumbing stays, so landing a working fetch is a change to
``_fetch_windows`` alone.

``read()`` must never raise. ``harnessd``'s ``do_GET`` has no try wrapper,
so an escaping exception means no HTTP response at all, which would take
every other provider's card down with it (drover#65).
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

# No reachable REST route serves FetchQuotaStatus (see the module docstring).
# The cockpit has no wording for this category, so it renders as the neutral
# "Not reporting on <host>" rather than blaming sign-in or reachability.
_NO_QUOTA_API = "quota_api_unreachable"


class AgyUsageProbe:
    """Report the agy account this host is signed into, and its capacity."""

    def __init__(
        self,
        accounts_path: str | Path | None = None,
        state_dir: str | Path | None = None,
    ):
        base = Path(state_dir) if state_dir is not None else Path.home() / ".gemini"
        self.state_dir = base
        self.accounts_path = (
            Path(accounts_path)
            if accounts_path is not None
            else base / "google_accounts.json"
        )

    def read(self, *, host_id: str = "local") -> ProviderAccountSnapshot:
        observed_at = datetime.now(timezone.utc)
        account_label = self._account_label()
        try:
            windows = self._fetch_windows()
        except Exception:  # noqa: BLE001 -- read() must never raise
            log.debug("agy capacity probe failed", exc_info=True)
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category="probe_failed",
            )
        if not windows:
            return _snapshot(
                host_id=host_id,
                account_label=account_label,
                status="usage_unavailable",
                observed_at=observed_at,
                windows=(),
                plan_label=None,
                error_category=_NO_QUOTA_API,
            )
        return _snapshot(
            host_id=host_id,
            account_label=account_label,
            status="ok",
            observed_at=observed_at,
            windows=windows,
            plan_label=None,
            error_category=None,
        )

    def _fetch_windows(self) -> tuple[ProviderUsageWindow, ...]:
        """Capacity windows for this account, empty while none can be read.

        Deliberately makes no request: every candidate ``fetchQuotaStatus``
        route 404s at the Google frontend, so calling one would only spend a
        refresh cycle to learn what the docstring already records.
        """
        return ()

    def _account_label(self) -> str:
        """Name the account this host is signed into.

        A generic label merges distinct accounts into one card and
        misattributes one account's consumption to the other's machines
        (drover#69), so the signed-in address is read per host. Falls back to
        the generic name rather than failing -- no worse than having no
        label at all.
        """
        try:
            raw = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _ACCOUNT_LABEL
        if not isinstance(raw, Mapping):
            return _ACCOUNT_LABEL
        active = raw.get("active")
        if isinstance(active, str) and active.strip():
            return active.strip()
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
    fingerprint: dict[str, Any] = {
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
                "resets_at": (
                    window.resets_at.isoformat() if window.resets_at else None
                ),
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
