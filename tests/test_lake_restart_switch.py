"""Restart-only DuckLake selection: no handover state or legacy fallback."""

from dataclasses import replace

import pytest

from drover.config import AnalyticsConfig, default_config, load_config
from drover.server.lake.lifecycle import ExporterLifecycle, selected_exporter
from drover.server.lake.runtime import LakeError
from drover.server.lake.serving import lake_spec, validate_startup_config


def _ducklake_config(tmp_path, **changes):
    values = dict(
        backend="ducklake",
        catalog_dsn_env="DROVER_TEST_CATALOG",
        data_root=str(tmp_path / "fresh-root"),
        extension_dir=str(tmp_path / "extensions"),
        engine_sha256="0" * 64,
        verification_sha256="1" * 64,
    )
    values.update(changes)
    return AnalyticsConfig(**values)


def test_legacy_default_and_exporter_are_unchanged(tmp_path):
    config = default_config()
    assert config.analytics.backend == "legacy"
    assert selected_exporter(replace(config, duckdb_path=tmp_path / "legacy"))


def test_ducklake_selection_always_routes_exporter_and_uses_catalog_dsn(tmp_path):
    config = _ducklake_config(tmp_path)
    assert lake_spec(config, exporter=True).catalog_dsn_env == "DROVER_TEST_CATALOG"
    assert isinstance(
        selected_exporter(replace(default_config(), analytics=config)),
        ExporterLifecycle,
    )


@pytest.mark.parametrize(
    "value, code",
    [(None, "lake_catalog_dsn_missing"), ("not a dsn", "lake_catalog_dsn_invalid")],
)
def test_missing_or_invalid_catalog_config_fails_before_startup(
    tmp_path, monkeypatch, value, code
):
    config = _ducklake_config(tmp_path)
    if value is None:
        monkeypatch.delenv(config.catalog_dsn_env, raising=False)
    else:
        monkeypatch.setenv(config.catalog_dsn_env, value)
    with pytest.raises(LakeError, match=code):
        validate_startup_config(config)


def test_ducklake_requires_postgres_control_store(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[analytics]\n"
        'backend = "ducklake"\n'
        'catalog_dsn_env = "DROVER_TEST_CATALOG"\n'
        f'data_root = "{tmp_path / "fresh-root"}"\n'
        f'extension_dir = "{tmp_path / "extensions"}"\n'
        f'engine_sha256 = "{"0" * 64}"\n'
        f'verification_sha256 = "{"1" * 64}"\n'
    )
    with pytest.raises(ValueError, match="requires control_store.backend=postgres"):
        load_config(path)
