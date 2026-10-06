"""Fail-closed engine loading and stable PostgreSQL-catalog attachment.

Runtime never downloads extensions, updates them, or migrates a catalog. The
installer supplies an independently recorded engine digest for its wheel/ABI.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import _duckdb
import duckdb

DUCKDB_VERSION = "1.5.5"
EXTENSION_HASHES = {
    "osx_arm64": {
        "ducklake": "1b72fc42164dbf21948e5a912cefc383b24071852f9e70209874ebd0a73b7caa",
        "postgres_scanner": "f39dd59bf57679d0242e19e180d90b4222e5cdb691ab67c7506658f3548c0e05",
    },
    "linux_amd64": {
        "ducklake": "e51bf9e8d933d0e83780ae096455501b542cf962569a2ce5613532d702c08302",
        "postgres_scanner": "b1ced4cfc6311313e117c2afb3eac76508718778dde0716421503c7dbfb5605c",
    },
}


class LakeError(RuntimeError):
    """Explicit analytics failure, safe to expose by code (never by raw DSN)."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        # Sanitized, bounded cause for operator tooling; never part of the code.
        self.detail = detail
        super().__init__(code)


DETAIL_LIMIT = 300
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_CONNINFO_RE = re.compile(
    r"\b(password|passfile|sslpassword|sslkey|sslcert|sslrootcert|service|"
    r"user|host|hostaddr|port|dbname|options)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)",
    re.IGNORECASE,
)


def sanitize_detail(exc: BaseException, *secrets: str | None) -> str:
    """Exception class plus first message line, scrubbed of DSN material, <=300 chars."""
    line = (str(exc).strip().splitlines() or [""])[0]
    values = {s for s in secrets if s}
    for dsn in list(values):
        try:
            import psycopg.conninfo

            info = psycopg.conninfo.conninfo_to_dict(dsn)
            values.update(
                str(info[k]) for k in ("password", "sslpassword") if info.get(k)
            )
        except Exception:
            pass
    for value in sorted(values, key=len, reverse=True):
        if len(value) >= 3:
            line = line.replace(value, "***")
    line = _URL_RE.sub("***", line)
    line = _CONNINFO_RE.sub(lambda m: f"{m.group(1)}=***", line)
    text = f"{type(exc).__name__}: {line}" if line else type(exc).__name__
    return text[:DETAIL_LIMIT]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class LakeSpec:
    catalog_dsn_env: str
    data_root: Path
    extension_dir: Path
    engine_sha256: str

    def dsn(self) -> str:
        import psycopg.conninfo

        dsn = os.environ.get(self.catalog_dsn_env, "")
        if not dsn:
            raise LakeError("lake_catalog_dsn_missing")
        try:
            info = psycopg.conninfo.conninfo_to_dict(dsn)
        except Exception:
            raise LakeError("lake_catalog_dsn_invalid") from None
        if not info.get("dbname") or info["dbname"] in {
            "postgres",
            "template0",
            "template1",
        }:
            raise LakeError("lake_requires_separate_catalog_database")
        return dsn


def verify_runtime(spec: LakeSpec) -> dict[str, str]:
    if duckdb.__version__ != DUCKDB_VERSION:
        raise LakeError("lake_engine_version_mismatch")
    if sha256_file(Path(_duckdb.__file__)) != spec.engine_sha256:
        raise LakeError("lake_engine_hash_mismatch")
    # Checking platform does not attach or execute any user SQL.
    with duckdb.connect(config={"autoload_known_extensions": False}) as probe:
        platform = probe.execute("PRAGMA platform").fetchone()[0]
    if platform not in EXTENSION_HASHES:
        raise LakeError("lake_platform_unpinned")
    hashes = EXTENSION_HASHES[platform]
    for name, expected in hashes.items():
        path = spec.extension_dir / f"{name}.duckdb_extension"
        if not path.is_file() or sha256_file(path) != expected:
            raise LakeError("lake_extension_hash_mismatch")
    return hashes


# Analytical query children are killed above QueryLimits.rss_bytes (2 GiB).
# Their engine budget must sit well inside that: memory_limit bounds only the
# buffer manager, and the process also carries Python, the extensions and
# expression scratch. One thread halves per-thread operator state; sorts and
# windows spill to the child's own scratch directory instead of growing RSS.
QUERY_CHILD_SETTINGS = {
    "memory_limit": "768MB",
    "threads": 1,
    "preserve_insertion_order": False,
    "max_temp_directory_size": "4GB",
}


