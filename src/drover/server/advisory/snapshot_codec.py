"""JSON boundary for bounded operational advisory snapshots."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from drover.server.advisory.analyzers import (
    AnalysisSnapshot,
    HookDescriptor,
    ProviderConnectionObservation,
    ProviderResetWindow,
    RoutingAggregate,
    TelemetryAggregate,
)


def _encode_datetime(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def encode_snapshot(snapshot: AnalysisSnapshot) -> dict[str, Any]:
    """Return only JSON types; the child never sends Python objects to its parent."""
    return json.loads(json.dumps(asdict(snapshot), default=_encode_datetime))


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def decode_snapshot(payload: dict[str, Any]) -> AnalysisSnapshot:
    """Rebuild the validated fact contracts after a local reader exits."""
    providers = []
    for item in payload["provider_connections"]:
        values = dict(item)
        for name in (
            "observed_at",
            "last_attempt_at",
            "last_success_at",
            "host_last_seen_at",
        ):
            values[name] = _moment(values[name])
        values["reset_windows"] = tuple(
            ProviderResetWindow(
                kind=window["kind"],
                starts_at=_moment(window["starts_at"]),
                resets_at=_moment(window["resets_at"]),
            )
            for window in values["reset_windows"]
        )
        providers.append(ProviderConnectionObservation(**values))

    telemetry = []
    for item in payload["telemetry"]:
        values = dict(item)
        values["observed_at"] = _moment(values["observed_at"])
        values["latest_span_at"] = _moment(values["latest_span_at"])
        telemetry.append(TelemetryAggregate(**values))

    routing = []
    for item in payload["routing"]:
        values = dict(item)
        values["observed_at"] = _moment(values["observed_at"])
        routing.append(RoutingAggregate(**values))

    hooks = []
    for item in payload["hooks"]:
        values = dict(item)
        values["observed_at"] = _moment(values["observed_at"])
        hooks.append(HookDescriptor(**values))

    return AnalysisSnapshot(
        source_version=payload["source_version"],
        analyzed_at=_moment(payload["analyzed_at"]),
        provider_connections=tuple(providers),
        telemetry=tuple(telemetry),
        routing=tuple(routing),
        hooks=tuple(hooks),
    )
