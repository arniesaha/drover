"""The advisory reader returns bounded, typed facts across a process boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module

import pytest

from drover.server.advisory.analyzers import (
    AnalysisSnapshot,
    HookDescriptor,
    ProviderConnectionObservation,
    ProviderResetWindow,
    RoutingAggregate,
    TelemetryAggregate,
)

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)


def _complete_snapshot() -> AnalysisSnapshot:
    return AnalysisSnapshot(
        source_version="facts:v1",
        analyzed_at=NOW,
        provider_connections=(
            ProviderConnectionObservation(
                provider="openai",
                account_label="personal",
                host_id="mac-mini",
                enabled=True,
                status="ok",
                observed_at=NOW,
                last_attempt_at=NOW,
                last_success_at=NOW,
                error_category=None,
                reset_windows=(
                    ProviderResetWindow(kind="weekly", starts_at=NOW, resets_at=NOW),
                ),
                reset_windows_complete=True,
                source_ref="provider:openai/personal",
                host_last_seen_at=NOW,
            ),
        ),
        telemetry=(
            TelemetryAggregate(
                target_id="mac-mini/codex",
                host_id="mac-mini",
                harness_id="codex",
                observed_at=NOW,
                total_sessions=2,
                sessions_with_spans=2,
                repository_attributed_sessions=1,
                token_observed_sessions=2,
                cost_observed_sessions=1,
                prompt_tokens=100,
                cache_read_tokens=20,
                facts_complete=True,
                input_span_records=3,
                source_ref="telemetry:mac-mini/codex",
                latest_span_at=NOW,
                exact_cache_metric_pair_sessions=1,
                exact_cache_metric_pair_prompt_tokens=100,
                exact_cache_metric_pair_cache_read_tokens=20,
            ),
        ),
        routing=(
            RoutingAggregate(
                target_id="mac-mini/codex",
                host_id="mac-mini",
                harness_id="codex",
                provider="openai",
                observed_at=NOW,
                decision_count=2,
                mismatch_count=1,
                facts_complete=True,
                input_span_records=3,
                source_ref="routing:mac-mini/codex",
            ),
        ),
        hooks=(
            HookDescriptor(
                hook_id="hook-1",
                host_id="mac-mini",
                harness_id="codex",
                canonical_config_path="/tmp/hooks.json",
                canonical_executable_path="/tmp/hook",
                target_hash="sha256:abc",
                enabled=True,
                executable_exists=True,
                executable_is_file=True,
                executable_is_executable=True,
                allowlisted=True,
                observed_at=NOW,
                source_ref="hook:hook-1",
            ),
        ),
    )


def test_snapshot_codec_preserves_all_operational_fact_types() -> None:
    codec = import_module("drover.server.advisory.snapshot_codec")
    snapshot = _complete_snapshot()

    assert codec.decode_snapshot(codec.encode_snapshot(snapshot)) == snapshot


def test_snapshot_codec_rejects_naive_time() -> None:
    codec = import_module("drover.server.advisory.snapshot_codec")
    payload = codec.encode_snapshot(_complete_snapshot())
    payload["analyzed_at"] = "2026-09-25T18:00:00"

    with pytest.raises(ValueError, match="analyzed_at must be timezone-aware"):
        codec.decode_snapshot(payload)
