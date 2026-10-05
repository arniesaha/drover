"""Acceptance test fixtures for Drover DuckLake v2.

Provides:
- prod_shaped: PostgreSQL control-store fixture restored from committed production dump.
- small_lake: Small deterministic synthetic lakehouse (~50k events).
- scale_5m_lake: Full 5M event lakehouse (only used for scale benchmarks).
- served_small_lake / served_scale_lake: those lakes rebuilt into verified DuckLakes
  (production rebuild path) and hub_*_lake: selected on the prod_shaped control store.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from drover.config import ControlStoreConfig
from drover.server.control_store import close_control_store, configure_control_store

_ACCEPTANCE_DIR = Path(__file__).parent
if str(_ACCEPTANCE_DIR) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE_DIR))

from lake_generator import get_cached_lake

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROD_SCHEMA_DUMP = FIXTURES_DIR / "prod_control_schema.sql"
PROD_ROWS_DUMP = FIXTURES_DIR / "prod_control_migration_rows.sql"


def is_pgvector_supported(dsn: str) -> bool:
    """Check if the vector extension is available in this PostgreSQL instance."""
    try:
        with psycopg.connect(dsn, autocommit=True) as con:
            row = con.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
            return bool(row)
    except Exception:
        return False


def restore_prod_control_schema(
    dsn: str,
    target_schema: str = "drover_control",
) -> None:
    """Restore PostgreSQL control schema from the committed production dump.

    Must NEVER run migrations or bootstrap a fresh schema.
    """
    has_vector = is_pgvector_supported(dsn)
    with psycopg.connect(dsn, autocommit=True) as con:
        if has_vector:
            con.execute("CREATE EXTENSION IF NOT EXISTS vector")

        con.execute(f'DROP SCHEMA IF EXISTS "{target_schema}" CASCADE')

        schema_sql = PROD_SCHEMA_DUMP.read_text(encoding="utf-8")
        if target_schema != "drover_control":
            schema_sql = schema_sql.replace("drover_control", target_schema)

        if not has_vector:
            # If pgvector extension is not present on this PostgreSQL instance,
            # adapt the session_embeddings vector type so DDL succeeds.
            schema_sql = schema_sql.replace("public.vector(768)", "text")

        con.execute(schema_sql)

        # Restore migration and initialization rows
        rows_sql = PROD_ROWS_DUMP.read_text(encoding="utf-8")
        if target_schema != "drover_control":
            rows_sql = rows_sql.replace("drover_control", target_schema)

        lines = rows_sql.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("COPY "):
                copy_stmt = line
                copy_data = []
                i += 1
                while i < len(lines) and lines[i] != "\\.":
                    copy_data.append(lines[i])
                    i += 1
                payload = "\n".join(copy_data) + "\n"
                with con.cursor() as cur:
                    with cur.copy(copy_stmt) as copy:
                        copy.write(payload)
            else:
                if line.strip() and not line.startswith("--"):
                    con.execute(line)
            i += 1


@pytest.fixture
def prod_shaped(postgres_dsn, tmp_path: Path, monkeypatch):
    """Restore a Postgres drover_control schema from committed production dumps.

    Guaranteed never to be a freshly migrated store. Yields a control_path Path
    registered with the restored PostgreSQL drover_control store.
    """
    schema = "drover_control"
    restore_prod_control_schema(postgres_dsn, target_schema=schema)

    dsn_env = f"DROVER_TEST_PG_{uuid4().hex[:8].upper()}"
    monkeypatch.setenv(dsn_env, postgres_dsn)
    monkeypatch.setenv("DROVER_TEST_POSTGRES_DSN", postgres_dsn)

    control_path = tmp_path / "prod_shaped_control.duckdb"
    config = ControlStoreConfig(
        backend="postgres",
        dsn_env=dsn_env,
        pool_min_size=1,
        pool_max_size=4,
        acquire_timeout_seconds=5.0,
        statement_timeout_seconds=10.0,
        schema=schema,
    )
    configure_control_store(control_path, config)
    try:
        yield control_path
    finally:
        close_control_store(control_path)
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture(scope="session")
def acceptance_cache_dir() -> Path:
    env_dir = os.environ.get("DROVER_ACCEPTANCE_CACHE")
    if env_dir:
        path = Path(env_dir).expanduser().resolve()
    else:
        path = Path(".pytest_cache") / "acceptance_lake_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def small_lake(acceptance_cache_dir: Path) -> Path:
    """Deterministic synthetic lakehouse (~50k events) with cache."""
    return get_cached_lake(scale="small", seed=42, cache_dir=acceptance_cache_dir)


@pytest.fixture(scope="session")
def scale_5m_lake(acceptance_cache_dir: Path) -> Path:
    """Full 5M-event synthetic lakehouse (used only on high-capacity runner)."""
    return get_cached_lake(scale="5m", seed=42, cache_dir=acceptance_cache_dir)


@pytest.fixture(scope="session")
def lake_extension_dir() -> Path:
    """Require explicitly provisioned artifacts and verify every pinned hash."""
    import duckdb

    from drover.server.lake.runtime import EXTENSION_HASHES, sha256_file

    directory = os.environ.get("DROVER_TEST_LAKE_EXTENSIONS")
    if not directory:
        pytest.fail(
            "Run scripts/fetch-lake-extensions.sh DIR and set "
            "DROVER_TEST_LAKE_EXTENSIONS=DIR before running lake contracts"
        )
    target = Path(directory)
    with duckdb.connect(config={"autoload_known_extensions": False}) as con:
        platform = con.execute("PRAGMA platform").fetchone()[0]
    for name, digest in EXTENSION_HASHES[platform].items():
        if sha256_file(target / f"{name}.duckdb_extension") != digest:
            pytest.fail(f"{name}: provisioned artifact does not match the pinned hash")
    return target


def _served(
    request, postgres_dsn, extension_dir, parquet_root, tmp_path_factory, label
):
    from served_lake import build_served_lake, drop_served_lake

    served = build_served_lake(
        parquet_root=parquet_root,
        postgres_dsn=postgres_dsn,
        extension_dir=extension_dir,
        workdir=tmp_path_factory.mktemp(f"served_{label}"),
        label=label,
    )
    request.addfinalizer(lambda: drop_served_lake(postgres_dsn, served))
    return served


@pytest.fixture(scope="session")
def served_small_lake(
    request, postgres_dsn, lake_extension_dir, small_lake, tmp_path_factory
):
    """The small lake rebuilt into a verified DuckLake (built once per run)."""
    return _served(
        request, postgres_dsn, lake_extension_dir, small_lake, tmp_path_factory, "small"
    )


@pytest.fixture(scope="session")
def served_scale_lake(
    request, postgres_dsn, lake_extension_dir, scale_5m_lake, tmp_path_factory
):
    """The 5M lake rebuilt into a verified DuckLake (Mac Studio only)."""
    return _served(
        request, postgres_dsn, lake_extension_dir, scale_5m_lake, tmp_path_factory, "5m"
    )


def _wire_hub(control_path: Path, served) -> None:
    """Register the lake the way ``_load_runtime_config`` does for the hub."""
    from drover.server.lake.serving import configure_analytics

    configure_analytics(control_path, served.config)


@pytest.fixture
def hub_small_lake(prod_shaped: Path, served_small_lake):
    """prod_shaped control store + small DuckLake selected, as on the hub."""
    _wire_hub(prod_shaped, served_small_lake)
    return served_small_lake


@pytest.fixture
def hub_scale_lake(prod_shaped: Path, served_scale_lake):
    """prod_shaped control store + 5M DuckLake selected, as on the hub."""
    _wire_hub(prod_shaped, served_scale_lake)
    return served_scale_lake


@pytest.fixture(scope="session")
def served_recent_lake(
    request, postgres_dsn, lake_extension_dir, small_lake, tmp_path_factory
):
    """Only the last date is imported; first-day sessions remain in legacy archive.

    S4 will persist an import_watermark; until then the partition boundary is
    the explicit harness watermark, not a fabricated product table.
    """
    import shutil

    root = tmp_path_factory.mktemp("recent_legacy")
    last = sorted([p for p in (small_lake / "agent_events").glob("date=*") if p.name != "date=_seed"])[-1]
    shutil.copytree(last, root / "agent_events" / last.name)
    return _served(
        request, postgres_dsn, lake_extension_dir, root, tmp_path_factory, "recent"
    )


@pytest.fixture
def hub_recent_lake(prod_shaped, served_recent_lake):
    _wire_hub(prod_shaped, served_recent_lake)
    return served_recent_lake
