"""Scripted DuckLake v2 production switch: one name, preflight, backup, switch.

Every database, path and environment name is derived from ``--lake NAME`` by
:class:`CutoverPlan`; nothing below hard-codes a target. Preflight observes the
host *as the service sees it*: the launchd plist's environment, the config file
its program arguments name, and catalog roles tested by actually connecting
with the service's DSNs. Switch and rollback are ordered action lists that can
print themselves (``--dry-run``) and are safe to re-run. Nothing here runs at
hub startup.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .runtime import LakeError, sanitize_detail

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,23}")
SERVICE_LABEL = "com.drover.server"
DEFAULT_PLIST = Path("~/Library/LaunchAgents/com.drover.server.plist")
MEMORY_LIMIT_ENV = "DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "4GB"
GATE_DATABASE_PREFIX = "drover_gate_"
PROOF = Path("verification") / "serving-proof.json"

# DuckDB size semantics: KB/MB/GB are decimal, KiB/MiB/GiB binary.
_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
_SIZE_FACTORS = {
    "B": 1,
    "KB": 1000,
    "KIB": 1024,
    "MB": 1000**2,
    "MIB": 1024**2,
    "GB": 1000**3,
    "GIB": 1024**3,
    "TB": 1000**4,
    "TIB": 1024**4,
}
# conninfo keys mapped to libpq variables, so no DSN ever appears on argv.
_LIBPQ_ENV = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
}


class CutoverError(RuntimeError):
    """A stable code plus a sanitized cause; never a DSN."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CutoverPlan:
    """Every name used by gate, preflight, switch and rollback, from one name."""

    name: str
    lake_root: Path
    gate_root: Path
    backup_root: Path

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        lake_root: Path | str,
        gate_root: Path | str | None = None,
        backup_root: Path | str | None = None,
    ) -> "CutoverPlan":
        if not NAME_PATTERN.fullmatch(name or ""):
            raise CutoverError("cutover_lake_name_invalid", "use [a-z][a-z0-9_]{0,23}")
        root = Path(lake_root).expanduser().resolve()
        gate = Path(gate_root) if gate_root else root / ".gate"
        backups = Path(backup_root) if backup_root else root / ".cutover-backups"
        return cls(
            name=name,
            lake_root=root,
            gate_root=gate.expanduser().resolve(),
            backup_root=backups.expanduser().resolve(),
        )

    # Target lake (production after the switch).
    @property
    def data_root(self) -> Path:
        return self.lake_root / self.name

    @property
    def proof_path(self) -> Path:
        return self.data_root / PROOF

    @property
    def catalog_database(self) -> str:
        return f"drover_lake_{self.name}"

    @property
    def catalog_role_prefix(self) -> str:
        return f"drover_lake_{self.name}"

    @property
    def env_prefix(self) -> str:
        return f"DROVER_LAKE_{self.name.upper()}"

    @property
    def reader_dsn_env(self) -> str:
        return f"{self.env_prefix}_READER_DSN"

    @property
    def exporter_dsn_env(self) -> str:
        return f"{self.env_prefix}_EXPORTER_DSN"

    @property
    def admin_dsn_env(self) -> str:
        return f"{self.env_prefix}_ADMIN_DSN"

    # Gate (scratch) resources; never the target's.
    @property
    def gate_dir(self) -> Path:
        return self.gate_root / self.name

    @property
    def verdict_path(self) -> Path:
        return self.gate_root / f"{self.name}.verdict.json"

    @property
    def gate_control_database(self) -> str:
        return f"{GATE_DATABASE_PREFIX}{self.name}_control"

    @property
    def gate_catalog_database(self) -> str:
        return f"{GATE_DATABASE_PREFIX}{self.name}_lake"

    @property
    def gate_control_dsn_env(self) -> str:
        return f"{self.env_prefix}_GATE_CONTROL_DSN"

    @property
    def gate_catalog_dsn_env(self) -> str:
        return f"{self.env_prefix}_GATE_CATALOG_DSN"

    # Cutover bookkeeping.
    @property
    def backup_dir(self) -> Path:
        return self.backup_root / self.name

    @property
    def state_path(self) -> Path:
        return self.backup_dir / "cutover-state.json"

    def describe(self) -> dict[str, str]:
        return {
            "lake": self.name,
            "data_root": str(self.data_root),
            "catalog_database": self.catalog_database,
            "catalog_role_prefix": self.catalog_role_prefix,
            "reader_dsn_env": self.reader_dsn_env,
            "exporter_dsn_env": self.exporter_dsn_env,
            "admin_dsn_env": self.admin_dsn_env,
            "gate_dir": str(self.gate_dir),
            "gate_control_database": self.gate_control_database,
            "gate_catalog_database": self.gate_catalog_database,
            "verdict_path": str(self.verdict_path),
            "backup_dir": str(self.backup_dir),
        }


