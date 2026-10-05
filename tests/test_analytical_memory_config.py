from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from drover.server.db import (
    open_duckdb_connection,
    startup_analytical_checkpoint,
    validate_duckdb_size,
)


def test_env_validation_fails_loudly_on_garbage(tmp_path, monkeypatch):
    """Garbage DuckDB memory limits fail loudly with ValueError."""
    # Direct validation checks
    assert validate_duckdb_size("4GB") == "4GB"
    assert validate_duckdb_size("512MB") == "512MB"
    assert validate_duckdb_size(" 1.5 GiB ") == "1.5 GiB"

    for invalid in ("garbage", "0GB", "-1GB", "1024", ""):
        with pytest.raises(ValueError):
            validate_duckdb_size(invalid)

    # Garbage in DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT raises on connect
    monkeypatch.setenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", "invalid_limit")
    db_path = tmp_path / "test.duckdb"
    with pytest.raises(ValueError, match="invalid DuckDB size format"):
        with open_duckdb_connection(db_path, role="worker"):
            pass

    # Garbage in DROVER_ANALYTICAL_CHECKPOINT_MEMORY_LIMIT raises on checkpoint
    monkeypatch.delenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", raising=False)
    monkeypatch.setenv("DROVER_ANALYTICAL_CHECKPOINT_MEMORY_LIMIT", "invalid_ckpt")
    with pytest.raises(ValueError, match="invalid DuckDB size format"):
        startup_analytical_checkpoint(db_path)


def test_startup_checkpoint_drains_forced_wal(tmp_path):
    """Startup checkpoint drains a forced WAL leaving data intact."""
    db_path = tmp_path / "analytical.duckdb"
    wal_path = Path(str(db_path) + ".wal")

    # Force a WAL by abruptly terminating a subprocess after writes without closing
    script = f"""import duckdb, os
con = duckdb.connect({repr(str(db_path))})
con.execute("CREATE TABLE metrics (id INT, val TEXT);")
con.execute("INSERT INTO metrics VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma');")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script], check=True)

    assert wal_path.exists()
    assert wal_path.stat().st_size > 0

    # Execute startup analytical checkpoint
    drained = startup_analytical_checkpoint(db_path)
    assert drained is True

    # The WAL should be gone or truncated to 0
    assert not wal_path.exists() or wal_path.stat().st_size == 0

    # Ensure data is intact
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        count = con.execute("SELECT count(*) FROM metrics").fetchone()[0]
        assert count == 3
    finally:
        con.close()


def test_checkpoint_limit_env_is_honoured(tmp_path, monkeypatch):
    """The checkpoint memory limit env override is passed to duckdb.connect."""
    db_path = tmp_path / "analytical.duckdb"
    wal_path = Path(str(db_path) + ".wal")
    wal_path.write_bytes(b"dummy wal data")

    # When env override is provided
    monkeypatch.setenv("DROVER_ANALYTICAL_CHECKPOINT_MEMORY_LIMIT", "8GB")
    with mock.patch("duckdb.connect") as mock_connect:
        mock_con = mock.MagicMock()
        mock_connect.return_value = mock_con

        assert startup_analytical_checkpoint(db_path) is True
        mock_connect.assert_called_once_with(
            str(db_path),
            config={"memory_limit": "8GB", "threads": "1"},
        )
        mock_con.execute.assert_called_once_with("CHECKPOINT")
        mock_con.close.assert_called_once()

    # When no env override is provided, default 4GB is used
    monkeypatch.delenv("DROVER_ANALYTICAL_CHECKPOINT_MEMORY_LIMIT", raising=False)
    monkeypatch.delenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", raising=False)
    with mock.patch("duckdb.connect") as mock_connect:
        mock_con = mock.MagicMock()
        mock_connect.return_value = mock_con

        assert startup_analytical_checkpoint(db_path) is True
        mock_connect.assert_called_once_with(
            str(db_path),
            config={"memory_limit": "4GB", "threads": "1"},
        )
        mock_con.execute.assert_called_once_with("CHECKPOINT")
        mock_con.close.assert_called_once()

    # Without a checkpoint override, use the running analytical instance limit.
    monkeypatch.setenv("DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT", "6GB")
    with mock.patch("duckdb.connect") as mock_connect:
        mock_con = mock.MagicMock()
        mock_connect.return_value = mock_con

        assert startup_analytical_checkpoint(db_path) is True
        mock_connect.assert_called_once_with(
            str(db_path),
            config={"memory_limit": "6GB", "threads": "1"},
        )


def test_failing_checkpoint_logs_and_returns_without_raising(tmp_path, caplog):
    """A failing checkpoint logs a warning and returns False without raising."""
    db_path = tmp_path / "analytical.duckdb"
    wal_path = Path(str(db_path) + ".wal")
    wal_path.write_bytes(b"dummy wal data")

    with mock.patch("duckdb.connect") as mock_connect:
        mock_con = mock.MagicMock()
        mock_connect.return_value = mock_con
        mock_con.execute.side_effect = RuntimeError("simulated checkpoint error")

        with caplog.at_level(logging.WARNING, logger="drover.db"):
            result = startup_analytical_checkpoint(db_path)

        assert result is False
        assert any(
            "analytical store startup checkpoint failed" in record.message
            for record in caplog.records
        )


def test_no_wal_is_noop(tmp_path):
    """When no WAL exists, startup checkpoint is a no-op returning False."""
    db_path = tmp_path / "clean.duckdb"
    wal_path = Path(str(db_path) + ".wal")
    assert not wal_path.exists()

    with mock.patch("duckdb.connect") as mock_connect:
        result = startup_analytical_checkpoint(db_path)
        assert result is False
        mock_connect.assert_not_called()
