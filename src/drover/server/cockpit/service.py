"""Composition and bounded runtime refresh for cockpit APIs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import logging
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable

from drover.server.cockpit.analytics import (
    ActivityAnalytics,
    AnalyticsCursorCodec,
    AnalyticsFilters,
    activity_analytics,
)
from drover.server.db import open_duckdb_connection

log = logging.getLogger("drover.cockpit")

COCKPIT_API_VERSION = 1
COCKPIT_SECTIONS = (
    "provider_capacity",
    "activity",
    "popular_projects",
    "insights",
)
PROVIDER_REFRESH_INTERVAL_SECONDS = 300.0
# The iOS client abandons a cockpit request at 15s (DroverClient's default
# timeoutInterval). The overview is one response, so the activity section has to
# leave room for the other sections, for a cold cache, and for a phone's
# transport. Measured on the live fleet, a 6s budget still produced a 12.4s cold
# overview -- inside the limit, but with too little margin to survive a slow
# network. A query that cannot answer in 6s cannot answer in 3s either, so the
# shorter budget costs nothing real and buys back the headroom.
ACTIVITY_BUDGET_SECONDS = 3.0
_INTERRUPT_GRACE_SECONDS = 1.0


class CockpitService:
    """Keep provider-reported capacity separate from observed activity."""

    def __init__(
        self,
        *,
        duckdb_path: str | Path | None,
        provider_usage: Any | None,
        connect: Callable[[], Any] | None = None,
        advisory_repository: Any | None = None,
        cursor_secret: bytes | None = None,
    ) -> None:
        self.duckdb_path = Path(duckdb_path) if duckdb_path is not None else None
        self.provider_usage = provider_usage
        self._connect = connect
        if advisory_repository is None and self.duckdb_path is not None:
            from drover.server.advisory.repository import AdvisoryRepository

            advisory_repository = AdvisoryRepository(self.duckdb_path)
        self.advisory_repository = advisory_repository
        self._cursor_codec = AnalyticsCursorCodec(
            cursor_secret or secrets.token_bytes(32)
        )

    def overview(self, filters: AnalyticsFilters) -> dict[str, Any]:
        provider_capacity = self._provider_capacity(filters)
        activity = self._activity(filters)
        projects = []
        if activity["status"] == "ok":
            analytics = activity["data"]
            projects = [
                {**project, "metric": analytics["project_metric"]}
                for project in analytics["projects"]
            ]
        return {
            "cockpit_api_version": COCKPIT_API_VERSION,
            "provider_capacity": provider_capacity,
            "activity": activity,
            "popular_projects": projects,
            "insight_counts": self._insight_counts(),
        }

    def analytics(self, filters: AnalyticsFilters) -> dict[str, Any]:
        return {
            "cockpit_api_version": COCKPIT_API_VERSION,
            "filters": asdict(filters),
            "provider_capacity": self._provider_capacity(filters),
            "activity": self._activity(filters),
        }

    def _provider_capacity(self, filters: AnalyticsFilters) -> dict[str, Any]:
        if self.provider_usage is None:
            return _section("unavailable", data=[], coverage=None)
        try:
            accounts = [
                account
                for account in self.provider_usage.latest_accounts()
                if (filters.host_id is None or account.host_id == filters.host_id)
                and (filters.provider is None or account.provider == filters.provider)
            ]
            data = [asdict(account) for account in accounts]
            observed_at = max(
                (account.observed_at for account in accounts), default=None
            )
            # Section status describes the *section*: whether what we are
            # showing is current. The client treats it as authoritative over
            # every card, so folding per-account failures in here relabels
            # healthy, freshly-observed accounts as "Stale" -- one erroring
            # account did exactly that to seven good ones, and one host going
            # dark did it again. The section is stale only when nothing in it
            # is current. Account-level failures travel on the account and
            # render on their own card.
            status = (
                "ok"
                if not accounts
                or any(
                    account.status in {"ok", "usage_unavailable"}
                    for account in accounts
                )
                else "stale"
            )
            return _section(
                status,
                data=data,
                observed_at=observed_at,
                coverage={"source": "provider_reported", "account_count": len(data)},
            )
        except Exception as exc:  # noqa: BLE001 - isolate response sections
            log.warning("failed to render provider capacity: %s", exc)
            return _section("error", data=[], coverage=None)

    def _activity(self, filters: AnalyticsFilters) -> dict[str, Any]:
        con = None
        owns_connection = self._connect is None
        try:
            if self._connect is not None:
                con = self._connect()
            elif self.duckdb_path is not None:
                con = open_duckdb_connection(
                    self.duckdb_path, read_only=True, role="diagnostic"
                )
            else:
                raise RuntimeError("activity store is unavailable")
            result = self._activity_within_budget(con, filters)
            data = asdict(result)
            return _section(
                "ok",
                data=data,
                observed_at=result.metadata.observed_at,
                coverage=asdict(result.coverage),
            )
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate response sections
            log.warning("failed to render observed activity: %s", exc)
            return _section("error", data=None, coverage=None)
        finally:
            if owns_connection and con is not None:
                con.close()

    def _activity_within_budget(
        self, con: Any, filters: AnalyticsFilters
    ) -> ActivityAnalytics:
        """Run the activity query, but never for longer than its budget.

        The overview is assembled from several sections and returned as one
        response, so the slowest section sets the client's wait. The iOS client
        gives up at 15 seconds; when this query ran long the whole response was
        lost, taking provider capacity and insight counts -- which had both
        succeeded -- down with it.

        A budget alone would only stop us waiting: the query would keep running
        and keep holding memory. `interrupt()` is what actually stops it, so a
        section that cannot answer in time costs a thread for its budget and
        nothing after that.
        """
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                outcome["result"] = activity_analytics(
                    con, filters, cursor_codec=self._cursor_codec
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=run, name="cockpit-activity", daemon=True)
        worker.start()
        if not done.wait(ACTIVITY_BUDGET_SECONDS):
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001 - interrupt is best effort
                pass
            # Give the interrupt a moment to unwind the query thread so the
            # connection is not closed underneath it.
            done.wait(_INTERRUPT_GRACE_SECONDS)
            raise TimeoutError(
                f"activity query exceeded {ACTIVITY_BUDGET_SECONDS:g}s budget"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def _insight_counts(self) -> dict[str, int] | None:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        if self.advisory_repository is None:
            return counts
        try:
            for finding in self.advisory_repository.list_findings():
                if finding.state.value not in {"open", "regressed"}:
                    continue
                counts[finding.severity.value] += 1
            return counts
        except Exception as exc:  # noqa: BLE001 - isolate response sections
            log.warning("failed to render insight counts: %s", exc)
            return None


class ProviderRefreshLoop:
    """Refresh online hosts at a hard lower bound between attempts."""

    def __init__(
        self,
        *,
        provider_usage: Any,
        registry: Any,
        shutdown_event: threading.Event,
        interval_seconds: float = PROVIDER_REFRESH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        fetch: Callable[[Any], Any] | None = None,
        operational_source_version: Callable[[str], str] | None = None,
        on_operational_change: Callable[[str, str], None] | None = None,
    ) -> None:
        if interval_seconds < PROVIDER_REFRESH_INTERVAL_SECONDS:
            raise ValueError("provider refresh interval must be at least 300 seconds")
        self.provider_usage = provider_usage
        self.registry = registry
        self.shutdown_event = shutdown_event
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.fetch = fetch
        self.operational_source_version = operational_source_version
        self.on_operational_change = on_operational_change
        self._last_attempt: dict[str, float] = {}
        self._offline_hosts: set[str] = set()
        self._last_operational_version: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="drover-provider-refresh", daemon=True
        )
        self._thread.start()

    def run_once(self) -> None:
        now = self.clock()
        try:
            hosts = self.registry.list_hosts()
        except Exception as exc:  # noqa: BLE001 - refresh cannot kill server
            log.warning("failed to list hosts for provider refresh: %s", exc)
            return
        for host in hosts:
            host_id = str(getattr(host, "host_id", "") or "").strip()
            if not host_id:
                continue
            status = str(getattr(host, "status", "online") or "").lower()
            if status != "online":
                try:
                    self.provider_usage.mark_host_unavailable(
                        host_id, error_category="host_offline"
                    )
                except Exception as exc:  # noqa: BLE001 - status overlay is isolated
                    log.warning(
                        "failed to mark provider host %s unavailable: %s",
                        host_id,
                        exc,
                    )
                self._offline_hosts.add(host_id)
                continue
            previous = self._last_attempt.get(host_id)
            recovering = host_id in self._offline_hosts
            if (
                not recovering
                and previous is not None
                and now - previous < self.interval_seconds
            ):
                continue
            self._last_attempt[host_id] = now
            self._offline_hosts.discard(host_id)
            try:
                if self.fetch is None:
                    self.provider_usage.refresh_host(host)
                else:
                    self.provider_usage.refresh_host(host, fetch=self.fetch)
                if (
                    self.on_operational_change is not None
                    and self.operational_source_version is not None
                ):
                    version = self.operational_source_version(host_id)
                    try:
                        if self._last_operational_version.get(host_id) != version:
                            self.on_operational_change(host_id, version)
                            self._last_operational_version[host_id] = version
                    except Exception as exc:  # noqa: BLE001 - scheduling is isolated
                        log.warning(
                            "failed to enqueue advisory checks for host %s: %s",
                            host_id,
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001 - defensive connector isolation
                log.warning("provider refresh failed for host %s: %s", host_id, exc)

    def _run(self) -> None:
        while not self.shutdown_event.is_set():
            self.run_once()
            self.shutdown_event.wait(self.interval_seconds)


def _section(
    status: str,
    *,
    data: Any,
    coverage: dict[str, Any] | None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "observed_at": observed_at,
        "coverage": coverage,
        "data": data,
    }