@dataclass
class Check:
    id: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "passed": self.passed, "evidence": self.evidence}


def run_check(check_id: str, probe: Callable[[], tuple[bool, dict]], *secrets):
    """Run one probe; an exception is a failed check with a sanitized cause."""
    try:
        passed, evidence = probe()
    except LakeError as exc:
        detail = f"{exc.code}: {exc.detail}" if exc.detail else exc.code
        return Check(check_id, False, {"error": detail})
    except CutoverError as exc:
        return Check(check_id, False, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - a probe failure is a failed check
        return Check(check_id, False, {"error": sanitize_detail(exc, *secrets)})
    return Check(check_id, bool(passed), evidence)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verdict(kind: str, plan: CutoverPlan, checks: list[Check], **extra) -> dict:
    """One JSON document; passes only with checks, all passing, and no error."""
    error = extra.get("error")
    return {
        "kind": kind,
        "lake": plan.name,
        "passed": not error and bool(checks) and all(c.passed for c in checks),
        "failed": [c.id for c in checks if not c.passed],
        "checks": [c.as_dict() for c in checks],
        **extra,
        "generated_at": utc_now(),
    }


def exit_code(document: dict) -> int:
    """0 passed, 1 a check failed, 2 the run itself could not complete."""
    if document.get("passed"):
        return 0
    return 2 if document.get("error") else 1


def memory_limit_bytes(value: str) -> int:
    match = _SIZE.match(value or "")
    unit = match.group(2).upper() if match else ""
    if not match or unit not in _SIZE_FACTORS or float(match.group(1)) <= 0:
        raise CutoverError("memory_limit_invalid", repr(value))
    return int(float(match.group(1)) * _SIZE_FACTORS[unit])


def libpq_env(dsn: str, *, read_only: bool = False, base=None) -> dict[str, str]:
    """Child environment addressing ``dsn`` through libpq variables only."""
    from psycopg.conninfo import conninfo_to_dict

    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith("PG")}
    for key, value in conninfo_to_dict(dsn).items():
        name = _LIBPQ_ENV.get(key)
        if name is None:
            raise CutoverError("dsn_parameter_unsupported", key)
        if value is not None:
            env[name] = str(value)
    if read_only:
        env["PGOPTIONS"] = (
            env.get("PGOPTIONS", "") + " -c default_transaction_read_only=on"
        ).strip()
    return env


def database_identity(dsn: str) -> tuple[str, str, str]:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    return (
        str(info.get("host") or info.get("hostaddr") or "localhost"),
        str(info.get("port") or "5432"),
        str(info.get("dbname") or ""),
    )


def pg_tool(name: str, pg_bin: Path | None = None) -> str:
    if pg_bin is not None:
        path = Path(pg_bin) / name
        if not path.is_file():
            raise CutoverError("pg_tool_missing", str(path))
        return str(path)
    found = shutil.which(name)
    if not found:
        raise CutoverError("pg_tool_missing", name)
    return found


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def control_store_risks(con, schema: str) -> dict[str, Any]:
    """Read-only facts that block serving or export on this control store.

    ``identity_sessions`` is what every lake read must carry (see
    ``serving.IDENTITY_LIMIT``); unacknowledged ``lake_export_batches`` rows
    are frozen inputs a new exporter must resume, and a different catalog id
    stops it with ``lake_export_input_mismatch``.
    """
    from psycopg import sql

    from .serving import IDENTITY_LIMIT

    sessions = con.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE command <> 'collector'").format(
            sql.Identifier(schema, "harness_sessions")
        )
    ).fetchone()[0]
    exists = con.execute(
        "SELECT to_regclass(%s) IS NOT NULL",
        [sql.Identifier(schema, "lake_export_batches").as_string(None)],
    ).fetchone()[0]
    pending, catalogs = 0, []
    if exists:
        pending, catalogs = con.execute(
            sql.SQL(
                "SELECT count(*), coalesce(array_agg(DISTINCT catalog_id), "
                "ARRAY[]::text[]) FROM {} WHERE acknowledged_at IS NULL"
            ).format(sql.Identifier(schema, "lake_export_batches"))
        ).fetchone()
    return {
        "identity_sessions": sessions,
        "identity_limit": IDENTITY_LIMIT,
        "lake_export_batches_table": bool(exists),
        "unacknowledged_export_batches": pending,
        "unacknowledged_catalog_ids": [str(c) for c in catalogs or []],
    }


