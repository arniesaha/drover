from __future__ import annotations

import gc
import logging
import re
import sys
import weakref
from types import SimpleNamespace

import pytest

from drover.server import db, memory


def _duckdb_bytes(value):
    match = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", value)
    assert match, f"unexpected DuckDB memory limit: {value!r}"
    units = {
        "b": 1,
        "kb": 1_000,
        "kib": 1_024,
        "mb": 1_000_000,
        "mib": 1_048_576,
        "gb": 1_000_000_000,
        "gib": 1_073_741_824,
    }
    return float(match.group(1)) * units[match.group(2).lower()]


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


def test_default_analytical_memory_limit_is_about_4gb(tmp_path):
    with db.open_duckdb_connection(tmp_path / "db") as con:
        limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert _duckdb_bytes(limit) == pytest.approx(4_000_000_000, rel=0.03)


def test_recovery_reopen_honors_analytical_memory_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", "1536MB")
    con = db._open_analytical_handle(tmp_path / "db", "worker", None)
    try:
        limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    finally:
        con.close()
    assert _duckdb_bytes(limit) == pytest.approx(1_536_000_000, rel=0.03)


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
    monkeypatch.setattr(memory, "_sample_times", memory.deque())
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
    assert "duckdb_tags=" in caplog.text
    assert "arrow_pool_bytes=" in caplog.text
    assert "python_traced_bytes=" in caplog.text
    assert str(tmp_path) not in caplog.text


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


@pytest.mark.parametrize("role", ["worker", "summarizer", "diagnostic", "snapshot"])
def test_parquet_scans_do_not_retain_external_file_cache(tmp_path, role):
    import pyarrow as pa
    import pyarrow.parquet as pq

    parquet = tmp_path / "rows.parquet"
    pq.write_table(pa.table({"value": list(range(10000))}), parquet)
    path = tmp_path / "db"
    with db.open_duckdb_connection(path, role=role) as con:
        assert con.execute(
            "SELECT current_setting('enable_external_file_cache')"
        ).fetchone() == (False,)
        # View binding and repeated reads preserve results without keeping
        # Parquet contents in the pinned DuckDB instance between requests.
        con.execute(
            f"CREATE VIEW rows AS SELECT * FROM read_parquet({db.sql_path_literal(parquet)})"
        )
        for _ in range(2):
            assert con.execute("SELECT sum(value) FROM rows").fetchone() == (49995000,)
        assert con.execute(
            "SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory() "
            "WHERE tag = 'EXTERNAL_FILE_CACHE'"
        ).fetchone() == (0,)
        with db.open_duckdb_connection(path, role=role) as second:
            assert second.execute("SELECT count(*) FROM rows").fetchone() == (10000,)
            assert second.execute(
                "SELECT current_setting('enable_external_file_cache')"
            ).fetchone() == (False,)


def test_diagnostics_are_opt_in(monkeypatch):
    def forbidden(*args):
        pytest.fail("diagnostics touched an idle connection without opt-in")

    memory.log_instance_memory(SimpleNamespace(execute=forbidden), "secret-path")


def test_diagnostics_limit_churning_paths(monkeypatch, caplog):
    monkeypatch.setenv("DROVER_DUCKDB_MEMORY_DIAGNOSTICS", "1")
    monkeypatch.setattr(memory, "process_rss_bytes", lambda: 1)
    monkeypatch.setattr(memory, "_samples", memory.OrderedDict())
    monkeypatch.setattr(memory, "_sample_times", memory.deque())
    monkeypatch.setattr(memory, "_MAX_SAMPLES_PER_MINUTE", 2)
    now = [10.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: now[0])
    con = SimpleNamespace(execute=lambda _: SimpleNamespace(fetchall=lambda: []))
    caplog.set_level(logging.INFO, logger="drover.memory")
    for index in range(10):
        memory.log_instance_memory(con, f"secret-{index}")
    assert len(caplog.records) == 2
    assert len(memory._sample_times) == 2
    assert "secret" not in caplog.text
    now[0] += 60
    memory.log_instance_memory(con, "secret-10")
    assert len(caplog.records) == 3


def test_diagnostic_failure_is_non_secret_and_harmless(monkeypatch, caplog):
    monkeypatch.setenv("DROVER_DUCKDB_MEMORY_DIAGNOSTICS", "1")
    monkeypatch.setattr(memory, "_samples", memory.OrderedDict())
    monkeypatch.setattr(memory, "_sample_times", memory.deque())

    def fail(_):
        raise RuntimeError("secret-query-and-path")

    caplog.set_level(logging.DEBUG, logger="drover.memory")
    memory.log_instance_memory(SimpleNamespace(execute=fail), "secret-db")
    assert "memory sample unavailable" in caplog.text
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_diagnostics_observe_loaded_pools_without_starting_tracing(monkeypatch, caplog):
    monkeypatch.setenv("DROVER_DUCKDB_MEMORY_DIAGNOSTICS", "1")
    monkeypatch.setattr(memory, "process_rss_bytes", lambda: 1)
    monkeypatch.setattr(memory, "_samples", memory.OrderedDict())
    monkeypatch.setattr(memory, "_sample_times", memory.deque())
    monkeypatch.setitem(
        sys.modules, "pyarrow", SimpleNamespace(total_allocated_bytes=lambda: 7)
    )
    monkeypatch.setitem(
        sys.modules,
        "tracemalloc",
        SimpleNamespace(is_tracing=lambda: True, get_traced_memory=lambda: (11, 12)),
    )
    con = SimpleNamespace(
        execute=lambda _: SimpleNamespace(
            fetchall=lambda: [("EXTERNAL_FILE_CACHE", 0, 0), ("BASE_TABLE", 13, 17)]
        )
    )
    caplog.set_level(logging.INFO, logger="drover.memory")
    memory.log_instance_memory(con, "secret-db")
    assert "arrow_pool_bytes=7 python_traced_bytes=11" in caplog.text
    assert 'duckdb_tags={"BASE_TABLE":13}' in caplog.text
    assert "duckdb_memory_bytes=13 duckdb_spill_bytes=17" in caplog.text


def test_control_plane_cache_configuration_is_unchanged():
    import duckdb

    with duckdb.connect() as con:
        previous = con.execute(
            "SELECT current_setting('enable_external_file_cache')"
        ).fetchone()
        db._apply_role_settings(con, "control_plane")
        assert (
            con.execute(
                "SELECT current_setting('enable_external_file_cache')"
            ).fetchone()
            == previous
        )


def test_reader_diagnostics_exclude_unrelated_and_malformed_stderr():
    line = (
        "INFO:drover.memory:analytical_memory pid=42 instance=0123456789abcdef "
        "rss_bytes=123 duckdb_memory_bytes=13 duckdb_spill_bytes=0 phase=before_close "
        'arrow_pool_bytes=0 python_traced_bytes=None duckdb_tags={"BASE_TABLE":13}'
    )
    stderr = (
        "private-error-query-and-path\n"
        + line
        + "\n"
        + line.replace("0123456789abcdef", "/private/path")
    )
    assert memory.reader_memory_samples(stderr) == [
        line.removeprefix("INFO:drover.memory:")
    ]
    assert len(memory.reader_memory_samples((line + "\n") * 1000)) == 64
