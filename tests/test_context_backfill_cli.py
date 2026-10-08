"""Backfill requires explicit apply; fixtures never address production stores."""

import json
from dataclasses import replace
from datetime import datetime, timezone

from click.testing import CliRunner
from memory_helpers import put_summary

from drover.config import default_config
from drover.schema import bootstrap
from drover.server import __main__ as cli
from drover.server.db import open_duckdb_connection


def config(monkeypatch, path):
    cfg = replace(default_config(), duckdb_path=path)
    monkeypatch.setattr(cli, "_resolve_config", lambda *a, **kw: cfg)
    return cfg


def count(path):
    with open_duckdb_connection(path) as con:
        return con.execute("SELECT COUNT(*) FROM context_containers").fetchone()[0]


def test_cli_default_dry_run_apply_and_idempotence(
    pg_control_path, tmp_path, monkeypatch
):
    path = pg_control_path
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    put_summary(
        path,
        "s",
        summary_md="Continue research",
        next_steps_md="Review evidence",
        generated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    cfg = config(monkeypatch, path)
    assert cfg.context_containers_enabled is False
    runner = CliRunner()
    args = ["context", "backfill-containers"]
    result = runner.invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == "dry-run"
    assert json.loads(result.output)["created"] == 1
    assert count(path) == 0
    result = runner.invoke(cli.main, [*args, "--apply"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["applied"] == 1
    assert count(path) == 1
    result = runner.invoke(cli.main, [*args, "--apply"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["unchanged"] == 1
    assert json.loads(result.output)["applied"] == 0
    assert count(path) == 1
    result = runner.invoke(cli.main, [*args, "--dry-run"])
    assert result.exit_code == 0
    assert json.loads(result.output)["applied"] == 0


def test_cli_refuses_local_store_without_creating_file(tmp_path, monkeypatch):
    path = tmp_path / "local.duckdb"
    config(monkeypatch, path)
    result = CliRunner().invoke(cli.main, ["context", "backfill-containers", "--apply"])
    assert result.exit_code == 1
    assert "requires the hub PostgreSQL store" in result.output
    assert not path.exists()


def test_cli_snapshot_limit_never_applies_partial_data(
    pg_control_path, tmp_path, monkeypatch
):
    path = pg_control_path
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    put_summary(path, "a")
    put_summary(path, "b")
    config(monkeypatch, path)
    result = CliRunner().invoke(
        cli.main, ["context", "backfill-containers", "--apply", "--max-containers", "1"]
    )
    assert result.exit_code == 1
    assert "no partial snapshot written" in result.output
    assert count(path) == 0
