"""Pre-switch gate: a spare-port hub on scratch copies, A1–A7, one JSON verdict.

Read-only on production. The control store is a ``pg_dump`` of the source
(read-only session) restored into a scratch database; the lake is a disposable
rehearsal built exactly as the v2 target is (seed-only rebuild, exporter
provisioning, verify, ``lake import --since``). The gate never exports into the
operator's target lake: an exporter fed by a copied control store would leave
receipts there that the real outbox could later collide with.

The spare hub runs with its own HOME, incoming/parquet/data paths and ports;
auth, updates, APNs, spans, MCP and every LLM worker are off; harness host URLs
are blanked in the scratch copy so it never calls real hosts. The checks reuse
the ``tests/acceptance`` contracts against that live process.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .cutover import (
    DEFAULT_MEMORY_LIMIT,
    GATE_DATABASE_PREFIX,
    MEMORY_LIMIT_ENV,
    Check,
    CutoverError,
    CutoverPlan,
    control_store_risks,
    database_identity,
    libpq_env,
    memory_limit_bytes,
    pg_tool,
    run_check,
    utc_now,
    verdict,
)
from .runtime import LakeError, LakeSpec, sanitize_detail

GATE_MARKER = ".drover-gate"
GATE_HOST = "drover-gate"
GATE_REPO = ("drover-gate", "rehearsal")
GATE_TEXT = "drover gate rehearsal"
# Never handed to the spare hub: production secrets and service identity.
_STRIPPED_ENV = (
    "XPC_SERVICE_NAME",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "DROVER_CLAUDE_CREDENTIALS_PATH",
    "NEXUS_CLAUDE_CREDENTIALS_PATH",
    "OPENAI_API_KEY",
    "DROVER_API_TOKEN",
)
_LIVE_JOB = {"pending", "running"}


@dataclass(frozen=True)
class GateOptions:
    legacy_root: Path
    source_dsn_env: str
    scratch_admin_dsn_env: str
    control_schema: str = "drover_control"
    since_days: int = 60
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    hub_timeout: float = 600.0
    port: int = 0
    keep: bool = False
    replace: bool = False
    allow_shared_cluster: bool = False
    pg_bin: Path | None = None
    today: date | None = None

    @property
    def since(self) -> str:
        today = self.today or datetime.now(timezone.utc).date()
        return (today - timedelta(days=self.since_days)).isoformat()


class PeakRss:
    """Sample one process's RSS until stopped; keeps the peak and sample count."""

    def __init__(self, pid: int, *, interval: float = 0.05, reader=None):
        from drover.server.process_memory import process_rss

        self.pid = pid
        self.interval = interval
        self.reader = reader or process_rss
        self.peak = 0
        self.samples = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        try:
            rss = self.reader(self.pid)
        except (OSError, ValueError):
            self.errors += 1
            return
        self.samples += 1
        self.peak = max(self.peak, rss)

    def start(self) -> "PeakRss":
        self.sample()
        self._thread = threading.Thread(target=self._run, name="gate-rss", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "peak_bytes": self.peak,
            "peak_gib": round(self.peak / 1024**3, 3),
            "samples": self.samples,
            "sample_errors": self.errors,
            "interval_seconds": self.interval,
        }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_seed_tar(path: Path) -> Path:
    """A fresh lake's only input: the legacy empty seed plus empty companions."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.schema import _ensure_seed_parquet

    staging = path.with_name(path.name + ".d")
    parquet = staging / "parquet"
    # The bootstrap seed is the agent_events shape every legacy root carries.
    _ensure_seed_parquet(parquet)
    for table, column in (
        ("provider_usage_snapshots", "snapshot_id"),
        ("control_outbox_batches", "event_id"),
    ):
        shutil.rmtree(parquet / table, ignore_errors=True)
        (parquet / table).mkdir(parents=True)
        pq.write_table(
            pa.table({column: pa.array([], type=pa.string())}),
            parquet / table / "empty.parquet",
        )
    with tarfile.open(path, "w") as tar:
        tar.add(parquet / "agent_events", arcname="parquet/agent_events")
        for table in ("provider_usage_snapshots", "control_outbox_batches"):
            tar.add(parquet / table, arcname=f"parquet/{table}")
    shutil.rmtree(staging)
    return path


def build_fresh_lake(seed_tar: Path, spec: LakeSpec) -> dict[str, Any]:
    """Seed-only rebuild → exporter provisioning → verify (the v2 target recipe)."""
    from .exporter import provision_exporter
    from .rebuild import rebuild, verify

    report = rebuild(seed_tar, spec)
    provision_exporter(spec)
    verified = verify(spec)
    return {
        "rebuild_peak_rss_bytes": report.get("peak_rss_bytes"),
        "verify_keys": sorted(verified)[:20] if isinstance(verified, dict) else None,
        "serving_proof": (spec.data_root / "verification/serving-proof.json").is_file(),
    }


def gate_config_text(
    *,
    duckdb_path: Path,
    incoming: Path,
    parquet: Path,
    worktrees: Path,
    control_dsn_env: str,
    schema: str,
    ports: dict[str, int],
    budget_bytes: int,
    analytics: dict[str, Any],
) -> str:
    def q(value) -> str:
        return json.dumps(str(value))

    analytics_lines = "\n".join(f"{k} = {json.dumps(v)}" for k, v in analytics.items())
    return f"""# Generated by drover-server gate; isolated from the production hub.
