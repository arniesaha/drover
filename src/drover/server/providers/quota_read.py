"""Provider quota projection and routing, independent of MCP/analytics entrypoints.

The MCP facade retains selected-store checks, freshness metadata and read caps.
Queue admission uses the same projection with its relay-aware refresh transport.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from drover.server.providers.quota import is_public_window, is_routing_window


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (
        stamp.replace(tzinfo=timezone.utc)
        if stamp.tzinfo is None
        else stamp.astimezone(timezone.utc)
    )


def _format_routing_reset_time(dt: datetime, now: datetime | None = None) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if abs((dt - now).total_seconds()) < 24 * 3600 and dt.date() == now.date():
        return dt.strftime("%H:%MZ")
    return dt.strftime("%Y-%m-%d %H:%MZ")


def _mask_email(label: str | None) -> str | None:
    """Agents get a recognisable label, never a full mailbox address."""
    if not label or "@" not in label:
        return label
    local, _, domain = label.partition("@")
    return f"{local[:1]}***@{domain}"


def _account_identity_key(snapshot: Any) -> str:
    identity = (getattr(snapshot, "account_identity", None) or "").strip().lower()
    label = (getattr(snapshot, "account_label", None) or "").strip().lower()
    provider = getattr(snapshot, "provider", "")
    if identity:
        return f"{provider}|{identity}"
    if "@" in label:
        return f"{provider}|{label}"
    if label and label != "unknown account":
        return f"{provider}|{label}"
    return f"{provider}|unknown|{getattr(snapshot, 'host_id', '')}"


def _build_routing_hint(accounts: list[dict[str, Any]], now: datetime) -> str:
    """Choose the best usable subscription for each harness, preserving uncertainty."""
    import math

    if not accounts:
        return "no provider accounts configured"

    default_pools = {
        "openai": ("codex",),
        "anthropic": ("claude",),
        "google": ("gemini", "agy-claude"),
    }
    # Each candidate is (state, binding utilization, recovery instant).
    pools: dict[str, list[tuple[str, float | None, datetime | None]]] = {}
    for account in accounts:
        provider = account.get("provider", "")
        defaults = default_pools.get(provider, (provider or "other",))
        windows_by_pool: dict[str, list[dict[str, Any]]] = {}
        unknown_kind = False
        for window in account.get("windows", []):
            kind = window.get("kind")
            if not is_routing_window(provider, kind):
                # Known Anthropic billing allowances do not constrain routing.
                # Unsupported kinds still preserve main's uncertainty handling.
                if provider != "anthropic" or not is_public_window(provider, kind):
                    unknown_kind = True
                continue
            pool = defaults[0]
            if provider == "google" and kind in {
                "five_hour_claude_gpt",
                "seven_day_claude_gpt",
            }:
                pool = "agy-claude"
            windows_by_pool.setdefault(pool, []).append(window)
        # Missing or unsupported windows must never imply an absent account.
        # An unknown Google window could constrain either harness.
        if not windows_by_pool or unknown_kind:
            for pool in defaults:
                windows_by_pool.setdefault(pool, [])

        for pool, windows in windows_by_pool.items():
            state = account.get("status")
            candidate: tuple[str, float | None, datetime | None]
            resets = [_instant(w.get("resets_at")) for w in windows]
            if state == "stale" or any(w.get("status") == "stale" for w in windows):
                candidate = ("stale", None, None)
            elif state not in {"ok", "observed_exhausted"}:
                label = (
                    "configured, usage unknown"
                    if state == "usage_unavailable"
                    else "unavailable"
                )
                candidate = (label, None, None)
            elif any(r is not None and r <= now for r in resets):
                # A reset invalidates the old reading, including observed 429s.
                candidate = ("stale", None, None)
            elif unknown_kind or not windows:
                candidate = ("configured, usage unknown", None, None)
            else:
                blocked = [
                    (w, r)
                    for w, r in zip(windows, resets)
                    if w.get("status") in {"exhausted", "observed_exhausted"}
                    or (
                        isinstance(w.get("used_percent"), (int, float))
                        and w["used_percent"] >= 100
                    )
                ]
                if blocked:
                    # Every binding constraint on this account must recover.
                    recovery = (
                        max(r for _, r in blocked)
                        if all(r is not None for _, r in blocked)
                        else None
                    )
                    label = (
                        "observed exhausted"
                        if any(
                            w.get("status") == "observed_exhausted" for w, _ in blocked
                        )
                        else "out"
                    )
                    candidate = (label, None, recovery)
                elif all(
                    w.get("status") == "ok"
                    and isinstance(w.get("used_percent"), (int, float))
                    and not isinstance(w["used_percent"], bool)
                    and math.isfinite(w["used_percent"])
                    and 0 <= w["used_percent"] < 100
                    for w in windows
                ):
                    candidate = (
                        "usable",
                        max(w["used_percent"] for w in windows),
                        None,
                    )
                else:
                    candidate = ("configured, usage unknown", None, None)
            pools.setdefault(pool, []).append(candidate)

    priority = ["gemini", "codex", "agy-claude", "claude"]
    hints: list[str] = []
    for pool in sorted(
        pools, key=lambda k: (priority.index(k) if k in priority else 99, k)
    ):
        candidates = pools[pool]
        usable = [used for state, used, _ in candidates if state == "usable"]
        if usable:
            hints.append(f"{pool}: {int(round(min(usable)))}%")
        elif all(state in {"out", "observed exhausted"} for state, _, _ in candidates):
            # Alternative subscriptions recover independently: choose the first.
            timed = [c for c in candidates if c[2] is not None]
            state, _, reset = min(timed, key=lambda c: c[2]) if timed else candidates[0]
            until = (
                f" until {_format_routing_reset_time(reset, now=now)}" if reset else ""
            )
            hints.append(f"{pool}: {state}{until}")
        else:
            labels = sorted({state for state, _, _ in candidates})
            hints.append(f"{pool}: {', '.join(labels)}")
    return "; ".join(hints)


def read_provider_quota(
    *,
    duckdb_path: Path,
    provider: str | None = None,
    fresh: bool = False,
    timeout_s: float = 3.0,
    service: Any | None = None,
    fetch: Any | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only provider quota for every account across hosts with routing hint."""
    import concurrent.futures

    from drover.server.harness.registry import HarnessRegistry
    from drover.server.providers.service import ProviderUsageService

    current_now = now or datetime.now(timezone.utc)
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=timezone.utc)

    db_path = Path(duckdb_path)
    usage_service = service or ProviderUsageService(
        duckdb_path=db_path,
        parquet_dir=db_path.parent / "parquet",
        clock=lambda: current_now,
    )

    refresh_errors: list[dict[str, str]] = []
    if fresh:
        try:
            registry = HarnessRegistry(db_path)
            online_hosts = [
                h
                for h in registry.list_hosts()
                if getattr(h, "liveness", None) and h.liveness().state == "online"
            ]
            if online_hosts:
                # Not a `with` block: its exit joins every worker, so a slow
                # host would hold the call past `timeout_s`. Probes still
                # running at the deadline finish in the background and land
                # in the store for the next read.
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(online_hosts))
                )
                try:

                    def refresh(host):
                        if fetch is None:
                            raise RuntimeError("transport_unavailable")
                        return usage_service.refresh_host(
                            host, fetch=fetch, record_failures=False
                        )

                    futures = {
                        executor.submit(refresh, host): host.host_id
                        for host in online_hosts
                    }
                    done, pending = concurrent.futures.wait(futures, timeout=timeout_s)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            category = str(exc)
                            if category not in {
                                "timeout",
                                "unavailable",
                                "protocol_error",
                                "empty_inventory",
                                "provider_error",
                                "transport_unavailable",
                            }:
                                category = "unavailable"
                            refresh_errors.append(
                                {"host_id": futures[future], "error_category": category}
                            )
                    refresh_errors.extend(
                        {"host_id": futures[future], "error_category": "timeout"}
                        for future in pending
                    )
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            refresh_errors.append({"host_id": "", "error_category": "unavailable"})

    try:
        raw_snapshots = usage_service.latest_accounts()
    except (duckdb.Error, OSError) as exc:
        logging.getLogger(__name__).warning("failed to read provider accounts: %s", exc)
        return {
            "accounts": [],
            "routing_hint": "no provider accounts configured",
            "updated_at": None,
            "refresh_errors": sorted(
                refresh_errors, key=lambda error: error["host_id"]
            ),
        }

    if provider:
        provider_norm = provider.strip().lower()
        raw_snapshots = [
            s for s in raw_snapshots if s.provider.lower() == provider_norm
        ]
    # A host with an enabled harness but no provider login is inventory, not
    # capacity. ProviderUsageService normally removes these by disabling the
    # connection, but keep this public aggregation defensive for alternate
    # service implementations too.
    raw_snapshots = [
        snapshot for snapshot in raw_snapshots if snapshot.status != "not_configured"
    ]

    # Group snapshots by subscription identity
    buckets: dict[str, list[Any]] = {}
    for snapshot in raw_snapshots:
        key = _account_identity_key(snapshot)
        buckets.setdefault(key, []).append(snapshot)

    account_records: list[dict[str, Any]] = []
    latest_timestamps: list[datetime] = []

    for key, members in buckets.items():

        def member_sort_key(s: Any):
            status = getattr(s, "status", "")
            if status in {"ok", "observed_exhausted"} and any(
                w.resets_at is not None and w.resets_at <= current_now
                for w in s.windows
            ):
                status = "stale"
            status_priority = {
                "observed_exhausted": 4,
                "ok": 3,
                "usage_unavailable": 2,
                "stale": 1,
                "error": 0,
            }.get(status, 0)
            obs = getattr(s, "observed_at", current_now)
            return (status_priority, obs)

        representative = max(members, key=member_sort_key)
        rep_provider = representative.provider
        rep_plan = representative.plan_label or next(
            (s.plan_label for s in members if s.plan_label), None
        )
        rep_label = (
            representative.account_label
            if representative.account_label != "Unknown account"
            else next(
                (
                    s.account_label
                    for s in members
                    if s.account_label != "Unknown account"
                ),
                representative.account_label,
            )
        )
        hosts = sorted({s.host_id for s in members if s.host_id})
        rep_obs = max(s.observed_at for s in members)
        latest_timestamps.append(rep_obs)

        # Build windows
        windows_list: list[dict[str, Any]] = []
        for w in representative.windows:
            if not is_public_window(rep_provider, w.kind):
                continue
            w_status: str
            if representative.status not in {"ok", "observed_exhausted"}:
                w_status = representative.status
                w_source = representative.source
            elif w.resets_at is not None and w.resets_at <= current_now:
                w_status = "stale"
                w_source = representative.source
            elif (
                representative.status == "observed_exhausted"
                or representative.source == "observed_429"
            ) and (w.used_percent == 100.0 or "claude_gpt" in w.kind):
                w_status = "observed_exhausted"
                w_source = "observed_429"
            elif w.used_percent is not None and w.used_percent >= 100.0:
                w_status = "exhausted"
                w_source = "provider_api"
            elif w.used_percent is not None:
                w_status = "ok"
                w_source = "provider_api"
            else:
                w_status = "usage_unavailable"
                w_source = "provider_api"

            windows_list.append(
                {
                    "kind": w.kind,
                    "window_minutes": w.window_minutes,
                    "used_percent": w.used_percent,
                    "resets_at": w.resets_at.isoformat() if w.resets_at else None,
                    "status": w_status,
                    "source": w_source,
                    "updated_at": representative.observed_at.isoformat(),
                }
            )

        account_records.append(
            {
                "provider": rep_provider,
                "account_label": _mask_email(rep_label),
                "plan": rep_plan,
                "hosts": hosts,
                "status": representative.status,
                "windows": windows_list,
                "updated_at": rep_obs.isoformat(),
            }
        )

    account_records.sort(key=lambda a: (a["provider"], a["account_label"]))
    overall_updated_at = (
        max(latest_timestamps).isoformat() if latest_timestamps else None
    )
    routing_hint = _build_routing_hint(account_records, now=current_now)

    return {
        "accounts": account_records,
        "routing_hint": routing_hint,
        "updated_at": overall_updated_at,
        "refresh_errors": sorted(refresh_errors, key=lambda error: error["host_id"]),
    }
