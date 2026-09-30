"""Synthetic service-level cockpit RSS probe; run from a development checkout.

Uses the analytics test schema, 50k sessions / spans, and ten uncached requests.
PYTHONPATH can select baseline src while keeping this workload unchanged.
RSS is the parent process only; it does not include the short-lived reader.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from drover.server.cockpit.analytics import AnalyticsFilters
from drover.server.cockpit.service import CockpitService


def rss():
    return (
        int(
            subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True
            ).strip()
        )
        * 1024
    )


def main():
    fixture = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "tests/test_cockpit_analytics.py")
    )
    with tempfile.TemporaryDirectory(prefix="drover-rss-synthetic-") as directory:
        path = Path(directory) / "store.duckdb"
        con = fixture["_analytics_file_connection"](path)
        con.execute("""
            INSERT INTO sessions
            SELECT 'session-' || i, 'agent', NULL, now(), now(),
                   'example', 'project-' || (i % 20), 'main', '{}'
            FROM range(50000) t(i);
            INSERT INTO spans_enriched
            SELECT 'span-' || i, 'session-' || i, now(), now(), 1000,
                   'claude-code', 'anthropic', 'synthetic', 'synthetic',
                   'example', 'project-' || (i % 20), 100, 80, 20, 0.01, 10, 0
            FROM range(50000) t(i);
        """)
        con.close()
        service = CockpitService(
            duckdb_path=path,
            provider_usage=None,
            advisory_repository=SimpleNamespace(list_findings=lambda: []),
        )
        samples = [rss()]
        for index in range(10):
            result = service.overview(AnalyticsFilters(days=7 + index))
            assert result["activity"]["status"] == "ok", result["activity"]
            samples.append(rss())
        print(
            json.dumps(
                {
                    "synthetic": True,
                    "rows": 50000,
                    "requests": 10,
                    "parent_rss_bytes": samples,
                    "isolated_reader": service._isolated_reader_supported,
                }
            )
        )


if __name__ == "__main__":
    main()