[paths]
incoming_dir = {q(incoming)}
parquet_dir = {q(parquet)}
duckdb_path = {q(duckdb_path)}
worktrees_dir = {q(worktrees)}

[control_store]
backend = "postgres"
dsn_env = {q(control_dsn_env)}
schema = {q(schema)}

[server]
metrics_http_port = {ports["metrics"]}
mcp_http_port = {ports["mcp"]}
otlp_grpc_port = {ports["otlp"]}
metrics_host = "127.0.0.1"

[auth]
enabled = false

[update]
enabled = false

[apns]
enabled = false

[telemetry]
spans_enabled = false

[redis_jobs]
enabled = false

[redis_shadow]
enabled = false

[summarizer]
# "cloud" with every credential stripped (and HOME redirected away from
# ~/.claude) selects no backend: nothing copied from production leaves the
# host. "local" is retired and would fall back to the claude-code harness.
backend_policy = "cloud"
local_ollama_url = ""
gpu_relay_url = ""
gpu_ollama_url = ""
mac_ollama_url = ""

[embeddings]
api_base_url = ""
api_key = ""
mac_ollama_url = ""

[memory]
rss_budget_bytes = {budget_bytes}

[analytics]
{analytics_lines}
"""


def _event(index: str, session_id: str, run_id: str) -> dict[str, Any]:
    return {
        "id": f"drover-gate-{run_id}-{index}",
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "user_message",
        "agent_id": GATE_HOST,
        "message": {"role": "user", "content": f"{GATE_TEXT} turn {index}"},
        "raw_data": {
            "harness": "claude",
            "_repo_owner": GATE_REPO[0],
            "_repo_name": GATE_REPO[1],
        },
    }


class GateRun:
    """One gate execution; owns every scratch resource it creates."""

    def __init__(self, plan: CutoverPlan, options: GateOptions, echo=print):
        self.plan = plan
        self.o = options
        self.echo = echo
        self.run_id = uuid4().hex[:10]
        self.dir = plan.gate_dir
        self.since = options.since
        self.budget = memory_limit_bytes(options.memory_limit)
        self.created: list[str] = []
        self.set_env: list[str] = []
        self.hub: subprocess.Popen | None = None
        self.hub_rss: PeakRss | None = None
        self.import_rss: PeakRss | None = None
        self.hub_started_epoch: float | None = None
        self.base_url = ""
        self.migrations_before: list[tuple[int, str]] = []
        self.events: dict[str, str] = {}
        self.cfg = None
        self.spec: LakeSpec | None = None
        self.readyz_memory: Any = None

    # -- paths -----------------------------------------------------------
    @property
    def client_path(self) -> Path:
        return self.dir / "data" / "gate-client.duckdb"

    @property
    def hub_config(self) -> Path:
        return self.dir / "gate.toml"

    @property
    def replay_config(self) -> Path:
        return self.dir / "replay.toml"

    def log(self, message: str) -> None:
        self.echo(f"[gate {self.plan.name}] {message}")

    # -- secrets ---------------------------------------------------------
    def _secret(self, env_name: str) -> str:
        value = os.environ.get(env_name, "")
        if not value:
            raise CutoverError("gate_env_missing", env_name)
        return value

    def secrets(self) -> list[str]:
        names = [self.o.source_dsn_env, self.o.scratch_admin_dsn_env, *self.set_env]
        return [os.environ[n] for n in names if os.environ.get(n)]

    def _scratch_dsn(self, database: str) -> str:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(
            self._secret(self.o.scratch_admin_dsn_env), dbname=database
        )

    def _export(self, name: str, value: str) -> None:
        os.environ[name] = value
        if name not in self.set_env:
            self.set_env.append(name)

    # -- setup -----------------------------------------------------------
    def prepare_dir(self) -> None:
        if self.dir.exists() and any(self.dir.iterdir()):
            if not (self.o.replace and (self.dir / GATE_MARKER).is_file()):
                raise CutoverError(
                    "gate_dir_not_empty",
                    f"{self.dir}; pass --replace to reuse a previous gate's directory",
                )
            shutil.rmtree(self.dir)
        for sub in ("home", "incoming", "parquet", "data", "logs", "worktrees"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        (self.dir / GATE_MARKER).write_text(self.run_id + "\n")
        legacy = Path(self.o.legacy_root).resolve()
        if legacy == self.dir or self.dir in legacy.parents:
            raise CutoverError("gate_legacy_root_inside_gate_dir", str(legacy))

    def check_isolation(self) -> None:
        source = self._secret(self.o.source_dsn_env)
        admin = self._secret(self.o.scratch_admin_dsn_env)
        src_host, src_port, src_db = database_identity(source)
        adm_host, adm_port, _ = database_identity(admin)
        if src_db.startswith(GATE_DATABASE_PREFIX):
            raise CutoverError("gate_source_is_scratch", src_db)
        if (src_host, src_port) == (adm_host, adm_port) and not (
            self.o.allow_shared_cluster
        ):
            raise CutoverError(
                "gate_scratch_cluster_is_source",
                "use a separate scratch cluster or pass --allow-shared-cluster",
            )

    def create_database(self, name: str) -> None:
        import psycopg
        from psycopg import sql

        if not name.startswith(GATE_DATABASE_PREFIX):
            raise CutoverError("gate_database_name_unsafe", name)
        admin = self._secret(self.o.scratch_admin_dsn_env)
        with psycopg.connect(admin, autocommit=True) as con:
            exists = con.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", [name]
            ).fetchone()
            if exists:
                if not self.o.replace:
                    raise CutoverError(
                        "gate_database_exists", f"{name}; pass --replace"
                    )
                con.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
            con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.created.append(name)

    def restore_control(self) -> dict[str, Any]:
        import psycopg
        from psycopg import sql

        source = self._secret(self.o.source_dsn_env)
        dump = self.dir / "control.dump"
        started = time.monotonic()
        self._run_tool(
            [
                pg_tool("pg_dump", self.o.pg_bin),
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--schema={self.o.control_schema}",
                f"--file={dump}",
            ],
            libpq_env(source, read_only=True),
            "gate_source_dump_failed",
        )
        self.create_database(self.plan.gate_control_database)
        scratch = self._scratch_dsn(self.plan.gate_control_database)
        with psycopg.connect(scratch, autocommit=True) as con:
            try:
                con.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except psycopg.Error:
                pass  # Only needed when the source uses pgvector types.
        self._run_tool(
            [
                pg_tool("pg_restore", self.o.pg_bin),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={self.plan.gate_control_database}",
                str(dump),
            ],
            libpq_env(scratch),
            "gate_scratch_restore_failed",
        )
        schema = self.o.control_schema
        with psycopg.connect(scratch, autocommit=True) as con:
            # Scratch-only isolation: the spare hub must never call real hosts.
            hosts = con.execute(
                sql.SQL("UPDATE {} SET local_url = NULL, tailscale_url = NULL").format(
                    sql.Identifier(schema, "harness_hosts")
                )
            ).rowcount
            self.migrations_before = [
                (int(v), str(a))
                for v, a in con.execute(
                    sql.SQL(
                        "SELECT version, applied_at FROM {} ORDER BY version"
                    ).format(sql.Identifier(schema, "control_schema_migrations"))
                ).fetchall()
            ]
            risks = control_store_risks(con, schema)
        self._export(self.plan.gate_control_dsn_env, scratch)
        return {
            "dump_bytes": dump.stat().st_size,
            "seconds": round(time.monotonic() - started, 3),
            "migration_versions": [v for v, _ in self.migrations_before],
            "hosts_isolated": hosts,
            **risks,
        }

    def _run_tool(self, argv: list[str], env: dict[str, str], code: str) -> None:
        result = subprocess.run(argv, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CutoverError(
                code,
                sanitize_detail(
                    RuntimeError((result.stderr or "").strip()), *self.secrets()
                ),
            )

    def lake_spec(self) -> LakeSpec:
        directory = self._secret("DROVER_LAKE_EXTENSION_DIR")
        digest = self._secret("DROVER_LAKE_ENGINE_SHA256")
        return LakeSpec(
            self.plan.gate_catalog_dsn_env, self.dir / "lake", Path(directory), digest
        )

    def build_lake(self) -> dict[str, Any]:
        self.create_database(self.plan.gate_catalog_database)
        self._export(
            self.plan.gate_catalog_dsn_env,
            self._scratch_dsn(self.plan.gate_catalog_database),
        )
        self.spec = self.lake_spec()
        started = time.monotonic()
        seed = write_seed_tar(self.dir / "seed.tar")
        result = build_fresh_lake(seed, self.spec)
        result["seconds"] = round(time.monotonic() - started, 3)
        return result

    def run_import(self) -> dict[str, Any]:
        assert self.spec is not None
        argv = [
            sys.executable,
            "-m",
            "drover.server",
            "lake",
            "import",
            "--since",
            self.since,
            "--data-root",
            str(self.spec.data_root),
            "--catalog-dsn-env",
            self.spec.catalog_dsn_env,
            "--legacy-root",
            str(Path(self.o.legacy_root).resolve()),
        ]
        log = self.dir / "logs" / "import.log"
        started = time.monotonic()
        with log.open("w") as stderr:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=stderr, text=True
            )
            self.import_rss = PeakRss(process.pid).start()
            try:
                stdout, _ = process.communicate()
            finally:
                self.import_rss.stop()
        seconds = round(time.monotonic() - started, 3)
        if process.returncode != 0:
            raise CutoverError("gate_import_failed", self._tail(log))
        report = json.loads(stdout)
        return {
            "since": self.since,
            "report": report,
            "seconds": seconds,
            "rss": self.import_rss.as_dict(),
        }

    def _tail(self, path: Path, size: int = 600) -> str:
        try:
            text = path.read_text(errors="replace")[-size:]
        except OSError:
            return ""
        for secret in self.secrets():
            text = text.replace(secret, "***")
        return text

    def analytics(self) -> dict[str, Any]:
        import hashlib

        assert self.spec is not None
        proof = self.spec.data_root / "verification" / "serving-proof.json"
        return {
            "backend": "ducklake",
            "catalog_dsn_env": self.spec.catalog_dsn_env,
            "data_root": str(self.spec.data_root.resolve()),
            "extension_dir": str(self.spec.extension_dir),
            "engine_sha256": self.spec.engine_sha256,
            "verification_sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
            "epoch": f"gate-{self.plan.name}-{self.run_id}",
        }

    def write_configs(self) -> None:
        port = self.o.port or free_port()
        self.ports = {"metrics": port, "mcp": free_port(), "otlp": free_port()}
        common = dict(
            incoming=self.dir / "incoming",
            parquet=self.dir / "parquet",
            worktrees=self.dir / "worktrees",
            control_dsn_env=self.plan.gate_control_dsn_env,
            schema=self.o.control_schema,
            ports=self.ports,
            budget_bytes=self.budget,
            analytics=self.analytics(),
        )
        self.hub_config.write_text(
            gate_config_text(duckdb_path=self.dir / "data" / "drover.duckdb", **common)
        )
        # Rollback replay runs beside the live spare hub; its own DuckDB path
        # avoids contending for the hub's analytical file lock.
        self.replay_config.write_text(
            gate_config_text(duckdb_path=self.dir / "data" / "replay.duckdb", **common)
        )
        self.base_url = f"http://127.0.0.1:{port}"

    def child_env(self) -> dict[str, str]:
        dropped = {
            self.o.source_dsn_env,
            self.o.scratch_admin_dsn_env,
            self.plan.reader_dsn_env,
            self.plan.exporter_dsn_env,
            self.plan.admin_dsn_env,
            *_STRIPPED_ENV,
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in dropped and not k.startswith("PG")
        }
        env["HOME"] = str(self.dir / "home")
        env[MEMORY_LIMIT_ENV] = self.o.memory_limit
        return env

    def start_hub(self) -> dict[str, Any]:
        argv = [
            sys.executable,
            "-m",
            "drover.server",
            "--config",
            str(self.hub_config),
            "run",
            "--role",
            "all",
            "--no-otlp",
            "--no-mcp",
            "--no-summarizer",
            "--no-briefs",
            "--no-embeddings",
            "--metrics-host",
            "127.0.0.1",
        ]
        log = (self.dir / "logs" / "hub.log").open("w")
        self.hub_started_epoch = time.time() - 1
        started = time.monotonic()
        self.hub = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.child_env(),
            start_new_session=True,
        )
        self.hub_rss = PeakRss(self.hub.pid).start()
        deadline = started + self.o.hub_timeout
        last = None
        while time.monotonic() < deadline:
            if self.hub.poll() is not None:
                raise CutoverError(
                    "gate_hub_exited", self._tail(self.dir / "logs" / "hub.log")
                )
            try:
                status, body = self.request("GET", "/readyz", timeout=5)
                last = status
                if status == 200:
                    return {
                        "url": self.base_url,
                        "pid": self.hub.pid,
                        "ready_seconds": round(time.monotonic() - started, 3),
                    }
            except OSError as exc:
                last = sanitize_detail(exc)
            time.sleep(1)
        raise CutoverError("gate_hub_not_ready", f"last /readyz: {last}")

    def register_client(self) -> None:
        """This process reads the same scratch stores through its own path."""
        from drover.config import load_config
        from drover.schema import bootstrap
        from drover.server.control_store import configure_control_store
        from drover.server.lake.serving import configure_analytics

        cfg = load_config(self.hub_config)
        self.cfg = replace(cfg, duckdb_path=self.client_path)
        configure_control_store(self.client_path, cfg.control_store)
        configure_analytics(self.client_path, cfg.analytics)
        bootstrap(parquet_dir=self.dir / "client-parquet", duckdb_path=self.client_path)

    def stop_hub(self) -> None:
        if self.hub is None:
            return
        try:
            if self.hub.poll() is None:
                try:
                    readyz = self.request("GET", "/readyz", timeout=5)[1]
                    self.readyz_memory = json.loads(readyz).get("memory")
                except (OSError, ValueError, AttributeError):
                    pass
                os.killpg(self.hub.pid, signal.SIGTERM)
                try:
                    self.hub.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(self.hub.pid, signal.SIGKILL)
                    self.hub.wait(timeout=30)
        finally:
            if self.hub_rss is not None:
                self.hub_rss.stop()

    def close(self) -> None:
        self.stop_hub()
        try:
            from drover.server.control_store import close_control_store

            close_control_store(self.client_path)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        if not self.o.keep:
            self.drop_databases()
        for name in self.set_env:
            os.environ.pop(name, None)

    def drop_databases(self) -> None:
        import psycopg
        from psycopg import sql

        admin = os.environ.get(self.o.scratch_admin_dsn_env)
        if not admin:
            return
        with psycopg.connect(admin, autocommit=True) as con:
            for name in self.created:
                if name.startswith(GATE_DATABASE_PREFIX):
                    con.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )

    # -- HTTP --------------------------------------------------------------
    def request(self, method: str, path: str, body=None, timeout: float = 15):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # -- helpers shared with tests/acceptance -------------------------------
    def register(self, session_id: str, **options):
        from drover.server.harness.registry import HarnessRegistry

        registry = HarnessRegistry(self.client_path)
        registry.register_host(host_id=GATE_HOST, display_name="Gate", kind="test")
        registry.create_session(
            host_id=GATE_HOST,
            harness="claude",
            command="test",
            session_id=session_id,
            repo_owner=GATE_REPO[0],
            repo_name=GATE_REPO[1],
            branch="main",
            **options,
        )
        return registry

    def summarize(self, session_id: str, *, deadline: float = 300):
        from drover.server.ledger import SUMMARIZE_SESSION, JobLedger
        from drover.server.summarizer.jobs import enqueue_summary_generation
        from drover.server.summarizer.worker import SummarizerWorker

        enqueue_summary_generation(self.client_path, session_id, "drover-gate-v1")
        # No backend_config: the worker always calls this deterministic stub,
        # so no model credential (and no model) is ever involved.
        worker = SummarizerWorker(
            duckdb_path=self.client_path,
            _llm_call=lambda *a, **kw: {
                "summary_md": f"{GATE_TEXT} summary",
                "next_steps_md": "Continue",
                "open_questions": [],
            },
        )
        ledger = JobLedger(self.client_path)
        stop = time.monotonic() + deadline
        job = ledger.latest(SUMMARIZE_SESSION, session_id)
        # A copied queue may hold older due jobs; drain until ours settles.
        while job is not None and job.status in _LIVE_JOB:
            if time.monotonic() > stop or not worker.drain_once():
                break
            job = ledger.latest(SUMMARIZE_SESSION, session_id)
        return job

    def wait_in_lake(self, event_id: str, timeout: float = 30) -> float | None:
        from drover.server.lake.serving import open_history

        started = time.monotonic()
        while time.monotonic() - started < timeout:
            with open_history(self.client_path) as con:
                if con.execute(
                    "SELECT id FROM agent_events WHERE id=?", [event_id]
                ).fetchone():
                    return round(time.monotonic() - started, 3)
            time.sleep(0.25)
        return None

    def write_incoming(self, events: list[dict], run: str) -> None:
        from drover.collect.sources import write_events_jsonl
        from drover.models import AgentEvent

        write_events_jsonl(
            [AgentEvent.model_validate(e) for e in events],
            self.dir / "incoming",
            run_id=run,
            source_id="collector",
        )

    # -- risk checks -------------------------------------------------------
    @staticmethod
    def identity_check(restore: dict[str, Any]) -> tuple[bool, dict]:
        evidence = {
            "sessions": restore["identity_sessions"],
            "limit": restore["identity_limit"],
            "why": "open_history refuses analytics_identity_limit_exceeded above it",
        }
        return restore["identity_sessions"] <= restore["identity_limit"], evidence

    @staticmethod
    def export_batches_check(restore: dict[str, Any]) -> tuple[bool, dict]:
        evidence = {
            "unacknowledged": restore["unacknowledged_export_batches"],
            "catalog_ids": restore["unacknowledged_catalog_ids"],
            "why": "a new exporter resumes these first; another catalog's rows "
            "stop it with lake_export_input_mismatch",
        }
        return restore["unacknowledged_export_batches"] == 0, evidence

    # -- A1–A7 ---------------------------------------------------------------
    def a1(self):
        from drover.server.mcp.tools import drover_recall, drover_session_replay

        session = f"drover-gate-{self.run_id}-a1"
        event = _event("a1", session, self.run_id)
        self.events["a1"] = event["id"]
        self.write_incoming([event], f"gate-{self.run_id}-a1")
        seconds = self.wait_in_lake(event["id"])
        replay = drover_session_replay(duckdb_path=self.client_path, session_id=session)
        replayed = any(r.get("id") == event["id"] for r in replay.get("events", []))
        job = self.summarize(session)
        recall = drover_recall(
            duckdb_path=self.client_path,
            query=GATE_TEXT,
            repo_owner=GATE_REPO[0],
            repo_name=GATE_REPO[1],
        )
        recalled = any(r.get("session_id") == session for r in recall["results"])
        passed = (
            seconds is not None
            and replay.get("status") == "ok"
            and replayed
            and job is not None
            and job.status == "succeeded"
            and recalled
        )
        return passed, {
            "event_id": event["id"],
            "lake_visible_seconds": seconds,
            "limit_seconds": 30,
            "replay_status": replay.get("status"),
            "replayed": replayed,
            "summary_job": getattr(job, "status", None),
            "recalled": recalled,
        }

    def a2(self):
        from drover.server.db import control_plane_connection

        session = f"drover-gate-{self.run_id}-a2"
        registry = self.register(session)
        harness_id = f"drover-gate-{self.run_id}-harness"
        api_id = f"drover-gate-{self.run_id}-api"
        collector = _event("a2", session, self.run_id)
        self.events["a2"] = collector["id"]
        statuses = []
        for attempt in range(2):
            registry.append_events_if_new(
                [
                    dict(
                        event_id=harness_id,
                        session_id=session,
                        event_type="user_input",
                        payload={"text": "harness"},
                        seq=1,
                    )
                ]
            )
            status, _ = self.request(
                "POST",
                "/harness/events",
                {
                    "events": [
                        dict(
                            event_id=api_id,
                            session_id=session,
                            type="user_input",
                            seq=2,
                            text="api",
                        )
                    ]
                },
            )
            statuses.append(status)
            self.write_incoming([collector], f"gate-{self.run_id}-a2-{attempt}")
        ids = [harness_id, api_id, collector["id"]]
        deadline = time.monotonic() + 30
        counts: dict[str, int] = {}
        while time.monotonic() < deadline:
            with control_plane_connection(self.client_path) as con:
                counts = dict(
                    con.execute(
                        "SELECT event_id, count(*) FROM control_outbox_events "
                        "WHERE event_id IN (?, ?, ?) GROUP BY event_id",
                        ids,
                    ).fetchall()
                )
            pending = list((self.dir / "incoming").glob("*.jsonl"))
            if len(counts) == 3 and not pending:
                break
            time.sleep(0.25)
        parquet = [
            str(p)
            for p in (self.dir / "parquet").rglob("*.parquet")
            if "_seed" not in str(p)
        ]
        passed = (
            counts == {i: 1 for i in ids}
            and all(s == 200 for s in statuses)
            and not parquet
        )
        return passed, {
            "outbox_counts": counts,
            "http_statuses": statuses,
            "ingest_parquet_files": parquet[:5],
        }

    def a3(self):
        from drover.server.lake.serving import open_history

        with open_history(self.client_path) as con:
            row = con.execute(
                "SELECT session_id, count(*) AS n FROM agent_events "
                "WHERE session_id NOT LIKE 'drover-gate-%' "
                "GROUP BY session_id ORDER BY n DESC LIMIT 1"
            ).fetchone()
        if not row:
            return False, {"error": "the imported window has no sessions"}
        session, events = row
        job = self.summarize(session)
        return job is not None and job.status == "succeeded", {
            "session_id": session,
            "events": events,
            "summary_job": getattr(job, "status", None),
            "last_error": getattr(job, "last_error", None),
        }

    def a4(self):
        latencies, activities, statuses = [], [], []
        for _ in range(5):
            started = time.perf_counter()
            status, body = self.request("GET", "/cockpit/overview?days=30", timeout=15)
            latencies.append(round(time.perf_counter() - started, 6))
            statuses.append(status)
            if status == 200:
                activities.append(json.loads(body).get("activity", {}).get("status"))
        p95 = max(latencies)  # nearest-rank p95 of five requests
        passed = (
            all(s == 200 for s in statuses) and activities == ["ok"] * 5 and p95 < 2
        )
        return passed, {
            "statuses": statuses,
            "activity": activities,
            "latencies_seconds": latencies,
            "p95_seconds": p95,
            "limit_seconds": 2,
        }

    def a5(self):
        import psycopg
        from psycopg import sql

        scratch = os.environ[self.plan.gate_control_dsn_env]
        with psycopg.connect(scratch) as con:
            after = [
                (int(v), str(a))
                for v, a in con.execute(
                    sql.SQL(
                        "SELECT version, applied_at FROM {} ORDER BY version"
                    ).format(
                        sql.Identifier(
                            self.o.control_schema, "control_schema_migrations"
                        )
                    )
                ).fetchall()
            ]
        before = self.migrations_before
        versions = [v for v, _ in after]
        passed = (
            after[: len(before)] == before
            and versions == list(range(1, len(versions) + 1))
            and len(after) >= len(before)
        )
        return passed, {
            "before": [v for v, _ in before],
            "after": versions,
            "applied_by_startup": versions[len(before) :],
            "released_rows_preserved": after[: len(before)] == before,
        }

    def a6(self):
        import duckdb

        expected = sorted(self.events.values())
        if not expected:
            return False, {"error": "no gate events were written (A1/A2 failed)"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "drover.server",
                "--config",
                str(self.replay_config),
                "outbox",
                "replay",
                "--sink",
                "legacy",
                "--since",
                repr(float(self.hub_started_epoch or 0)),
            ],
            env=self.child_env(),
            capture_output=True,
            text=True,
            timeout=900,
        )
        found: set[str] = set()
        files = list((self.dir / "parquet" / "agent_events").rglob("*.parquet"))
        if result.returncode == 0 and files:
            placeholders = ",".join("?" for _ in expected)
            with duckdb.connect() as con:
                found = {
                    r[0]
                    for r in con.execute(
                        "SELECT id FROM read_parquet(?, hive_partitioning=true, "
                        f"union_by_name=true) WHERE id IN ({placeholders})",
                        [[str(f) for f in files], *expected],
                    ).fetchall()
                }
        status, health = self.request("GET", "/healthz")
        passed = (
            result.returncode == 0
            and set(expected) <= found
            and status == 200
            and health.strip() == "ok\nanalytical=ok"
        )
        return passed, {
            "replay_output": (result.stdout or "").strip()[-200:],
            "replay_error": (
                self._clean(result.stderr)[-300:] if result.returncode else None
            ),
            "expected_ids": expected,
            "replayed_ids": sorted(found),
            "healthz": health.strip(),
        }

    def a7(self):
        import duckdb

        from drover.server.db import control_plane_connection
        from drover.server.mcp.tools import drover_session_replay

        root = Path(self.o.legacy_root).resolve() / "agent_events"
        days = sorted(
            p.name[5:]
            for p in root.glob("date=*")
            if p.name != "date=_seed" and p.name[5:] < self.since
        )
        if not days:
            return False, {
                "error": "no legacy partition precedes the import watermark",
                "watermark": self.since,
            }
        day = days[-1]
        with duckdb.connect() as con:
            native, started = con.execute(
                "SELECT session_id, min(timestamp) FROM read_parquet(?, "
                "hive_partitioning=true, union_by_name=true) "
                "WHERE session_id IS NOT NULL GROUP BY session_id "
                "ORDER BY session_id LIMIT 1",
                [str(root / f"date={day}" / "**" / "*.parquet")],
            ).fetchone()
        if isinstance(started, str):
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        with control_plane_connection(self.client_path) as con:
            row = con.execute(
                "SELECT session_id FROM harness_sessions "
                "WHERE session_id = ? OR native_session_id = ? LIMIT 1",
                [native, native],
            ).fetchone()
        registered = row is not None
        session = row[0] if row else native
        if not registered:
            self.register(session, native_session_id=native, started_at=started)
        replay = drover_session_replay(duckdb_path=self.client_path, session_id=session)
        return replay.get("status") == "archived", {
            "legacy_day": day,
            "watermark": self.since,
            "session_id": session,
            "registered_in_copy": registered,
            "replay_status": replay.get("status"),
        }

    def _clean(self, text: str | None) -> str:
        text = text or ""
        for secret in self.secrets():
            text = text.replace(secret, "***")
        return text


def run_gate(plan: CutoverPlan, options: GateOptions, *, echo=print) -> dict:
    """Run every phase; always return (and persist) one verdict document."""
    gate = GateRun(plan, options, echo)
    checks: list[Check] = []
    phases: dict[str, Any] = {}
    error = None
    started = utc_now()

    def add(check_id: str, probe: Callable[[], tuple[bool, dict]]) -> Check:
        gate.log(f"check {check_id}")
        check = run_check(check_id, probe, *gate.secrets())
        checks.append(check)
        gate.log(f"check {check_id}: {'pass' if check.passed else 'FAIL'}")
        return check

    try:
        gate.prepare_dir()
        gate.check_isolation()
        gate.log("restoring a read-only pg_dump of the source control store")
        restore = gate.restore_control()
        phases["restore"] = {
            k: v
            for k, v in restore.items()
            if k not in {"identity_sessions", "unacknowledged_export_batches"}
        }
        add("identity_limit", lambda: GateRun.identity_check(restore))
        add("stale_export_batches", lambda: GateRun.export_batches_check(restore))
        gate.log("building the fresh lake from the empty seed")
        lake = add("fresh_lake_seed_rebuild", lambda: (True, gate.build_lake()))
        if not lake.passed:
            raise CutoverError("gate_fresh_lake_failed", lake.evidence.get("error"))
        gate.log(f"importing legacy events since {gate.since}")
        imported = add("lake_import", lambda: (True, gate.run_import()))
        if not imported.passed:
            raise CutoverError("gate_import_failed", imported.evidence.get("error"))
        gate.write_configs()
        gate.log(f"starting the spare hub on {gate.base_url}")
        phases["hub"] = gate.start_hub()
        gate.register_client()
        for check_id, probe in (
            ("A5_startup_migrations", gate.a5),
            ("A1_collector_to_lake", gate.a1),
            ("A2_outbox_dedup_no_parquet", gate.a2),
            ("A4_cockpit_overview", gate.a4),
            ("A3_summarize_largest_session", gate.a3),
            ("A7_pre_watermark_archived", gate.a7),
            ("A6_rollback_outbox_replay", gate.a6),
        ):
            add(check_id, probe)
    except LakeError as exc:
        error = f"{exc.code}: {exc.detail}" if exc.detail else exc.code
    except CutoverError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - every failure still yields a verdict
        error = sanitize_detail(exc, *gate.secrets())
    finally:
        try:
            gate.close()
        except Exception as exc:  # noqa: BLE001
            phases["cleanup_error"] = sanitize_detail(exc, *gate.secrets())

    rss = {
        "budget": {MEMORY_LIMIT_ENV: options.memory_limit, "bytes": gate.budget},
        "hub": gate.hub_rss.as_dict() if gate.hub_rss else None,
        "hub_readyz_memory": gate.readyz_memory,
        "import": gate.import_rss.as_dict() if gate.import_rss else None,
    }
    if gate.hub_rss is not None:
        hub = gate.hub_rss
        checks.append(
            Check(
                "hub_rss_budget",
                hub.samples > 0 and hub.peak <= gate.budget,
                {
                    "peak_bytes": hub.peak,
                    "budget_bytes": gate.budget,
                    "samples": hub.samples,
                    "headroom_bytes": gate.budget - hub.peak,
                },
            )
        )
    document = verdict(
        "gate",
        plan,
        checks,
        error=error,
        run_id=gate.run_id,
        started_at=started,
        since=gate.since,
        resources=plan.describe(),
        phases=phases,
        rss=rss,
    )
    plan.verdict_path.parent.mkdir(parents=True, exist_ok=True)
    plan.verdict_path.write_text(json.dumps(document, indent=2, default=str) + "\n")
    return document