@contextmanager
def lake_connection(
    spec: LakeSpec,
    *,
    read_only: bool = True,
    create: bool = False,
    settings: dict | None = None,
):
    """A bounded instance; heavy readers must invoke this only in a child process."""
    if read_only and create:
        raise ValueError("a reader cannot initialize a catalog")
    verify_runtime(spec)
    con = duckdb.connect(
        config={
            "memory_limit": "2GB",
            "threads": 2,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            **(settings or {}),
        }
    )
    try:
        attach_lake(con, spec, read_only=read_only, create=create)
        con.execute("USE lake")
        yield con
    finally:
        con.close()


def attach_lake(con, spec: LakeSpec, *, read_only: bool = True, create: bool = False):
    """Attach to a caller-owned isolated admin engine without changing its default DB."""
    if read_only and create:
        raise ValueError("a reader cannot initialize a catalog")
    verify_runtime(spec)
    for name in ("postgres_scanner", "ducklake"):
        con.execute(
            f"LOAD {literal(str(spec.extension_dir / (name + '.duckdb_extension')))}"
        )
    con.execute("SET ducklake_default_data_inlining_row_limit = 0")
    con.execute("SET TimeZone = 'UTC'")
    options = [
        "DATA_INLINING_ROW_LIMIT 0",
        "AUTOMATIC_MIGRATION false",
        f"CREATE_IF_NOT_EXISTS {'true' if create else 'false'}",
        # Do not let a mistyped root silently redirect production reads.
        "OVERRIDE_DATA_PATH false",
        f"DATA_PATH {literal(str(spec.data_root.resolve()) + '/')}",
    ]
    if read_only:
        options.append("READ_ONLY")
    try:
        con.execute(
            f"ATTACH {literal('ducklake:postgres:' + spec.dsn())} AS lake ({', '.join(options)})"
        )
    except Exception as exc:
        raise LakeError(
            "analytics_unavailable",
            sanitize_detail(exc, os.environ.get(spec.catalog_dsn_env, "")),
        ) from None


def create_table(
    con, name: str, columns: dict[str, str], *, day_partition: bool = False
):
    """Create a stable table once; never replace or implicitly evolve a schema."""
    import re

    allowed = {"VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN", "TIMESTAMPTZ"}
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
        raise ValueError("invalid table name")
    if not columns or any(
        not re.fullmatch(r"[a-z_][a-z_0-9]*", c) or t not in allowed
        for c, t in columns.items()
    ):
        raise ValueError("invalid declared schema")
    if day_partition and columns.get("date") != "VARCHAR":
        raise ValueError("partition date must preserve the UTC day string")
    ddl = ", ".join(f'"{c}" {t}' for c, t in columns.items())
    con.execute(f'CREATE TABLE lake."{name}" ({ddl})')
    for option, value in (
        ("data_inlining_row_limit", "0"),
        ("parquet_compression", "zstd"),
    ):
        con.execute(
            f"CALL lake.set_option({literal(option)}, {literal(value)}, table_name => {literal(name)})"
        )
    if day_partition:
        con.execute(f'ALTER TABLE lake."{name}" SET PARTITIONED BY (date)')


def configure_catalog(con):
    con.execute("CALL lake.set_option('data_inlining_row_limit', 0)")
    con.execute("CALL lake.set_option('parquet_compression', 'zstd')")


def catalog_permission_preflight(spec: LakeSpec, schema: str = "public") -> None:
    """Fail fast, before any expensive build, when the catalog role cannot create.

    DuckLake creates its metadata tables in the target schema; a role without
    CONNECT or CREATE there otherwise surfaces only after the partition phase.
    """
    import psycopg

    dsn = spec.dsn()
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
            row = con.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'CONNECT'),"
                " to_regnamespace(%s) IS NOT NULL,"
                " CASE WHEN to_regnamespace(%s) IS NULL THEN false"
                " ELSE has_schema_privilege(current_user, %s, 'CREATE') END",
                [schema, schema, schema],
            ).fetchone()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise LakeError(
            "lake_catalog_permission_denied", sanitize_detail(exc, dsn)
        ) from None
    except Exception as exc:
        raise LakeError("analytics_unavailable", sanitize_detail(exc, dsn)) from None
    connect, exists, create = row
    if not connect:
        raise LakeError(
            "lake_catalog_permission_denied", "role lacks CONNECT on catalog database"
        )
    if not exists:
        raise LakeError(
            "lake_catalog_permission_denied", f"catalog schema {schema} does not exist"
        )
    if not create:
        raise LakeError(
            "lake_catalog_permission_denied", f"role lacks CREATE on schema {schema}"
        )
