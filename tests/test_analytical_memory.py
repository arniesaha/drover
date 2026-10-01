from __future__ import annotations

import gc
import logging
import sys
import weakref
from types import SimpleNamespace

import pytest

from drover.server import db, memory


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("DROVER_DUCKDB_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("role", ["worker", "summarizer", "diagnostic"])
def test_legacy_budget_applies_to_every_shared_role(tmp_path, monkeypatch, role):
    monkeypatch.setenv("DROVER_DUCKDB_SUMMARIZER_MEMORY_LIMIT", "64MB")
    with db.open_duckdb_connection(tmp_path / "db", role=role) as con:
        assert con.execute("SELECT current_setting('memory_limit')").fetchone() == (
            "61.0 MiB",
        )


@pytest.mark.parametrize(
    "setting,first,second", [("MEMORY_LIMIT", "64MB", "128MB"), ("THREADS", "1", "2")]
)
def test_conflicting_instance_budgets_fail_before_changing_live_settings(
    tmp_path, monkeypatch, setting, first, second
):
    path = tmp_path / "db"
    with db.open_duckdb_connection(path) as con:
        previous = con.execute(
            f"SELECT current_setting('{setting.lower()}')"
        ).fetchone()
        monkeypatch.setenv(f"DROVER_DUCKDB_ANALYTICAL_{setting}", first)
        monkeypatch.setenv(f"DROVER_DUCKDB_DIAGNOSTIC_{setting}", second)
        with pytest.raises(ValueError, match="conflicting analytical instance"):
            db.open_duckdb_connection(path, role="worker")
        assert (
            con.execute(f"SELECT current_setting('{setting.lower()}')").fetchone()
            == previous
        )


def test_private_budget_is_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", "64MB")
    monkeypatch.setenv("DROVER_DUCKDB_SNAPSHOT_MEMORY_LIMIT", "128MB")
    with db.open_duckdb_connection(tmp_path / "live") as live:
        with db.open_duckdb_connection(tmp_path / "copy", role="snapshot") as copy:
            assert live.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone() == ("61.0 MiB",)
            assert copy.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone() == ("122.0 MiB",)


def test_per_call_budget_cannot_change_shared_instance(tmp_path):
    with pytest.raises(ValueError, match="instance budget"):
        db.open_duckdb_connection(
            tmp_path / "db", settings_overrides={"memory_limit": "64MB"}
        )


def test_closed_cursor_releases_parent_and_registered_arrow(tmp_path):
    import pyarrow as pa

    parent = db.open_duckdb_connection(tmp_path / "db")
    table = pa.table({"value": [1, 2, 3]})
    table_ref = weakref.ref(table)
    parent.register("arrow_values", table)
    cursor = parent.cursor()
    parent_ref = weakref.ref(parent)
    del parent, table
    assert parent_ref() is not None  # cursor owns the parent until close
    cursor.close()
    gc.collect()
    assert parent_ref() is None
    assert table_ref() is None


def test_diagnostics_are_bounded_and_observational(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DROVER_DUCKDB_MEMORY_DIAGNOSTICS", "1")
    monkeypatch.setattr(memory, "process_rss_bytes", lambda: 123456)
    monkeypatch.setattr(memory, "_MAX_INSTANCES", 2)
    monkeypatch.setattr(memory, "_samples", memory.OrderedDict())
    caplog.set_level(logging.INFO, logger="drover.memory")
    for index in range(3):
        with db.open_duckdb_connection(tmp_path / str(index)) as con:
            memory.log_instance_memory(con, con._key)
            assert con.execute("SELECT 42").fetchone() == (42,)
    assert len(memory._samples) == 2
    assert len(caplog.records) == 3  # close doesn't sample again
    assert "rss_bytes=123456 duckdb_memory_bytes=" in caplog.text
    assert "duckdb_spill_bytes=" in caplog.text


def test_arrow_release_is_throttled(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "_last_release", float("-inf"))
    monkeypatch.setattr(memory.time, "monotonic", lambda: 10)
    monkeypatch.setitem(
        sys.modules,
        "pyarrow",
        SimpleNamespace(
            default_memory_pool=lambda: SimpleNamespace(
                release_unused=lambda: calls.append(True)
            )
        ),
    )
    memory.release_idle_arrow_memory()
    memory.release_idle_arrow_memory()
    assert calls == [True]


def test_rss_is_current_positive_bytes():
    assert memory.process_rss_bytes() > 0


def test_arrow_release_keeps_referenced_buffer_intact(monkeypatch):
    import pyarrow as pa

    monkeypatch.setattr(memory, "_last_release", float("-inf"))
    buffer = pa.allocate_buffer(1024)
    view = memoryview(buffer).cast("B")
    view[:] = b"x" * 1024
    memory.release_idle_arrow_memory()
    assert buffer.to_pybytes() == b"x" * 1024


def test_reader_timeout_removes_private_directory(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from drover.server.cockpit import service as service_module
    from drover.server.cockpit.analytics import AnalyticsFilters

    observed = []

    def timeout(command, **kwargs):
        import json

        snapshot = Path(json.loads(kwargs["input"])["snapshot"])
        snapshot.write_bytes(b"partial copy")
        observed.append(snapshot)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(service_module.subprocess, "run", timeout)
    service = service_module.CockpitService(
        duckdb_path=tmp_path / "store", provider_usage=None
    )
    with pytest.raises(TimeoutError, match="budget"):
        service._activity_in_reader_process(AnalyticsFilters(days=7))
    assert observed
    assert not observed[0].parent.exists()
