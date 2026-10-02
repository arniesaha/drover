"""Shared PostgreSQL provisioning for the memory-store integration tests.

``DROVER_TEST_POSTGRES_DSN`` wins when it is set (CI runs a postgres service).
Without it, ``postgres_dsn`` starts one disposable cluster per test session from
a local PostgreSQL install (``initdb``/``pg_ctl`` on PATH, under
``DROVER_TEST_PG_BINDIR``, or Homebrew's ``postgresql@17``), listening only on a
private Unix socket. Nothing here touches a cluster it did not create, and every
test schema is dropped afterwards. With neither available the dependent tests
skip, exactly like the existing PostgreSQL contracts.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

_HOMEBREW_BINDIRS = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
)


def _postgres_bindir() -> Path | None:
    explicit = os.environ.get("DROVER_TEST_PG_BINDIR")
    candidates = [explicit] if explicit else []
    found = shutil.which("initdb")
    if found:
        candidates.append(str(Path(found).parent))
    candidates.extend(_HOMEBREW_BINDIRS)
    for candidate in candidates:
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def postgres_dsn():
    """A DSN for a PostgreSQL server the test session may create schemas in."""
    dsn = os.environ.get("DROVER_TEST_POSTGRES_DSN")
    if dsn:
        yield dsn
        return
    bindir = _postgres_bindir()
    if bindir is None:
        pytest.skip("no DROVER_TEST_POSTGRES_DSN and no local initdb")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is not installed (drover[postgres])")
    # Unix socket paths are limited to ~104 bytes on macOS: keep it short.
    root = Path(tempfile.mkdtemp(prefix="drvpg", dir="/tmp"))
    data = root / "data"
    port = _free_port()
    # macOS postmaster refuses to start "multithreaded" without a valid locale.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        subprocess.run(
            [str(bindir / "initdb"), "-D", str(data), "-A", "trust", "-U",
             getpass.getuser(), "--no-sync", "-E", "UTF8"],
            check=True, capture_output=True, timeout=120, env=env,
        )
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-t", "60", "-l",
             str(root / "log"), "-o",
             f"-k {root} -p {port} -c listen_addresses='' -c fsync=off",
             "start"],
            check=True, capture_output=True, timeout=120, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log = root / "log"
        tail = log.read_text(errors="replace")[-600:] if log.exists() else ""
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"could not start a disposable PostgreSQL cluster: {exc} {tail}")
    try:
        yield f"host={root} port={port} dbname=postgres user={getpass.getuser()}"
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True, timeout=60, env=env,
        )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pg_control_path(postgres_dsn, tmp_path: Path, monkeypatch):
    """A bootstrapped, disposable control-store schema registered for a path.

    Yields the registration path (the file itself is never created): every
    production entry point resolves the PostgreSQL store from it.
    """
    from drover.config import ControlStoreConfig
    from drover.schema import bootstrap_control_plane_store
    from drover.server.control_store import close_control_store, configure_control_store

    schema = f"drover_test_{uuid4().hex}"
    monkeypatch.setenv("DROVER_TEST_POSTGRES_DSN", postgres_dsn)
    control_path = tmp_path / "drover.duckdb"
    configure_control_store(
        control_path,
        ControlStoreConfig(
            backend="postgres",
            dsn_env="DROVER_TEST_POSTGRES_DSN",
            pool_min_size=1,
            pool_max_size=4,
            acquire_timeout_seconds=5.0,
            statement_timeout_seconds=10.0,
            schema=schema,
        ),
    )
    bootstrap_control_plane_store(control_path)
    try:
        yield control_path
    finally:
        close_control_store(control_path)
        import psycopg

        with psycopg.connect(postgres_dsn, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def pgvector_available(postgres_dsn: str) -> bool:
    import psycopg

    with psycopg.connect(postgres_dsn, autocommit=True) as con:
        row = con.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
    return row is not None
