"""Production-shaped advisory facts through the served DuckLake gateway."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from served_lake import build_served_lake, drop_served_lake

from drover.schema import bootstrap
from drover.server.advisory.analyzers.connectors import ProviderResetWindowAnalyzer
from drover.server.advisory.jobs import enqueue_advisory_check
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.worker import AdvisoryWorker, load_operational_snapshot
from drover.server.db import control_plane_connection
from drover.server.lake.runtime import LakeError
from drover.server.lake.serving import configure_analytics

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
WINDOWS_PER_CONNECTION = 32
CONNECTIONS = 33


def _provider_rows() -> list[dict]:
    """More than the reader's 1,000-row cap, grouped into 33 result rows."""
    rows = []
    for account in range(CONNECTIONS):
        for window in range(WINDOWS_PER_CONNECTION):
            rows.append(
                {
                    "snapshot_id": f"snapshot-{account}",
                    "dedup_key": f"snapshot-{account}-{window}",
                    "provider": "openai",
                    "account_label": f"account-{account}",
                    "account_identity": None,
                    "plan_label": "plus",
                    "host_id": "studio-fixture",
                    "status": "ok",
                    "observed_at": NOW,
                    "source": "production-shaped-fixture",
                    "error_category": None,
                    "window_kind": f"window-{window:02d}",
                    "used_percent": 25.0,
                    "limit_value": None,
                    "remaining_value": None,
                    "unit": None,
                    "window_minutes": None,
                    "starts_at": NOW + timedelta(minutes=window),
                    "resets_at": NOW + timedelta(hours=window + 1),
                }
            )
    return rows


@pytest.mark.slow_integration
def test_first_advisory_review_reads_ducklake_provider_windows(
    prod_shaped: Path,
    postgres_dsn: str,
    lake_extension_dir: Path,
    small_lake: Path,
    tmp_path: Path,
) -> None:
    """Exercise startup, the real reader child, and the first advisory job.

    The analytical file intentionally has no legacy provider view. Connector
    facts live in its local projection, while all 1,056 reset-window facts are
    read from a rebuilt and verified DuckLake through ``open_history``.
    """
    served = build_served_lake(
        parquet_root=small_lake,
        postgres_dsn=postgres_dsn,
        extension_dir=lake_extension_dir,
        workdir=tmp_path / "served",
        label="advisory_provider_windows",
        provider_rows=_provider_rows(),
    )
    try:
        bootstrap(
            parquet_dir=tmp_path / "local-parquet",
            duckdb_path=prod_shaped,
            bind_parquet_views=False,
        )
        configure_analytics(prod_shaped, served.config)
        with duckdb.connect(str(prod_shaped)) as con:
            with pytest.raises(duckdb.CatalogException, match="provider_usage_snapshots"):
                con.execute("SELECT count(*) FROM provider_usage_snapshots")
            for account in range(CONNECTIONS):
                con.execute(
                    """
                    INSERT INTO provider_connections (
                      provider, account_label, host_id, enabled, last_attempt_at,
                      last_success_at, updated_at
                    ) VALUES ('openai', ?, 'studio-fixture', TRUE, ?, ?, ?)
                    """,
                    [f"account-{account}", NOW, NOW, NOW],
                )
        with control_plane_connection(prod_shaped) as con:
            con.execute(
                """
                INSERT INTO harness_hosts (
                  host_id, display_name, kind, status, capabilities_json,
                  last_seen_at, updated_at
                ) VALUES ('studio-fixture', 'Studio fixture', 'local', 'online', '{}', ?, ?)
                """,
                [NOW, NOW],
            )

        snapshot = load_operational_snapshot(
            prod_shaped,
            ProviderResetWindowAnalyzer.analyzer_id,
            "fleet",
            "facts:ducklake-provider-windows",
            analyzed_at=NOW,
        )
        assert len(snapshot.provider_connections) == CONNECTIONS
        assert all(
            len(connection.reset_windows) == WINDOWS_PER_CONNECTION
            and connection.reset_windows_complete
            for connection in snapshot.provider_connections
        )

        # A real unreadable catalog must remain an error, never become an
        # apparently healthy empty provider inventory.
        configure_analytics(
            prod_shaped,
            replace(served.config, data_root=str(tmp_path / "missing")),
        )
        with pytest.raises(LakeError):
            load_operational_snapshot(
                prod_shaped,
                ProviderResetWindowAnalyzer.analyzer_id,
                "fleet",
                "facts:broken-reader",
                analyzed_at=NOW,
            )
        configure_analytics(prod_shaped, served.config)

        enqueue_advisory_check(
            prod_shaped,
            analyzer_id=ProviderResetWindowAnalyzer.analyzer_id,
            target_id="fleet",
            source_version="facts:ducklake-provider-windows",
        )
        worker = AdvisoryWorker(
            duckdb_path=prod_shaped,
            repository=AdvisoryRepository(prod_shaped),
            snapshot_factory=lambda analyzer_id, target_id, source_version: load_operational_snapshot(
                prod_shaped,
                analyzer_id,
                target_id,
                source_version,
                analyzed_at=NOW,
            ),
        )
        result = worker.run_once([ProviderResetWindowAnalyzer()])
        assert result.succeeded == 1
        assert result.failed == 0
    finally:
        drop_served_lake(postgres_dsn, served)
