"""Thread-safe host-side discovery and validation for model catalogs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Mapping, Protocol

from .models import CatalogEnvelope, DiscoveredCatalog, STALE_REASONS
from .scope import AccountScopeIDs


class CatalogDiscoveryError(RuntimeError):
    def __init__(self, category: str):
        if category not in STALE_REASONS:
            category = "protocol_error"
        super().__init__(category)
        self.category = category


class CatalogSelectionError(ValueError):
    pass


class CatalogAdapter(Protocol):
    def cache_identity(self) -> str:
        raise NotImplementedError

    def discover(self) -> DiscoveredCatalog:
        raise NotImplementedError


@dataclass(frozen=True)
class _CacheEntry:
    envelope: CatalogEnvelope
    identity: str
    expires_at: datetime


class ModelCatalogService:
    def __init__(
        self,
        *,
        host_id: str,
        adapters: Mapping[str, CatalogAdapter],
        scope_ids: AccountScopeIDs | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ):
        if ttl < timedelta(0):
            raise ValueError("catalog TTL cannot be negative")
        self._host_id = host_id
        self._adapters = dict(adapters)
        self._scope_ids = scope_ids if scope_ids is not None else AccountScopeIDs()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._ttl = ttl
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = RLock()

    def read(self, harness: str, force: bool = False) -> CatalogEnvelope:
        with self._lock:
            adapter = self._adapters.get(harness)
            if adapter is None:
                return CatalogEnvelope.empty_failure(
                    self._host_id, harness, "unsupported"
                )

            cached = self._cache.get(harness)
            try:
                identity = adapter.cache_identity()
                if not isinstance(identity, str):
                    raise ValueError("adapter cache identity must be a string")
            except Exception:
                return self._failed_read(harness, cached, "protocol_error")

            now = self._now()
            if (
                not force
                and cached is not None
                and cached.identity == identity
                and cached.expires_at > now
            ):
                return cached.envelope

            try:
                discovered = adapter.discover()
                if not isinstance(discovered, DiscoveredCatalog):
                    raise ValueError("adapter returned an invalid discovered catalog")
                envelope = CatalogEnvelope(
                    host_id=self._host_id,
                    harness=harness,
                    account_scope_id=self._scope_ids.for_material(
                        discovered.account_scope_material
                    ),
                    harness_version=discovered.harness_version,
                    discovered_at=now,
                    stale=discovered.source_stale,
                    stale_reason="offline" if discovered.source_stale else None,
                    models=discovered.models,
                )
            except CatalogDiscoveryError as error:
                return self._failed_read(harness, cached, error.category)
            except Exception:
                return self._failed_read(harness, cached, "protocol_error")

            self._cache[harness] = _CacheEntry(
                envelope=envelope, identity=identity, expires_at=now + self._ttl
            )
            return envelope

    def validate(
        self, harness: str, model: str | None, thinking_effort: str | None
    ) -> None:
        if model is None and thinking_effort is None:
            return

        catalog = self.read(harness)
        if catalog.stale:
            raise CatalogSelectionError(
                "Model choices are unavailable; refresh model choices and try again."
            )

        selected = None
        if model is not None:
            selected = next((item for item in catalog.models if item.id == model), None)
            if selected is None:
                raise CatalogSelectionError(
                    "This model is no longer available; refresh model choices and try again."
                )
        elif thinking_effort is not None:
            selected = next((item for item in catalog.models if item.is_default), None)

        if thinking_effort is not None:
            if selected is None or selected.reasoning is None:
                raise CatalogSelectionError(
                    "The selected reasoning effort is not supported by this model."
                )
            if thinking_effort not in selected.reasoning.supported:
                raise CatalogSelectionError(
                    "The selected reasoning effort is not supported by this model."
                )

    def invalidate(self, harness: str) -> None:
        with self._lock:
            cached = self._cache.get(harness)
            if cached is not None:
                self._cache[harness] = replace(
                    cached, expires_at=datetime.min.replace(tzinfo=timezone.utc)
                )

    def _failed_read(
        self, harness: str, cached: _CacheEntry | None, category: str
    ) -> CatalogEnvelope:
        if cached is None:
            return CatalogEnvelope.empty_failure(self._host_id, harness, category)
        return replace(cached.envelope, stale=True, stale_reason=category)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError(
                "model catalog clock must return a timezone-aware datetime"
            )
        return now
