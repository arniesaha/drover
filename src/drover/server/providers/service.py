"""Central persistence and last-good projection for provider usage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen
from uuid import uuid4

import pyarrow as pa

from drover.server.db import open_duckdb_connection
from drover.server.parquet_io import atomic_write_table
from drover.server.providers.types import (
    ProviderAccountSnapshot,
    ProviderStatus,
    ProviderUsageWindow,
    provider_snapshot_table,
)

FetchProviderUsage = Callable[[Any], Mapping[str, Any]]
_SUCCESS_STATUSES = frozenset({"ok", "usage_unavailable"})


def provider_operational_source_version(duckdb_path: str | Path, host_id: str) -> str:
    """Hash current material connector/quota state for one host."""

    host_id = str(host_id).strip()
    if not host_id:
        raise ValueError("host_id is required")
    con = open_duckdb_connection(Path(duckdb_path), read_only=True, role="diagnostic")
    try:
        connections = con.execute(
            """
            SELECT provider, account_label, enabled, supports_usage,
                   supports_limits, supports_account_discovery,
                   supports_refresh, capabilities_json, error_category,
                   CASE
                     WHEN error_category IS NULL THEN FALSE
                     WHEN last_success_at IS NULL THEN TRUE
                     ELSE last_attempt_at IS NOT NULL
                          AND last_attempt_at >= last_success_at
                   END AS failed
            FROM provider_connections
            WHERE host_id = ?
            ORDER BY provider, account_label
            """,
            [host_id],
        ).fetchall()
        snapshots = con.execute(
            """
            WITH latest AS (
              SELECT provider, account_label, host_id,
                     arg_max(snapshot_id, observed_at) AS snapshot_id
              FROM provider_usage_snapshots
              WHERE host_id = ?
              GROUP BY provider, account_label, host_id
            )
            SELECT p.provider, p.account_label, p.dedup_key
            FROM provider_usage_snapshots p
            JOIN latest l USING (provider, account_label, host_id, snapshot_id)
            GROUP BY p.provider, p.account_label, p.dedup_key
            ORDER BY p.provider, p.account_label
            """,
            [host_id],
        ).fetchall()
    finally:
        con.close()
    material = {"connections": connections, "snapshots": snapshots}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"provider-state:{digest}"


class ProviderUsageService:
    """Refresh host-local provider facts into the central lakehouse."""

    def __init__(
        self,
        duckdb_path: str | Path,
        parquet_dir: str | Path,
        *,
        api_token: str | None = None,
        timeout_s: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        freshness_threshold_seconds: float = 600.0,
    ) -> None:
        if (
            type(freshness_threshold_seconds) not in (int, float)
            or not math.isfinite(freshness_threshold_seconds)
            or freshness_threshold_seconds <= 0
        ):
            raise ValueError(
                "provider freshness threshold must be a finite positive number"
            )
        self.duckdb_path = Path(duckdb_path)
        self.parquet_dir = Path(parquet_dir)
        self.api_token = api_token
        self.timeout_s = timeout_s
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.freshness_threshold_seconds = float(freshness_threshold_seconds)
        self.snapshot_dir = self.parquet_dir / "provider_usage_snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def refresh_host(
        self,
        host: Any,
        *,
        fetch: FetchProviderUsage | None = None,
    ) -> tuple[ProviderAccountSnapshot, ...]:
        """Fetch and persist one host observation without propagating failures."""
        attempted_at = self.clock()
        host_id = _host_value(host, "host_id")
        if not host_id:
            raise ValueError("host_id is required")
        try:
            payload = (fetch or self._fetch_host)(host)
            snapshots = _snapshots_from_payload(payload, host_id=host_id)
            if not snapshots:
                self._record_host_failure(
                    host_id,
                    attempted_at=attempted_at,
                    error_category="empty_inventory",
                )
                return ()
            snapshots = self._scope_connector_errors(snapshots)
            self._persist_new_snapshots(snapshots, host_id=host_id)
            self._record_snapshot_attempts(snapshots, attempted_at=attempted_at)
            self._retire_unreported_accounts(snapshots, host_id=host_id)
            return snapshots
        except Exception as exc:  # A connector failure must not stop refresh loops.
            self._record_host_failure(
                host_id,
                attempted_at=attempted_at,
                error_category=_error_category(exc),
            )
            return ()

    def _scope_connector_errors(
        self, snapshots: tuple[ProviderAccountSnapshot, ...]
    ) -> tuple[ProviderAccountSnapshot, ...]:
        """Map provider/host errors onto stable discovered account identities."""
        keys = {
            (snapshot.provider, snapshot.host_id)
            for snapshot in snapshots
            if snapshot.status == "error"
        }
        if not keys:
            return snapshots
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            existing = con.execute("""
                SELECT provider, host_id, account_label
                FROM provider_connections
                WHERE enabled
                ORDER BY provider, host_id, account_label
                """).fetchall()
        finally:
            con.close()
        account_labels: dict[tuple[str, str], set[str]] = {key: set() for key in keys}
        for provider, host_id, account_label in existing:
            key = (str(provider), str(host_id))
            if key in account_labels:
                account_labels[key].add(str(account_label))
        for snapshot in snapshots:
            if snapshot.status in _SUCCESS_STATUSES:
                account_labels.setdefault(
                    (snapshot.provider, snapshot.host_id), set()
                ).add(snapshot.account_label)

        scoped: list[ProviderAccountSnapshot] = []
        for snapshot in snapshots:
            if snapshot.status != "error":
                scoped.append(snapshot)
                continue
            labels = account_labels.get((snapshot.provider, snapshot.host_id), set())
            if not labels or snapshot.account_label in labels:
                scoped.append(snapshot)
                continue
            scoped.extend(
                _scoped_error_snapshot(snapshot, label) for label in sorted(labels)
            )
        return tuple(scoped)

    def _retire_unreported_accounts(
        self,
        snapshots: tuple[ProviderAccountSnapshot, ...],
        *,
        host_id: str,
    ) -> None:
        """Disable identities a host has stopped reporting for a provider.

        An account label can be abandoned: a probe that fails before reading
        the account falls back to a provider-generic label, and that label
        becomes its own identity. Nothing rewrites it once the host starts
        reporting the real account, so without this it stays projected forever
        at the reading that stranded it. Scoped to the providers named in this
        refresh, so a host reporting one provider cannot retire another.
        """
        if not snapshots:
            return
        reported: dict[str, set[str]] = {}
        for snapshot in snapshots:
            reported.setdefault(snapshot.provider, set()).add(snapshot.account_label)
        con = open_duckdb_connection(self.duckdb_path)
        try:
            for provider, labels in reported.items():
                placeholders = ",".join("?" for _ in labels)
                con.execute(
                    f"""
                    UPDATE provider_connections
                    SET enabled = FALSE, updated_at = ?
                    WHERE host_id = ? AND provider = ?
                      AND account_label NOT IN ({placeholders})
                    """,
                    [self.clock(), host_id, provider, *sorted(labels)],
                )
        finally:
            con.close()

    def latest_accounts(self) -> list[ProviderAccountSnapshot]:
        """Return last-good accounts with current connector state overlaid."""
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            rows = _rows(con.execute("""
                    SELECT *
                    FROM provider_usage_snapshots
                    ORDER BY observed_at DESC, snapshot_id, window_kind
                    """))
            connections = {
                (row["provider"], row["account_label"], row["host_id"]): row
                for row in _rows(con.execute("SELECT * FROM provider_connections"))
            }
        finally:
            con.close()

        snapshots = _snapshots_from_rows(rows)
        by_account: dict[tuple[str, str, str], list[ProviderAccountSnapshot]] = {}
        for snapshot in snapshots:
            key = (snapshot.provider, snapshot.account_label, snapshot.host_id)
            by_account.setdefault(key, []).append(snapshot)

        latest: list[ProviderAccountSnapshot] = []
        now = self.clock()
        for key, account_snapshots in by_account.items():
            if connections.get(key, {}).get("enabled") is False:
                # Retired identity: its snapshots stay as history, but the host
                # no longer reports it, so it is not a current account.
                continue
            base = next(
                (
                    snapshot
                    for snapshot in account_snapshots
                    if snapshot.status in _SUCCESS_STATUSES
                ),
                account_snapshots[0],
            )
            connection = connections.get(key)
            provider_observed_at = base.observed_at
            last_success_at = connection.get("last_success_at") if connection else None
            effective_observed_at = (
                last_success_at
                if isinstance(last_success_at, datetime)
                else base.observed_at
            )
            freshness_age_seconds = max(
                0.0, (now - effective_observed_at).total_seconds()
            )
            base = replace(
                base,
                observed_at=effective_observed_at,
                provider_observed_at=provider_observed_at,
                freshness_age_seconds=freshness_age_seconds,
            )
            if connection and _connection_failed(connection):
                status: ProviderStatus = (
                    "stale" if base.status in _SUCCESS_STATUSES else "error"
                )
                base = replace(
                    base,
                    status=status,
                    error_category=connection.get("error_category"),
                )
            elif (
                base.status in _SUCCESS_STATUSES
                and freshness_age_seconds > self.freshness_threshold_seconds
            ):
                base = replace(
                    base,
                    status="stale",
                    error_category="freshness_expired",
                )
            elif base.status in _SUCCESS_STATUSES and any(
                window.resets_at is not None and window.resets_at <= now
                for window in base.windows
            ):
                base = replace(
                    base,
                    status="stale",
                    error_category="provider_window_expired",
                )
            latest.append(base)
        return sorted(
            latest,
            key=lambda item: (item.provider, item.account_label, item.host_id),
        )

    def operational_source_version(self, host_id: str) -> str:
        """Hash material connector/quota state while ignoring refresh clocks."""
        return provider_operational_source_version(self.duckdb_path, host_id)

    def mark_host_unavailable(
        self, host_id: str, *, error_category: str = "host_offline"
    ) -> None:
        """Overlay an unavailable fleet state without mutating snapshot facts."""
        host_id = str(host_id).strip()
        if not host_id:
            raise ValueError("host_id is required")
        self._record_host_failure(
            host_id,
            attempted_at=self.clock(),
            error_category=error_category,
        )

    def _fetch_host(self, host: Any) -> Mapping[str, Any]:
        endpoint = (
            _host_value(host, "local_url") or _host_value(host, "tailscale_url")
        ).rstrip("/")
        if not endpoint:
            raise RuntimeError("unavailable")
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = Request(f"{endpoint}/providers/usage", headers=headers)
        with urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("provider usage payload must be an object")
        return payload

    def _persist_new_snapshots(
        self,
        snapshots: tuple[ProviderAccountSnapshot, ...],
        *,
        host_id: str,
    ) -> None:
        if not snapshots:
            return
        con = open_duckdb_connection(
            self.duckdb_path, read_only=True, role="diagnostic"
        )
        try:
            existing = {str(row[0]) for row in con.execute("""
                    SELECT DISTINCT dedup_key
                    FROM provider_usage_snapshots
                    WHERE dedup_key IS NOT NULL
                    """).fetchall()}
        finally:
            con.close()
        new_snapshots = [
            snapshot for snapshot in snapshots if snapshot.dedup_key not in existing
        ]
        if not new_snapshots:
            return
        table = pa.concat_tables(
            [provider_snapshot_table(snapshot) for snapshot in new_snapshots]
        )
        safe_host_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", host_id).strip("-")
        safe_host_id = safe_host_id or "host"
        out_path = self.snapshot_dir / f"part-{safe_host_id}-{uuid4().hex}.parquet"
        atomic_write_table(table, out_path, compression="zstd")

    def _record_snapshot_attempts(
        self,
        snapshots: tuple[ProviderAccountSnapshot, ...],
        *,
        attempted_at: datetime,
    ) -> None:
        if not snapshots:
            return
        con = open_duckdb_connection(self.duckdb_path)
        try:
            for snapshot in snapshots:
                succeeded = snapshot.status in _SUCCESS_STATUSES
                con.execute(
                    """
                    INSERT INTO provider_connections (
                      provider, account_label, host_id, enabled,
                      supports_usage, supports_limits,
                      supports_account_discovery, supports_refresh,
                      capabilities_json, last_attempt_at, last_success_at,
                      error_category, updated_at
                    ) VALUES (?, ?, ?, TRUE, ?, ?, TRUE, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (provider, account_label, host_id) DO UPDATE SET
                      enabled = TRUE,
                      supports_usage = excluded.supports_usage,
                      supports_limits = excluded.supports_limits,
                      supports_account_discovery = TRUE,
                      supports_refresh = excluded.supports_refresh,
                      capabilities_json = excluded.capabilities_json,
                      last_attempt_at = excluded.last_attempt_at,
                      last_success_at = CASE
                        WHEN excluded.error_category IS NULL
                          THEN excluded.last_success_at
                        ELSE provider_connections.last_success_at
                      END,
                      error_category = excluded.error_category,
                      updated_at = excluded.updated_at
                    """,
                    [
                        snapshot.provider,
                        snapshot.account_label,
                        snapshot.host_id,
                        snapshot.status == "ok",
                        bool(snapshot.windows),
                        snapshot.status == "ok",
                        json.dumps(
                            {
                                "source": snapshot.source,
                                "window_kinds": [
                                    window.kind for window in snapshot.windows
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        attempted_at,
                        attempted_at if succeeded else None,
                        (
                            None
                            if succeeded
                            else snapshot.error_category or snapshot.status
                        ),
                        attempted_at,
                    ],
                )
        finally:
            con.close()

    def _record_host_failure(
        self,
        host_id: str,
        *,
        attempted_at: datetime,
        error_category: str,
    ) -> None:
        con = open_duckdb_connection(self.duckdb_path)
        try:
            con.execute(
                """
                UPDATE provider_connections
                SET last_attempt_at = ?, error_category = ?, updated_at = ?
                WHERE host_id = ?
                """,
                [attempted_at, error_category, attempted_at, host_id],
            )
        finally:
            con.close()


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _host_value(host: Any, field: str) -> str:
    value = host.get(field) if isinstance(host, Mapping) else getattr(host, field, None)
    return str(value or "").strip()


def _parse_datetime(
    value: Any, field: str, *, required: bool = False
) -> datetime | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _snapshots_from_payload(
    payload: Mapping[str, Any], *, host_id: str
) -> tuple[ProviderAccountSnapshot, ...]:
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("provider usage accounts must be a list")
    snapshots: list[ProviderAccountSnapshot] = []
    for account in accounts:
        if not isinstance(account, Mapping):
            raise ValueError("provider usage account must be an object")
        windows_value = account.get("windows", [])
        if not isinstance(windows_value, list):
            raise ValueError("provider usage windows must be a list")
        windows = tuple(_window_from_payload(window) for window in windows_value)
        snapshots.append(
            ProviderAccountSnapshot(
                snapshot_id=str(account.get("snapshot_id") or "").strip(),
                dedup_key=str(account.get("dedup_key") or "").strip(),
                provider=str(account.get("provider") or "").strip(),
                account_label=str(account.get("account_label") or "").strip(),
                plan_label=_optional_text(account.get("plan_label")),
                host_id=host_id,
                status=str(account.get("status") or "error"),  # type: ignore[arg-type]
                observed_at=_parse_datetime(
                    account.get("observed_at"), "observed_at", required=True
                ),  # type: ignore[arg-type]
                windows=windows,
                source=_canonical_source(account.get("source")),
                error_category=_optional_text(account.get("error_category")),
            )
        )
    return tuple(snapshots)


def _window_from_payload(value: Any) -> ProviderUsageWindow:
    if not isinstance(value, Mapping):
        raise ValueError("provider usage window must be an object")
    return ProviderUsageWindow(
        kind=str(value.get("kind") or "").strip(),
        used_percent=value.get("used_percent"),
        limit_value=value.get("limit_value"),
        remaining_value=value.get("remaining_value"),
        unit=_optional_text(value.get("unit")),
        window_minutes=value.get("window_minutes"),
        starts_at=_parse_datetime(value.get("starts_at"), "starts_at"),
        resets_at=_parse_datetime(value.get("resets_at"), "resets_at"),
    )


def _snapshots_from_rows(rows: list[dict[str, Any]]) -> list[ProviderAccountSnapshot]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        snapshot_id = str(row["snapshot_id"])
        if snapshot_id not in grouped:
            grouped[snapshot_id] = []
            order.append(snapshot_id)
        grouped[snapshot_id].append(row)
    snapshots: list[ProviderAccountSnapshot] = []
    for snapshot_id in order:
        snapshot_rows = grouped[snapshot_id]
        first = snapshot_rows[0]
        windows = tuple(
            ProviderUsageWindow(
                kind=str(row["window_kind"]),
                used_percent=row["used_percent"],
                limit_value=row["limit_value"],
                remaining_value=row["remaining_value"],
                unit=row["unit"],
                window_minutes=row["window_minutes"],
                starts_at=row["starts_at"],
                resets_at=row["resets_at"],
            )
            for row in snapshot_rows
            if row["window_kind"] is not None
        )
        snapshots.append(
            ProviderAccountSnapshot(
                snapshot_id=snapshot_id,
                dedup_key=first["dedup_key"],
                provider=first["provider"],
                account_label=first["account_label"],
                plan_label=first["plan_label"],
                host_id=first["host_id"],
                status=first["status"],
                observed_at=first["observed_at"],
                windows=windows,
                source=_canonical_source(first["source"]),
                error_category=first["error_category"],
            )
        )
    return snapshots


def _connection_failed(connection: Mapping[str, Any]) -> bool:
    if not connection.get("error_category"):
        return False
    last_attempt = connection.get("last_attempt_at")
    last_success = connection.get("last_success_at")
    return last_success is None or (
        last_attempt is not None and last_attempt >= last_success
    )


def _scoped_error_snapshot(
    snapshot: ProviderAccountSnapshot, account_label: str
) -> ProviderAccountSnapshot:
    scope = f"{snapshot.provider}|{snapshot.host_id}|{account_label}"
    snapshot_suffix = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    dedup_key = hashlib.sha256(
        f"{snapshot.dedup_key}|{scope}".encode("utf-8")
    ).hexdigest()
    return replace(
        snapshot,
        snapshot_id=f"{snapshot.snapshot_id}-{snapshot_suffix}",
        dedup_key=dedup_key,
        account_label=account_label,
    )


def _error_category(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    category = getattr(exc, "reason", None)
    if isinstance(category, TimeoutError):
        return "timeout"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "protocol_error"
    text = str(exc).strip()
    return text if text in {"timeout", "unavailable"} else "unavailable"


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _canonical_source(value: Any) -> str:
    source = str(value or "").strip()
    return {
        "codex_app_server": "codex-app-server",
        "harness_inventory": "harness-inventory",
    }.get(source, source)
