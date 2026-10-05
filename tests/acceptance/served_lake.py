"""Turn a generated legacy-shape parquet lake into a verified, hub-servable DuckLake.

This is the production path (frozen tar -> ``rebuild`` -> ``verify`` -> selected
``AnalyticsConfig``), not a test double: the acceptance tests then read through
exactly the entry points the hub uses (``open_history``, ``read_model``).
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import _duckdb
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql
from psycopg.conninfo import make_conninfo

from drover.config import AnalyticsConfig
from drover.server.lake.exporter import provision_exporter
from drover.server.lake.rebuild import rebuild, verify
from drover.server.lake.runtime import LakeSpec


@dataclass(frozen=True)
class ServedLake:
    spec: LakeSpec
    config: AnalyticsConfig
    catalog_database: str
    rebuild_seconds: float


def _write_companion_tables(root: Path) -> None:
    """``rebuild`` requires all three relations; the other two stay trivial."""
    for table, row in (
        ("provider_usage_snapshots", {"snapshot_id": "p"}),
        ("control_outbox_batches", {"event_id": "frozen"}),
    ):
        folder = root / table
        folder.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist([row]), folder / "part.parquet")


def build_served_lake(
    *,
    parquet_root: Path,
    postgres_dsn: str,
    extension_dir: Path,
    workdir: Path,
    label: str,
) -> ServedLake:
    """Rebuild ``parquet_root/agent_events`` into a verified lake under ``workdir``."""
    database = f"drover_acc_{label}_{uuid4().hex[:10]}"
    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    env_name = f"DROVER_ACCEPTANCE_CATALOG_{label.upper()}"
    os.environ[env_name] = make_conninfo(postgres_dsn, dbname=database)
    spec = LakeSpec(
        env_name,
        workdir / "lake",
        extension_dir,
        hashlib.sha256(Path(_duckdb.__file__).read_bytes()).hexdigest(),
    )

    try:
        companions = workdir / "companions"
        _write_companion_tables(companions)
        archive = workdir / f"{label}.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(parquet_root / "agent_events", arcname="parquet/agent_events")
            for table in ("provider_usage_snapshots", "control_outbox_batches"):
                tar.add(companions / table, arcname=f"parquet/{table}")

        started = time.monotonic()
        rebuild(archive, spec)
        # Provisioning changes the lake snapshot; certify only after that DDL.
        provision_exporter(spec)
        verify(spec)
        elapsed = time.monotonic() - started
        archive.unlink()

        digest = hashlib.sha256(
            (spec.data_root / "verification/serving-proof.json").read_bytes()
        ).hexdigest()
        config = AnalyticsConfig(
            backend="ducklake",
            data_root=str(spec.data_root),
            extension_dir=str(spec.extension_dir),
            engine_sha256=spec.engine_sha256,
            catalog_dsn_env=spec.catalog_dsn_env,
            epoch="acceptance-epoch",
            verification_sha256=digest,
        )
        return ServedLake(spec, config, database, elapsed)
    except BaseException:
        os.environ.pop(env_name, None)
        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )
        raise


def drop_served_lake(postgres_dsn: str, served: ServedLake) -> None:
    os.environ.pop(served.spec.catalog_dsn_env, None)
    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        con.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(served.catalog_database)
            )
        )