@dataclass(frozen=True)
class ServiceView:
    """The hub service as launchd starts it: label, argv, env and config path."""

    plist_path: Path
    label: str
    program: list[str]
    env: dict[str, str]
    config_path: Path

    @classmethod
    def load(cls, plist_path: Path | str) -> "ServiceView":
        path = Path(plist_path).expanduser()
        with path.open("rb") as stream:
            data = plistlib.load(stream)
        program = [str(a) for a in data.get("ProgramArguments") or []]
        if not program and data.get("Program"):
            program = [str(data["Program"])]
        env = {
            str(k): str(v) for k, v in (data.get("EnvironmentVariables") or {}).items()
        }
        config = None
        for index, arg in enumerate(program):
            if arg == "--config" and index + 1 < len(program):
                config = program[index + 1]
            elif arg.startswith("--config="):
                config = arg.split("=", 1)[1]
        if config is None:
            home = env.get("HOME") or os.path.expanduser("~")
            config = str(Path(home) / ".drover" / "config.toml")
        return cls(path, str(data.get("Label", "")), program, env, Path(config))

    @property
    def home(self) -> Path:
        return Path(self.env.get("HOME") or os.path.expanduser("~"))

    def config(self):
        from drover.config import load_config

        return load_config(self.config_path)

    def version_argv(self) -> list[str]:
        """How to ask the service's own executable for its version."""
        for index, arg in enumerate(self.program):
            if Path(arg).name == "drover-server":
                return [arg, "--version"]
            if arg == "-m" and self.program[index + 1 : index + 2] == ["drover.server"]:
                return [*self.program[:index], "-m", "drover.server", "--version"]
        raise CutoverError("service_program_unrecognized", " ".join(self.program))

    def hub_url(self, cfg) -> str:
        host = cfg.server_metrics_host
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{cfg.metrics_http_port}"


def _ducklake_schema(con) -> str:
    schemas = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT schemaname FROM pg_tables "
            "WHERE tablename = 'ducklake_snapshot' "
            "AND schemaname NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
    ]
    if len(schemas) != 1:
        raise CutoverError("catalog_schema_ambiguous", f"{len(schemas)} found")
    return schemas[0]


def probe_catalog_role(dsn: str, *, kind: str, expected_database: str, connect):
    """Connect as the role and prove what it can and cannot do."""
    from psycopg import sql

    with connect(dsn) as con:
        user, database = con.execute(
            "SELECT current_user, current_database()"
        ).fetchone()
        schema = _ducklake_schema(con)
        table = sql.Identifier(schema, "ducklake_snapshot").as_string(None)
        can_select, can_insert, can_create = con.execute(
            "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
            "has_table_privilege(current_user, %s, 'INSERT'), "
            "has_database_privilege(current_user, current_database(), 'CREATE')",
            [table, table],
        ).fetchone()
        snapshots = con.execute(
            sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier(schema, "ducklake_snapshot")
            )
        ).fetchone()[0]
    expected_insert = kind == "exporter"
    passed = (
        database == expected_database
        and bool(can_select)
        and bool(can_insert) == expected_insert
        and not can_create
    )
    return passed, {
        "role": user,
        "database": database,
        "expected_database": expected_database,
        "catalog_schema": schema,
        "select": bool(can_select),
        "insert": bool(can_insert),
        "insert_expected": expected_insert,
        "create_database": bool(can_create),
        "snapshots": snapshots,
    }


def total_ram_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return int(result.stdout.strip())


def load_gate_verdict(plan: CutoverPlan) -> dict | None:
    try:
        return json.loads(plan.verdict_path.read_text())
    except (OSError, ValueError):
        return None


