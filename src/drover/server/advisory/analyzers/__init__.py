"""Immutable inputs and protocol for deterministic advisory analyzers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Protocol, runtime_checkable

from drover.server.advisory.types import FindingCandidate

MAX_SNAPSHOT_RECORDS = 512
_PROVIDER_STATUSES = frozenset({"ok", "usage_unavailable", "stale", "error"})


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_nonempty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _require_nonnegative(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True)
class ProviderResetWindow:
    """Provider-reported reset metadata, with no inferred capacity fields."""

    kind: str
    starts_at: datetime | None
    resets_at: datetime | None

    def __post_init__(self) -> None:
        _require_nonempty(self.kind, "kind")
        if self.starts_at is not None:
            _require_aware(self.starts_at, "starts_at")
        if self.resets_at is not None:
            _require_aware(self.resets_at, "resets_at")


@dataclass(frozen=True)
class ProviderConnectionObservation:
    provider: str
    account_label: str
    host_id: str
    enabled: bool
    status: str
    observed_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error_category: str | None
    reset_windows: tuple[ProviderResetWindow, ...]
    source_ref: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "account_label", "host_id", "source_ref"):
            _require_nonempty(getattr(self, field_name), field_name)
        if self.status not in _PROVIDER_STATUSES:
            raise ValueError("status must be a supported provider status")
        _require_aware(self.observed_at, "observed_at")
        if self.last_attempt_at is not None:
            _require_aware(self.last_attempt_at, "last_attempt_at")
        if self.last_success_at is not None:
            _require_aware(self.last_success_at, "last_success_at")
        windows = tuple(self.reset_windows)
        if len(windows) > MAX_SNAPSHOT_RECORDS:
            raise ValueError(f"reset_windows is bounded to {MAX_SNAPSHOT_RECORDS} rows")
        if not all(isinstance(item, ProviderResetWindow) for item in windows):
            raise ValueError("reset_windows must contain ProviderResetWindow records")
        object.__setattr__(self, "reset_windows", windows)


@dataclass(frozen=True)
class TelemetryAggregate:
    """Bounded coverage and token totals prepared by read-only lakehouse queries."""

    target_id: str
    host_id: str
    harness_id: str
    observed_at: datetime
    total_sessions: int
    sessions_with_spans: int
    repository_attributed_sessions: int
    token_observed_sessions: int
    cost_observed_sessions: int
    prompt_tokens: int
    cache_read_tokens: int
    source_ref: str

    def __post_init__(self) -> None:
        for field_name in ("target_id", "host_id", "harness_id", "source_ref"):
            _require_nonempty(getattr(self, field_name), field_name)
        _require_aware(self.observed_at, "observed_at")
        count_fields = (
            "total_sessions",
            "sessions_with_spans",
            "repository_attributed_sessions",
            "token_observed_sessions",
            "cost_observed_sessions",
            "prompt_tokens",
            "cache_read_tokens",
        )
        for field_name in count_fields:
            _require_nonnegative(getattr(self, field_name), field_name)
        for field_name in (
            "sessions_with_spans",
            "repository_attributed_sessions",
            "token_observed_sessions",
            "cost_observed_sessions",
        ):
            if getattr(self, field_name) > self.total_sessions:
                raise ValueError(f"{field_name} cannot exceed total_sessions")


@dataclass(frozen=True)
class RoutingAggregate:
    target_id: str
    host_id: str
    harness_id: str
    provider: str
    observed_at: datetime
    decision_count: int
    mismatch_count: int
    source_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "target_id",
            "host_id",
            "harness_id",
            "provider",
            "source_ref",
        ):
            _require_nonempty(getattr(self, field_name), field_name)
        _require_aware(self.observed_at, "observed_at")
        _require_nonnegative(self.decision_count, "decision_count")
        _require_nonnegative(self.mismatch_count, "mismatch_count")
        if self.mismatch_count > self.decision_count:
            raise ValueError("mismatch_count cannot exceed decision_count")


@dataclass(frozen=True)
class HookDescriptor:
    """Caller-verified hook metadata; deliberately excludes hook file content."""

    hook_id: str
    host_id: str
    harness_id: str
    canonical_config_path: str
    canonical_executable_path: str
    enabled: bool
    executable_exists: bool
    executable_is_file: bool
    executable_is_executable: bool
    allowlisted: bool
    observed_at: datetime
    source_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "hook_id",
            "host_id",
            "harness_id",
            "canonical_config_path",
            "canonical_executable_path",
            "source_ref",
        ):
            _require_nonempty(getattr(self, field_name), field_name)
        if not self.allowlisted:
            raise ValueError("hook descriptor must be allowlisted by the caller")
        for field_name in ("canonical_config_path", "canonical_executable_path"):
            path = PurePath(getattr(self, field_name))
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be an absolute canonical path")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class AnalysisSnapshot:
    """Frozen, bounded facts supplied to analyzers by the advisory service."""

    source_version: str
    analyzed_at: datetime
    provider_connections: tuple[ProviderConnectionObservation, ...] = ()
    telemetry: tuple[TelemetryAggregate, ...] = ()
    routing: tuple[RoutingAggregate, ...] = ()
    hooks: tuple[HookDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.source_version, "source_version")
        _require_aware(self.analyzed_at, "analyzed_at")
        collections = {
            "provider_connections": (
                self.provider_connections,
                ProviderConnectionObservation,
            ),
            "telemetry": (self.telemetry, TelemetryAggregate),
            "routing": (self.routing, RoutingAggregate),
            "hooks": (self.hooks, HookDescriptor),
        }
        for name, (values, item_type) in collections.items():
            frozen_values = tuple(values)
            if len(frozen_values) > MAX_SNAPSHOT_RECORDS:
                raise ValueError(f"{name} is bounded to {MAX_SNAPSHOT_RECORDS} rows")
            if not all(isinstance(item, item_type) for item in frozen_values):
                raise ValueError(f"{name} contains an invalid record")
            object.__setattr__(self, name, frozen_values)


@runtime_checkable
class Analyzer(Protocol):
    analyzer_id: str

    def analyze(self, snapshot: AnalysisSnapshot) -> list[FindingCandidate]: ...


__all__ = [
    "AnalysisSnapshot",
    "Analyzer",
    "HookDescriptor",
    "ProviderConnectionObservation",
    "ProviderResetWindow",
    "RoutingAggregate",
    "TelemetryAggregate",
]