def gate_verdict_fresh(document: dict | None, *, max_age_hours: float) -> bool:
    if not document or not document.get("passed") or document.get("kind") != "gate":
        return False
    try:
        generated = datetime.fromisoformat(document["generated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age = datetime.now(timezone.utc) - generated
    return 0 <= age.total_seconds() <= max_age_hours * 3600


def run_preflight(
    plan: CutoverPlan,
    service: ServiceView,
    *,
    stage: str,
    release: str,
    min_free_bytes: int,
    min_ram_bytes: int,
    max_verdict_age_hours: float = 24,
    connect: Callable = _connect,
    run: Callable = subprocess.run,
    ram: Callable[[], int] = total_ram_bytes,
    environ=None,
) -> list[Check]:
    """Every prerequisite, read-only, in the service's own view of the host."""
    from drover.server.runtime import MARKER_FILENAME, RuntimeLayout

    if stage not in {"gate", "switch"}:
        raise CutoverError("preflight_stage_invalid", stage)
    environ = os.environ if environ is None else environ
    want = release.lstrip("v")
    secrets = list(service.env.values())
    holder: dict[str, Any] = {}
    checks: list[Check] = []

    def service_config():
        cfg = service.config()
        holder["cfg"] = cfg
        passed = (
            service.label == SERVICE_LABEL and cfg.control_store.backend == "postgres"
        )
        return passed, {
            "plist": str(service.plist_path),
            "label": service.label,
            "config": str(service.config_path),
            "control_store_backend": cfg.control_store.backend,
            "control_schema": cfg.control_store.schema,
            "analytics_backend": cfg.analytics.backend,
            "hub_url": service.hub_url(cfg),
        }

    checks.append(run_check("service_config", service_config, *secrets))
    cfg = holder.get("cfg")

    def service_env():
        from psycopg.conninfo import conninfo_to_dict

        required = [plan.reader_dsn_env, plan.exporter_dsn_env]
        if cfg is not None:
            required.append(cfg.control_store.dsn_env)
        missing = [name for name in required if not service.env.get(name)]
        databases = {
            name: conninfo_to_dict(service.env[name]).get("dbname")
            for name in (plan.reader_dsn_env, plan.exporter_dsn_env)
            if service.env.get(name)
        }
        wrong = sorted(n for n, d in databases.items() if d != plan.catalog_database)
        limit = service.env.get(MEMORY_LIMIT_ENV, DEFAULT_MEMORY_LIMIT)
        return not missing and not wrong, {
            "missing": missing,
            "catalog_databases": databases,
            "expected_catalog_database": plan.catalog_database,
            "wrong_catalog_database": wrong,
            MEMORY_LIMIT_ENV: limit,
            "memory_limit_explicit": MEMORY_LIMIT_ENV in service.env,
            "analytical_budget_bytes": memory_limit_bytes(limit),
        }

    checks.append(run_check("service_env", service_env, *secrets))

    for kind, env_name in (
        ("reader", plan.reader_dsn_env),
        ("exporter", plan.exporter_dsn_env),
    ):

        def role(kind=kind, env_name=env_name):
            dsn = service.env.get(env_name)
            if not dsn:
                return False, {"error": f"{env_name} is not in the service env"}
            return probe_catalog_role(
                dsn,
                kind=kind,
                expected_database=plan.catalog_database,
                connect=connect,
            )

        checks.append(run_check(f"catalog_role_{kind}", role, *secrets))

    def control_store():
        if cfg is None:
            return False, {"error": "service config unavailable"}
        dsn = service.env.get(cfg.control_store.dsn_env)
        if not dsn:
            return False, {"error": f"{cfg.control_store.dsn_env} not in service env"}
        from psycopg import sql

        with connect(dsn) as con:
            # Every statement below runs in a read-only session.
            con.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            version = con.execute(
                sql.SQL("SELECT max(version) FROM {}").format(
                    sql.Identifier(
                        cfg.control_store.schema, "control_schema_migrations"
                    )
                )
            ).fetchone()[0]
            risks = control_store_risks(con, cfg.control_store.schema)
        passed = (
            version is not None
            and risks["identity_sessions"] <= risks["identity_limit"]
            and risks["unacknowledged_export_batches"] == 0
        )
        return passed, {"migration_version": version, **risks}

    checks.append(run_check("control_store", control_store, *secrets))

    def disk():
        free = shutil.disk_usage(plan.lake_root).free
        return free >= min_free_bytes, {
            "lake_root": str(plan.lake_root),
            "free_bytes": free,
            "min_free_bytes": min_free_bytes,
        }

    checks.append(run_check("lake_root_disk_free", disk))

    def memory():
        total = ram()
        return total >= min_ram_bytes, {
            "total_bytes": total,
            "min_bytes": min_ram_bytes,
        }

    checks.append(run_check("ram", memory))

    def layout():
        root = cfg.update_runtime_root if cfg is not None else None
        return RuntimeLayout(service.home / ".drover", root=root)

    def version():
        result = run(
            service.version_argv(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        reported = (result.stdout or "").strip().rsplit(" ", 1)[-1].lstrip("v")
        active = layout().active_version()
        passed = (
            result.returncode == 0
            and reported == want
            and (active is None or active.lstrip("v") == want)
        )
        return passed, {
            "release": want,
            "service_reports": reported,
            "runtime_current": active,
            "returncode": result.returncode,
        }

    checks.append(run_check("installed_version", version))

    def updater():
        marker = layout().root / MARKER_FILENAME
        enabled = bool(cfg.update_enabled) if cfg is not None else True
        pinned = (cfg.update_pinned_version if cfg is not None else "").lstrip("v")
        frozen = not enabled or pinned == want
        return frozen and not marker.exists(), {
            "update_enabled": enabled,
            "pinned_version": pinned,
            "pending_verification_marker": marker.exists(),
            "why": "harnessd re-flips runtime/current on start while a marker "
            "or an unpinned target exists",
        }

    checks.append(run_check("updater_state", updater))

    def lake_runtime():
        from .runtime import LakeSpec, verify_runtime

        directory = environ.get("DROVER_LAKE_EXTENSION_DIR", "")
        digest = environ.get("DROVER_LAKE_ENGINE_SHA256", "")
        if not directory or not digest:
            return False, {
                "error": "set DROVER_LAKE_EXTENSION_DIR and DROVER_LAKE_ENGINE_SHA256"
            }
        verify_runtime(
            LakeSpec(plan.reader_dsn_env, plan.data_root, Path(directory), digest)
        )
        return True, {"extension_dir": directory, "engine_sha256": digest}

    checks.append(run_check("lake_runtime_pins", lake_runtime))

    def paths():
        evidence: dict[str, Any] = {"stage": stage}
        if stage == "gate":
            gate_dir = plan.gate_dir
            empty = not gate_dir.exists() or not any(gate_dir.iterdir())
            evidence.update(gate_dir=str(gate_dir), gate_dir_empty=empty)
            return empty, evidence
        legacy = cfg.parquet_dir.resolve() if cfg is not None else None
        overlaps = legacy is not None and (
            plan.data_root == legacy or legacy in plan.data_root.parents
        )
        backup_parent = plan.backup_root
        while not backup_parent.exists() and backup_parent != backup_parent.parent:
            backup_parent = backup_parent.parent
        document = load_gate_verdict(plan)
        fresh = gate_verdict_fresh(document, max_age_hours=max_verdict_age_hours)
        evidence.update(
            data_root=str(plan.data_root),
            serving_proof=plan.proof_path.is_file(),
            data_root_inside_legacy_parquet=overlaps,
            backup_root_writable=os.access(backup_parent, os.W_OK),
            gate_verdict=str(plan.verdict_path),
            gate_verdict_passed_and_fresh=fresh,
        )
        passed = (
            plan.proof_path.is_file()
            and not overlaps
            and evidence["backup_root_writable"]
            and fresh
        )
        return passed, evidence

    checks.append(run_check("target_paths", paths))
    return checks


# --- Backup, config flip and the service manager -------------------------


def _sha256(path: Path) -> str:
    from .runtime import sha256_file

    return sha256_file(path)


def take_backup(
    plan: CutoverPlan,
    service: ServiceView,
    cfg,
    *,
    pg_bin: Path | None = None,
    run: Callable = subprocess.run,
    stamp: str | None = None,
) -> dict:
    """Control DB dump plus config/plist copies; verified before returning."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = plan.backup_dir / stamp
    dsn = service.env.get(cfg.control_store.dsn_env)
    if not dsn:
        raise CutoverError("backup_control_dsn_missing", cfg.control_store.dsn_env)
    target.mkdir(parents=True, exist_ok=False)
    dump = target / "control.dump"
    result = run(
        [
            pg_tool("pg_dump", pg_bin),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--schema={cfg.control_store.schema}",
            f"--file={dump}",
        ],
        env=libpq_env(dsn, read_only=True),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CutoverError(
            "backup_dump_failed",
            sanitize_detail(RuntimeError(result.stderr or ""), dsn),
        )
    shutil.copy2(service.config_path, target / "config.toml")
    shutil.copy2(service.plist_path, target / service.plist_path.name)
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in (dump, target / "config.toml", target / service.plist_path.name)
    }
    manifest = {
        "lake": plan.name,
        "created_at": utc_now(),
        "control_schema": cfg.control_store.schema,
        "files": files,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return verify_backup(target, pg_bin=pg_bin, run=run)


def verify_backup(
    target: Path, *, pg_bin: Path | None = None, run: Callable = subprocess.run
) -> dict:
    try:
        manifest = json.loads((target / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise CutoverError("backup_manifest_missing", str(target)) from exc
    for name, meta in manifest["files"].items():
        path = target / name
        if not path.is_file() or path.stat().st_size == 0:
            raise CutoverError("backup_file_missing", name)
        if _sha256(path) != meta["sha256"]:
            raise CutoverError("backup_file_changed", name)
    listing = run(
        [pg_tool("pg_restore", pg_bin), "--list", str(target / "control.dump")],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0 or "TABLE" not in (listing.stdout or ""):
        raise CutoverError("backup_dump_unreadable", str(target / "control.dump"))
    return {"backup": str(target), "verified": True, "files": manifest["files"]}


_HEADER = re.compile(r"^\s*\[(\[)?\s*([^\]]+?)\s*\]")


def replace_table(text: str, table: str, body: list[str]) -> str:
    """Drop every ``[table]`` block and append a freshly rendered one."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        match = _HEADER.match(line)
        if match:
            skipping = match.group(1) is None and match.group(2) == table
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [f"[{table}]", *body]
    return "\n".join([*kept, "", *block] if kept else block) + "\n"


def render_values(values: dict[str, Any]) -> list[str]:
    # JSON strings and booleans are valid TOML basic strings and booleans.
    return [f"{key} = {json.dumps(value)}" for key, value in values.items()]


def flip_config(config_path: Path, analytics: dict[str, Any]) -> bool:
    """Atomically select ``analytics``; False when it is already selected."""
    from dataclasses import replace

    from drover.config import AnalyticsConfig, load_config

    current = load_config(config_path)
    if current.analytics == replace(AnalyticsConfig(), **analytics):
        return False
    text = config_path.read_text()
    staged = config_path.with_name(config_path.name + ".cutover-new")
    staged.write_text(replace_table(text, "analytics", render_values(analytics)))
    try:
        shutil.copymode(config_path, staged)
        load_config(staged)  # Refuse to install a config the hub cannot load.
        os.replace(staged, config_path)
    finally:
        staged.unlink(missing_ok=True)
    return True


def ducklake_selection(
    plan: CutoverPlan, *, extension_dir: str, engine_sha256: str, epoch: str
) -> dict[str, Any]:
    if not plan.proof_path.is_file():
        raise CutoverError("serving_proof_missing", str(plan.proof_path))
    return {
        "backend": "ducklake",
        "catalog_dsn_env": plan.reader_dsn_env,
        "exporter_dsn_env": plan.exporter_dsn_env,
        "data_root": str(plan.data_root),
        "extension_dir": extension_dir,
        "engine_sha256": engine_sha256,
        "verification_sha256": hashlib.sha256(plan.proof_path.read_bytes()).hexdigest(),
        "epoch": epoch,
    }


LEGACY_SELECTION = {"backend": "legacy"}


class Launchd:
    """The hub's launchd job; every call is idempotent."""

    def __init__(self, service: ServiceView, *, run: Callable = subprocess.run):
        self.service = service
        self.run = run
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{service.label}"

    def loaded(self) -> bool:
        result = self.run(
            ["launchctl", "print", self.target], capture_output=True, check=False
        )
        return result.returncode == 0

    def stop_argv(self) -> list[str]:
        return ["launchctl", "bootout", self.target]

    def start_argv(self) -> list[str]:
        return ["launchctl", "bootstrap", self.domain, str(self.service.plist_path)]

    def stop(self) -> dict:
        if not self.loaded():
            return {"skipped": "not loaded"}
        self._call(self.stop_argv())
        deadline = time.monotonic() + 60
        while self.loaded():
            if time.monotonic() > deadline:
                raise CutoverError("service_stop_deadline", self.target)
            time.sleep(1)
        return {"stopped": self.target}

    def start(self) -> dict:
        if self.loaded():
            return {"skipped": "already loaded"}
        self._call(self.start_argv())
        return {"started": self.target}

    def _call(self, argv: list[str]) -> None:
        result = self.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CutoverError(
                "launchctl_failed", f"{' '.join(argv)}: {(result.stderr or '')[:200]}"
            )


def http_get(url: str, *, timeout: float = 10) -> tuple[int, str]:
    from urllib.error import HTTPError
    from urllib.request import urlopen

    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def verify_hub(
    base_url: str,
    *,
    timeout: float = 300,
    fetch: Callable[[str], tuple[int, str]] = http_get,
    interval: float = 2,
) -> dict:
    """Wait for liveness with analytical=ok AND readiness 200."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while True:
        try:
            health_status, health = fetch(base_url + "/healthz")
            ready_status, ready = fetch(base_url + "/readyz")
            last = {
                "healthz_status": health_status,
                "healthz": health.strip(),
                "readyz_status": ready_status,
            }
            if (
                health_status == 200
                and health.strip() == "ok\nanalytical=ok"
                and ready_status == 200
            ):
                try:
                    last["readyz_memory"] = json.loads(ready).get("memory")
                except (ValueError, AttributeError):
                    pass
                return last
        except OSError as exc:
            last = {"error": sanitize_detail(exc)}
        if time.monotonic() > deadline:
            raise CutoverError("hub_not_ready", json.dumps(last)[:300])
        time.sleep(interval)


# --- Ordered, printable procedures ---------------------------------------


@dataclass
class Action:
    step: str
    describe: str
    run: Callable[[], Any]


def execute(actions: list[Action], *, dry_run: bool, echo=print) -> list[dict]:
    """Print every action (dry run) or run them in order, stopping at a failure."""
    results = []
    for number, action in enumerate(actions, 1):
        prefix = f"[{number}/{len(actions)}] {action.step}"
        if dry_run:
            echo(f"{prefix} (dry-run): {action.describe}")
            results.append({"step": action.step, "dry_run": True})
            continue
        echo(f"{prefix}: {action.describe}")
        try:
            outcome = action.run()
        except LakeError as exc:
            raise CutoverError(
                f"{action.step}_failed",
                f"{exc.code}: {exc.detail}" if exc.detail else exc.code,
            ) from None
        except CutoverError as exc:
            raise CutoverError(f"{action.step}_failed", str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - stop the procedure, keep cause
            raise CutoverError(f"{action.step}_failed", sanitize_detail(exc)) from exc
        echo(f"{prefix}: done {json.dumps(outcome, default=str)}")
        results.append({"step": action.step, "result": outcome})
    return results


def _record_state(plan: CutoverPlan, **values) -> dict:
    """Keep the first switch time; later re-runs must not narrow rollback."""
    state = read_state(plan)
    if state.get("lake") not in (None, plan.name):
        raise CutoverError("cutover_state_foreign", str(plan.state_path))
    for key, value in values.items():
        state.setdefault(key, value)
    state["lake"] = plan.name
    plan.state_path.parent.mkdir(parents=True, exist_ok=True)
    plan.state_path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def read_state(plan: CutoverPlan) -> dict:
    try:
        return json.loads(plan.state_path.read_text())
    except (OSError, ValueError):
        return {}


def lake_spec_for(plan: CutoverPlan, dsn_env: str, environ=None):
    from .runtime import LakeSpec

    environ = os.environ if environ is None else environ
    directory = environ.get("DROVER_LAKE_EXTENSION_DIR", "")
    digest = environ.get("DROVER_LAKE_ENGINE_SHA256", "")
    if not directory or not digest:
        raise CutoverError(
            "lake_runtime_env_missing",
            "set DROVER_LAKE_EXTENSION_DIR and DROVER_LAKE_ENGINE_SHA256",
        )
    return LakeSpec(dsn_env, plan.data_root, Path(directory), digest)


def delta_since(spec) -> str:
    """Re-import from the newest day already in the lake; dedupe makes it safe."""
    from .runtime import lake_connection

    with lake_connection(spec) as con:
        newest = con.execute(
            "SELECT max(date) FROM lake.agent_events WHERE date <> '_seed'"
        ).fetchone()[0]
        if newest is None:
            newest = con.execute(
                "SELECT min(partition_date) FROM lake.import_watermark"
            ).fetchone()[0]
    if newest is None:
        raise CutoverError("lake_has_no_import", "run the 60-day import first")
    return str(newest)


def _backup_describe(plan, service, cfg, stamp) -> str:
    return (
        f"pg_dump --format=custom --schema={cfg.control_store.schema} "
        f"(${cfg.control_store.dsn_env}, read-only session) and copy "
        f"{service.config_path} + {service.plist_path} into "
        f"{plan.backup_dir}/{stamp}/; verify sha256 manifest + pg_restore --list"
    )


def _verify_describe(url: str, timeout: float) -> str:
    return (
        f"GET {url}/healthz == 'ok\\nanalytical=ok' and GET {url}/readyz == 200 "
        f"within {timeout:g}s"
    )


def switch_actions(
    plan: CutoverPlan,
    service: ServiceView,
    *,
    legacy_root: Path,
    max_verdict_age_hours: float = 24,
    pg_bin: Path | None = None,
    verify_timeout: float = 300,
    launchd: Launchd | None = None,
    environ=None,
) -> list[Action]:
    """Gate verdict → backup → stop → fenced delta import → flip → start → verify."""
    environ = os.environ if environ is None else environ
    cfg = service.config()
    launchd = launchd or Launchd(service)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    epoch = read_state(plan).get("epoch") or f"{plan.name}-{stamp}"
    url = service.hub_url(cfg)

    def require_gate():
        document = load_gate_verdict(plan)
        if not gate_verdict_fresh(document, max_age_hours=max_verdict_age_hours):
            raise CutoverError("gate_verdict_missing_or_stale", str(plan.verdict_path))
        return {
            "verdict": str(plan.verdict_path),
            "generated_at": document["generated_at"],
        }

    def delta_import():
        from .legacy_import import run_import

        spec = lake_spec_for(plan, plan.admin_dsn_env, environ)
        return run_import(spec, Path(legacy_root), since=delta_since(spec))

    def flip():
        selection = ducklake_selection(
            plan,
            extension_dir=environ.get("DROVER_LAKE_EXTENSION_DIR", ""),
            engine_sha256=environ.get("DROVER_LAKE_ENGINE_SHA256", ""),
            epoch=epoch,
        )
        changed = flip_config(service.config_path, selection)
        state = _record_state(
            plan, switch_epoch=time.time(), epoch=epoch, backup=str(plan.backup_dir)
        )
        return {"changed": changed, "switch_epoch": state["switch_epoch"]}

    return [
        Action(
            "require_gate",
            f"refuse unless {plan.verdict_path} passed within "
            f"{max_verdict_age_hours:g}h",
            require_gate,
        ),
        Action(
            "backup",
            _backup_describe(plan, service, cfg, stamp),
            lambda: take_backup(plan, service, cfg, pg_bin=pg_bin, stamp=stamp),
        ),
        Action(
            "stop_service",
            " ".join(launchd.stop_argv()) + " (skipped when not loaded)",
            launchd.stop,
        ),
        Action(
            "fenced_delta_import",
            "drover-server lake import --since <max(date) in lake.agent_events> "
            f"--data-root {plan.data_root} --catalog-dsn-env {plan.admin_dsn_env} "
            f"--legacy-root {legacy_root} (exclusive catalog fence; dedupes)",
            delta_import,
        ),
        Action(
            "flip_backend",
            f"rewrite [analytics] in {service.config_path}: backend=ducklake, "
            f"catalog_dsn_env={plan.reader_dsn_env}, "
            f"exporter_dsn_env={plan.exporter_dsn_env}, "
            f"data_root={plan.data_root}, "
            f"verification_sha256=sha256({plan.proof_path}), epoch={epoch}; "
            f"record the switch time in {plan.state_path}",
            flip,
        ),
        Action(
            "start_service",
            " ".join(launchd.start_argv()) + " (skipped when loaded)",
            launchd.start,
        ),
        Action(
            "verify",
            _verify_describe(url, verify_timeout),
            lambda: verify_hub(url, timeout=verify_timeout),
        ),
    ]


def rollback_actions(
    plan: CutoverPlan,
    service: ServiceView,
    *,
    since: float | None = None,
    pg_bin: Path | None = None,
    verify_timeout: float = 300,
    launchd: Launchd | None = None,
    run: Callable = subprocess.run,
) -> list[Action]:
    """Backup → stop → flip to legacy → outbox replay → start → verify."""
    cfg = service.config()
    launchd = launchd or Launchd(service)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = service.hub_url(cfg)
    replay_since = since if since is not None else read_state(plan).get("switch_epoch")
    if replay_since is None:
        raise CutoverError(
            "rollback_since_unknown",
            f"no switch time in {plan.state_path}; pass --since EPOCH",
        )
    replay_args = [
        "--config",
        str(service.config_path),
        "outbox",
        "replay",
        "--sink",
        "legacy",
        "--since",
        repr(float(replay_since)),
    ]

    def replay():
        # The service's own environment: its control-store DSN and limits.
        result = run(
            [sys.executable, "-m", "drover.server", *replay_args],
            env={**os.environ, **service.env},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise CutoverError(
                "outbox_replay_failed",
                sanitize_detail(
                    RuntimeError(result.stderr or result.stdout or ""),
                    *service.env.values(),
                ),
            )
        return {"output": (result.stdout or "").strip()[-300:]}

    return [
        Action(
            "backup",
            _backup_describe(plan, service, cfg, stamp),
            lambda: take_backup(plan, service, cfg, pg_bin=pg_bin, stamp=stamp),
        ),
        Action(
            "stop_service",
            " ".join(launchd.stop_argv()) + " (skipped when not loaded)",
            launchd.stop,
        ),
        Action(
            "flip_backend",
            f"rewrite [analytics] in {service.config_path}: backend=legacy",
            lambda: {"changed": flip_config(service.config_path, LEGACY_SELECTION)},
        ),
        Action(
            "outbox_replay",
            "drover-server "
            + " ".join(replay_args)
            + " (dedupes by dedup_key; safe to repeat)",
            replay,
        ),
        Action(
            "start_service",
            " ".join(launchd.start_argv()) + " (skipped when loaded)",
            launchd.start,
        ),
        Action(
            "verify",
            _verify_describe(url, verify_timeout),
            lambda: verify_hub(url, timeout=verify_timeout),
        ),
    ]
