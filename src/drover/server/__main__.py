"""drover-server CLI."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import click
import duckdb
from mcp.server.fastmcp import FastMCP

import drover
from drover.agent_aliases import canonicalize
from drover.config import (
    AdvisoryContentConfig,
    DroverConfig,
    config_home,
    default_config,
    default_config_path,
    default_token_file,
    load_config,
    resolve_api_token_env,
)
from drover.schema import (
    EXPECTED_TABLES,
    bootstrap,
    prune_legacy_control_plane_tables,
)
from drover.server import ledger_shadow
from drover.server.advisory.content_targets import content_bundle_from_payload
from drover.server.advisory.jobs import AdvisoryScheduler, enqueue_operational_checks
from drover.server.advisory.model_analyzer import build_configured_analysis_backend
from drover.server.advisory.repository import AdvisoryRepository
from drover.server.advisory.service import InsightsService
from drover.server.advisory.worker import (
    AdvisoryWorker,
    ContentAnalysisScheduler,
    ContentAnalysisWorker,
    load_operational_snapshot,
    operational_analyzers,
    operational_snapshot_source_version,
)
from drover.server.archive import (
    BackupConfig,
    PondArchiveClient,
    assess_metadata_only_source,
    backup_preflight_summary,
    backup_receipt_summary,
    backup_run_summary,
    build_coverage_report,
    coverage_summary,
    discover_native_history_inventory,
    export_pond_inventory,
    load_backup_config,
    load_backup_receipt,
    load_native_inventory,
    load_pond_inventory,
    load_registry_candidates,
    load_source_eligibility_receipt,
    native_inventory_summary,
    pond_inventory_summary,
    restore_backup,
    restore_summary,
    run_backup,
    run_backup_preflight,
    source_eligibility_summary,
    validate_restore_request,
    write_private_json,
)
from drover.server.archive.backup_runtime import RuntimeGuard
from drover.server.briefs.worker import (
    BriefWorker,
    enqueue_brief,
    enqueue_briefs_for_active_projects,
)
from drover.server.cockpit.analytics import AnalyticsFilters
from drover.server.cockpit.service import CockpitService, ProviderRefreshLoop
from drover.server.compact import compact_table
from drover.server.context_catalog import (
    diff_bundle,
    format_diff,
    format_validation_issues,
    import_bundle,
    load_bundle,
)
from drover.server.db import (
    CONTROL_PLANE_TABLES,
    close_control_plane_connections,
    control_plane_connection,
    control_plane_path,
    open_duckdb_connection,
    pin_control_plane_connection,
    sweep_orphaned_snapshot_scratch,
)
from drover.server.decisions import derive_decisions
from drover.server.doctor import audit_lakehouse, format_runtime_audit, runtime_audit
from drover.server.embeddings.client import EmbeddingBackendConfig
from drover.server.embeddings.worker import (
    EmbedWorker,
    enqueue_missing_span_embeds,
    reset_stale_session_embed_jobs,
    reset_stale_span_embed_jobs,
)
from drover.server.harness.recap_worker import LiveRecapWorker
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.schema import (
    audit_duplicate_harness_events,
    bootstrap_harness_tables,
    migrate_duplicate_harness_events,
)
from drover.server.jobs import RedisJobStream, RedisJobStreamConfig
from drover.server.mcp import tools as mcp_tools
from drover.server.mcp.server import build_mcp_server
from drover.server.metrics import (
    MetricsCollector,
    sequence_health_report,
    start_metrics_server,
)
from drover.server.observatory import pipeline_observatory_snapshot
from drover.server.otlp.receiver import OTLPReceiver
from drover.server.providers.service import ProviderUsageService
from drover.server.quality import format_prometheus, quality_snapshot
from drover.server.rollup import rollup_tasks
from drover.server.runtime import RuntimeLayout
from drover.server.session_graph import format_ascii, format_dot, session_graph_payload
from drover.server.summarizer.backends import SummarizerBackendConfig
from drover.server.summarizer.diagnostics import summarize_backend_auth
from drover.server.summarizer.retry import retry_errored_jobs
from drover.server.summarizer.worker import SummarizerWorker
from drover.server.update_planner import UpdatePlanner
from drover.server.watcher import (
    IncomingWatcher,
    ingest_incoming_file_once,
    sweep_processed,
)
from drover.server.web.auth import load_auth
from drover.server.web.pairing import PairingCodes
from drover.server.web.qr import pairing_url, qr_lines
from drover.session_audit import audit_session_consistency_db, format_session_audit
from drover.task_id import compute_task_id

log = logging.getLogger("drover.server")

_REDIS_JOB_STREAM_SUFFIXES = {
    "summarize": "summarize_session",
    "live_recap": "summarize_live_session",
    "brief": "regenerate_project_brief",
    "embed_session": "embed_session",
    "embed_span": "embed_span",
}

_ARCHIVE_BACKUP_ERRORS = frozenset(
    {
        "archive backup config failed",
        "archive backup preflight failed",
        "archive backup local changed",
        "archive backup storage unavailable",
        "archive backup copy failed",
        "archive backup verify failed",
        "archive backup receipt failed",
        "archive backup restore failed",
        "archive backup resource limit",
    }
)
_ARCHIVE_BACKUP_RUN_DRY_SUMMARY = {
    "mode": "dry-run",
    "preflight_ready": True,
    "remote_contacted": False,
    "schema_version": 1,
}
_ARCHIVE_BACKUP_RESTORE_DRY_SUMMARY = {
    "destination_valid": True,
    "mode": "dry-run",
    "receipt_chain_valid": True,
    "remote_contacted": False,
    "schema_version": 1,
    "store_started": False,
}


def _summarizer_backend_available(backend_cfg: SummarizerBackendConfig) -> bool:
    """Return whether starting summarizer-like workers can make progress.

    Checked before the workers start, so a host with no usable backend leaves
    its jobs pending rather than burning each one's retry budget discovering
    the same thing per job.
    """
    return (backend_cfg.allows_anthropic and backend_cfg.has_anthropic_creds) or (
        backend_cfg.allows_harness_backend and backend_cfg.has_harness_backend
    )


def _warm_cockpit(service: "CockpitService") -> None:
    """Prime the cockpit's DuckDB handles in the background at startup.

    Cold, the first overview spends its time opening the database and reading
    parquet view metadata rather than answering. Doing that here means the cost
    lands before any client asks, not on the client that asks first.
    """

    def run() -> None:
        try:
            service.overview(AnalyticsFilters(days=7))
        except Exception:  # noqa: BLE001 - warming is best effort
            log.debug("cockpit warm-up failed; the first request pays instead")

    threading.Thread(target=run, name="cockpit-warmup", daemon=True).start()


def _warm_metrics(collector: MetricsCollector) -> None:
    """Build the first /metrics render in the background at startup.

    Same bargain as ``_warm_cockpit``, for the other cold path: the first
    scrape after a restart copies the database and globs a parquet tree this
    DuckDB instance has never seen, measured at 35.6s against 7-15s warm
    (#78). Once this lands, scrapes are served from cache and refreshed
    behind the request.
    """

    def run() -> None:
        try:
            collector.warm()
        except Exception:  # noqa: BLE001 - warming is best effort
            log.debug("metrics warm-up failed; the first scrape pays instead")

    threading.Thread(target=run, name="metrics-warmup", daemon=True).start()


def _create_content_analysis_worker(
    *,
    cfg: DroverConfig,
    metrics_collector: MetricsCollector,
    backend_config: SummarizerBackendConfig,
    consent_reader: Callable[[], AdvisoryContentConfig],
) -> ContentAnalysisWorker:
    """Compose the content worker from production routing and backend policy."""

    def _fetch(host_id: str, target_ids: tuple[str, ...]):
        payload = metrics_collector.fetch_advisory_content_bundle(
            host_id, list(target_ids)
        )
        return content_bundle_from_payload(
            payload, host_id=host_id, requested_ids=target_ids
        )

    def _probe(host_id: str, target_ids: tuple[str, ...]) -> str:
        payload = metrics_collector.fetch_advisory_content_version(
            host_id, list(target_ids)
        )
        bundle_hash = payload.get("bundle_hash")
        if not isinstance(bundle_hash, str):
            raise ValueError("content version response has invalid bundle_hash")
        return bundle_hash

    scheduler = ContentAnalysisScheduler(
        duckdb_path=cfg.duckdb_path,
        registry=HarnessRegistry(cfg.duckdb_path),
        consent_reader=consent_reader,
        version_fetcher=_probe,
        interval_seconds=cfg.advisory_full_review_interval_seconds,
    )
    return ContentAnalysisWorker(
        consent_reader=consent_reader,
        bundle_fetcher=_fetch,
        backend_factory=lambda content_cfg: build_configured_analysis_backend(
            config=backend_config,
            backend_policy=content_cfg.backend_policy,
            external_consent=content_cfg.external_consent,
        ),
        duckdb_path=cfg.duckdb_path,
        repository=AdvisoryRepository(cfg.duckdb_path),
        scheduler=scheduler,
    )


_DEFAULT_CONFIG_PATH = default_config_path()

_DEFAULT_CONFIG_TEMPLATE = """\
# Drover runtime config — see docs/architecture.md

[paths]
incoming_dir = "{home}/.drover/incoming"
parquet_dir  = "{home}/.drover/parquet"
duckdb_path  = "{home}/.drover/drover.duckdb"
processed_retention_days = 7

[server]
otlp_grpc_port = 4317
mcp_http_port  = 7077
metrics_http_port = 7080  # cockpit HTTP API and Prometheus metrics

[auth]
# Central API auth. Token resolution order: DROVER_API_TOKEN env var,
# then api_token below, then auto-generated ~/.drover/api_token (created
# 0600 on first start). Set enabled = false only for local development.
enabled = true
api_token = ""

[agent]
agent_id     = "{default_agent_id}"
principal_id = "unknown"

[archive]
# Optional local Pond recall. This remains disabled until explicitly configured
# with a loopback-only HTTP URL.
enabled = false
base_url = ""
timeout_seconds = 3.0
search_limit = 5
context_before = 2
context_after = 2
max_context_chars = 24000
max_response_bytes = 1048576

[provider]
# Provider fetches run every five minutes. Retain provider-reported quota facts,
# but label them stale when no successful fetch has completed within this age.
# This value must be a finite positive integer or float in seconds.
freshness_threshold_seconds = 600

[summarizer]
# backend_policy:
#   harness = summarize with the local claude-code CLI (no API key needed)
#   hybrid  = prefer the Anthropic API, fall back to claude-code when cloud auth fails
#   cloud   = require the Anthropic API; never fall back
#   local   = retired; treated as "harness" with a warning
backend_policy  = "harness"
api_model        = "claude-haiku-4-5-20251001"
harness_model    = "haiku"  # model alias passed to `claude --model`
local_model      = "qwen3.5:35b-a3b"  # embeddings/advisory only; not summaries
local_ollama_url = ""   # e.g. "http://127.0.0.1:11435" — no wake relay
gpu_relay_url    = ""   # e.g. "http://gpu-host.local:9753" — optional WoL relay
gpu_ollama_url   = ""   # e.g. "http://gpu-host.local:11434" — optional Ollama host
wake_timeout_s   = 120  # GPU relay only
batch_size       = 8    # drain N jobs per batch

[embeddings]
# Prefer a remote OpenAI/Voyage-compatible embeddings API when configured;
# then Mac-local Ollama; then GPU/Ollama only as a final fallback.
# Anthropic/Claude does not expose a native embeddings endpoint.
api_base_url    = ""   # e.g. "https://api.openai.com/v1" or a proxy endpoint
api_key         = ""   # or set DROVER_EMBEDDINGS_API_KEY in the service env
api_model       = "text-embedding-3-small"
mac_ollama_url  = ""   # e.g. "http://127.0.0.1:11435" for Mac-local Ollama
local_model     = "nomic-embed-text"

[advisory]
# Local deterministic checks only. Advisory actions never mutate configuration.
full_review_interval_seconds = 86400
poll_interval_seconds = 5

[advisory_content]
# Content-sensitive checks are separately opt-in and local by default. Cloud
# analysis also requires external_consent = true. Targets and roots are empty
# until the operator explicitly allowlists them.
enabled = false
backend_policy = "local"
external_consent = false
targets = []
allowed_roots = []
max_file_bytes = 131072
max_bundle_bytes = 524288
excerpt_max_chars = 320

[redis_jobs]
# Optional production coordination for derived workers. Off by default; when
# enabled, workers consume Redis Streams and keep DuckDB as the durable serving
# store / compatibility queue.
enabled = false
url = "redis://127.0.0.1:6379/0"
stream_prefix = "drover:jobs"
group = "workers"
max_deliveries = 5
visibility_timeout_ms = 60000
maxlen = 100000
high_water = 1000
"""


def _resolve_config(path: Optional[str]) -> DroverConfig:
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    if p.exists():
        return load_config(p)
    return default_config()


def _advertised_host_port(cfg: DroverConfig) -> str:
    """Where clients should dial this hub.

    ``[server] advertised_url`` is written by the installer once it has
    detected a private address. Until then this falls back to loopback, which
    is correct for a simulator on the same machine and useless for a phone,
    so callers warn when they see it.
    """
    configured = cfg.server_advertised_url.strip()
    if configured:
        for scheme in ("http://", "https://"):
            if configured.startswith(scheme):
                configured = configured[len(scheme) :]
        return configured.rstrip("/")
    return f"127.0.0.1:{cfg.metrics_http_port}"


def _local_api_token(cfg: DroverConfig) -> str:
    token = resolve_api_token_env() or cfg.auth_api_token.strip()
    if token:
        return token
    try:
        return default_token_file().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise click.ClickException(
            f"no API token available: set DROVER_API_TOKEN or start "
            f"drover-server once to create {default_token_file()} ({exc})"
        ) from exc


def _local_api_host(cfg: DroverConfig) -> str:
    """The address this machine's own hub is listening on.

    Not always loopback. install.sh writes the address it detected for the
    phone into `[server].metrics_host`, and the server binds exactly that, so
    on a machine with a LAN address loopback is refused. Assuming 127.0.0.1
    here made every CLI call report "could not reach drover-server -- is it
    running?" about a server that was running and serving, and it is what
    failed the published release's own install verification for v0.2.0 and
    v0.3.0.

    A wildcard bind does answer on loopback, and loopback is the better
    choice there: it does not leave the machine and does not depend on which
    interface happens to be up.
    """

    host = (cfg.server_metrics_host or "").strip()
    if not host or host in {"0.0.0.0", "::", "*"}:
        return "127.0.0.1"
    return host


def _local_api_request(
    cfg: DroverConfig, method: str, path: str, payload: Optional[dict] = None
) -> dict:
    """Call the locally running hub.

    Pairing codes live in the hub's memory, and this CLI is a different
    process, so minting has to go over HTTP rather than in-process.
    """
    import urllib.error
    import urllib.request

    url = f"http://{_local_api_host(cfg)}:{cfg.metrics_http_port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {_local_api_token(cfg)}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raise click.ClickException(
            f"drover-server refused {method} {path}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise click.ClickException(
            f"could not reach drover-server on "
            f"{_local_api_host(cfg)}:{cfg.metrics_http_port} "
            f"-- is it running? ({exc.reason})"
        ) from exc


def _bootstrap_if_missing(cfg: DroverConfig) -> None:
    """Create the local store for first-run commands without taking live locks."""
    if not cfg.duckdb_path.exists():
        bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)


def _configure_push(cfg: DroverConfig, auth) -> None:
    """Register the APNs sender, if one is configured and auth is on.

    Push targets device credentials, which only exist when auth is enabled --
    with auth off there is nothing paired to push to.
    """
    if not getattr(auth, "enabled", False) or auth.credentials is None:
        return
    try:
        from drover.server.push import configure as configure_push

        configure_push(cfg, auth.credentials)
    except Exception:  # noqa: BLE001
        log.exception("APNs push failed to configure; continuing without it")


def _redis_job_stream_config(cfg: DroverConfig, suffix: str) -> RedisJobStreamConfig:
    return RedisJobStreamConfig(
        stream=f"{cfg.redis_jobs_stream_prefix}:{suffix}",
        group=cfg.redis_jobs_group,
        max_deliveries=cfg.redis_jobs_max_deliveries,
        visibility_timeout_ms=cfg.redis_jobs_visibility_timeout_ms,
        maxlen=cfg.redis_jobs_maxlen,
        high_water=cfg.redis_jobs_high_water,
    )


def _build_redis_job_streams(cfg: DroverConfig) -> dict[str, RedisJobStream]:
    """Create Redis streams for derived-job workers when enabled."""
    if not cfg.redis_jobs_enabled:
        return {}
    streams: dict[str, RedisJobStream] = {}
    for key, suffix in _REDIS_JOB_STREAM_SUFFIXES.items():
        streams[key] = RedisJobStream.from_url(
            cfg.redis_jobs_url, _redis_job_stream_config(cfg, suffix)
        )
    return streams


def _seed_redis_job_streams(
    *, duckdb_path: Path, streams: dict[str, RedisJobStream]
) -> dict[str, int]:
    """Mirror existing pending DuckDB jobs into Redis on startup.

    This is intentionally idempotent enough for operational cutover. Redis
    streams may receive duplicate entries across restarts; worker claim paths
    still reconcile against DuckDB before doing durable work and ACK already
    completed rows.
    """
    if not streams:
        return {}
    table_map = {
        "summarize": ("summarize_jobs", "session_id", "session_id"),
        "live_recap": ("live_recap_jobs", "session_id", "session_id"),
        "brief": ("brief_jobs", "project_key", "project_key"),
        "embed_session": ("embed_jobs", "session_id", "session_id"),
        "embed_span": ("span_embed_jobs", "span_id", "span_id"),
    }
    counts: dict[str, int] = {}
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        for key, stream in streams.items():
            table, column, field = table_map[key]
            if table in CONTROL_PLANE_TABLES:
                # `live_recap_jobs` moved to the control-plane store in #95.
                # Seeding is a startup read of a queue that the control plane
                # owns, so it reads it where the control plane keeps it.
                with control_plane_connection(duckdb_path) as cp_con:
                    counts[key] = _seed_one_stream(
                        cp_con, stream, key, table, column, field
                    )
                continue
            counts[key] = _seed_one_stream(con, stream, key, table, column, field)
    finally:
        con.close()
    return counts


def _seed_one_stream(
    con: Any,
    stream: RedisJobStream,
    key: str,
    table: str,
    column: str,
    field: str,
) -> int:
    if key == "brief":
        selected = f"{column}, source_session_id, source_version"
    elif key in ("summarize", "embed_session"):
        selected = f"{column}, source_version"
    elif key == "live_recap":
        selected = f"{column}, desired_source_seq"
    else:
        selected = column
    rows = con.execute(
        f"SELECT {selected} FROM {table} WHERE status='pending' ORDER BY enqueued_at ASC"
    ).fetchall()
    for row in rows:
        payload = {field: str(row[0])}
        if key == "brief" and row[1] is not None:
            payload["source_session_id"] = str(row[1])
        if key == "brief" and row[2] is not None:
            payload["source_version"] = str(row[2])
        elif key in ("summarize", "embed_session") and row[1] is not None:
            payload["source_version"] = str(row[1])
        elif key == "live_recap":
            payload["source_seq"] = str(row[1])
        stream.add(payload)
    return len(rows)


def _summarizer_backend_config(cfg: DroverConfig) -> SummarizerBackendConfig:
    return SummarizerBackendConfig.from_runtime(
        api_model=cfg.summarizer_api_model,
        backend_policy=cfg.summarizer_backend_policy,
        harness_model=cfg.summarizer_harness_model,
        local_model=cfg.summarizer_local_model,
        local_ollama_url=cfg.summarizer_local_ollama_url or None,
        gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
        gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
        wake_timeout_s=cfg.summarizer_wake_timeout_s,
        local_ollama_launchd_label=cfg.summarizer_local_ollama_launchd_label or None,
        local_ollama_launchd_plist=cfg.summarizer_local_ollama_launchd_plist or None,
    )


def _archive_client_from_config(cfg: DroverConfig) -> PondArchiveClient | None:
    """Construct the optional local archive client without probing Pond."""
    if not cfg.archive.enabled:
        return None
    return PondArchiveClient(cfg.archive)


def _build_runtime_mcp_server(
    *,
    cfg: DroverConfig,
    host: str,
    backend_config: SummarizerBackendConfig,
    summarize_job_stream: object | None,
) -> FastMCP:
    """Inject resolved runtime dependencies into the FastMCP server."""
    return build_mcp_server(
        duckdb_path=cfg.duckdb_path,
        host=host,
        port=cfg.mcp_http_port,
        backend_config=backend_config,
        summarize_job_stream=summarize_job_stream,
        archive_config=cfg.archive,
        archive=_archive_client_from_config(cfg),
    )


def _embedding_backend_config(
    cfg: DroverConfig, backend_cfg: SummarizerBackendConfig
) -> EmbeddingBackendConfig:
    return EmbeddingBackendConfig.from_runtime(
        api_base_url=cfg.embeddings_api_base_url or None,
        api_key=cfg.embeddings_api_key or None,
        api_model=cfg.embeddings_api_model or None,
        mac_ollama_url=cfg.embeddings_mac_ollama_url or None,
        gpu_rig=backend_cfg.gpu_rig,
        local_model=cfg.embeddings_local_model or None,
    )


def _prune_orphan_span_embed_jobs(
    *, duckdb_path: Path, limit: int, apply: bool
) -> dict[str, int]:
    con = open_duckdb_connection(duckdb_path)
    try:
        rows = con.execute(
            """
            SELECT j.span_id
            FROM span_embed_jobs j
            LEFT JOIN spans s ON s.span_id = j.span_id
            WHERE s.span_id IS NULL
              AND j.status = 'errored'
              AND COALESCE(j.last_error, '') = 'span row missing'
            ORDER BY j.updated_at NULLS LAST, j.enqueued_at NULLS LAST, j.span_id
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        span_ids = [str(row[0]) for row in rows]
        deleted = 0
        if apply and span_ids:
            con.execute(
                """
                DELETE FROM span_embed_jobs
                WHERE span_id IN (SELECT unnest(?))
                  AND status = 'errored'
                  AND COALESCE(last_error, '') = 'span row missing'
                  AND NOT EXISTS (
                    SELECT 1 FROM spans WHERE spans.span_id = span_embed_jobs.span_id
                  )
                """,
                [span_ids],
            )
            deleted = len(span_ids)
        return {"matched": len(span_ids), "deleted": deleted, "limit": limit}
    finally:
        con.close()


def _default_mcp_url(cfg: DroverConfig) -> str:
    return f"http://127.0.0.1:{cfg.mcp_http_port}/mcp"


def _parse_json_arg_pairs(pairs: tuple[str, ...]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw_value = pair.partition("=")
        if not sep or not key:
            raise click.ClickException(f"--arg must be key=value, got: {pair}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        args[key] = value
    return args


@contextmanager
def _diagnostic_db_path(path: Path):
    """Yield a short-lived DB snapshot for CLI diagnostics.

    DuckDB allows either one writer or multiple readers across processes. Even
    read-only live diagnostics can briefly block Drover workers that need a
    write connection. The diagnostic commands below only need a point-in-time
    view, so they query a copied catalog and leave the live DB lock-free.
    """
    path = Path(path)
    if not path.exists():
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="drover-diagnostic-") as tmp:
        snapshot = Path(tmp) / path.name
        shutil.copy2(path, snapshot)
        yield snapshot


@click.group()
# Load-bearing beyond convenience. `drover-server --version` is the smoke test
# install.sh runs before it will activate a freshly installed version, so
# without this flag every install downloads, verifies, installs, and then
# refuses itself. Verified against a real v0.1.1 artifact, which failed
# exactly that way.
@click.version_option(version=drover.__version__, prog_name="drover-server")
@click.option("--config", "config_path", default=None, help="Path to config TOML")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str], verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command(name="harnessd")
@click.option("--host-id", required=True, help="Stable host id, e.g. workstation")
@click.option("--display-name", default=None, help="Human-readable host label")
@click.option("--kind", default="linux", show_default=True, help="Host kind")
@click.option(
    "--listen",
    default="127.0.0.1:7081",
    show_default=True,
    help="Listen address as host:port",
)
@click.option("--local-url", default=None, help="Advertised LAN URL")
@click.option("--tailscale-url", default=None, help="Advertised Tailscale URL")
@click.option(
    "--central-url", default=None, help="Central Drover base URL for host registration"
)
@click.option(
    "--host-token",
    default=None,
    help=(
        "Shared Drover API token (falls back to DROVER_API_TOKEN, "
        "then ~/.drover/api_token)"
    ),
)
@click.pass_context
def harnessd_cmd(
    ctx: click.Context,
    host_id: str,
    display_name: Optional[str],
    kind: str,
    listen: str,
    local_url: Optional[str],
    tailscale_url: Optional[str],
    central_url: Optional[str],
    host_token: Optional[str],
) -> None:
    """Run the Drover host daemon."""
    from drover.server.harness.cli import run_harnessd_from_options

    run_harnessd_from_options(
        config_path=ctx.obj["config_path"],
        host_id=host_id,
        display_name=display_name,
        kind=kind,
        listen=listen,
        local_url=local_url,
        tailscale_url=tailscale_url,
        central_url=central_url,
        host_token=host_token,
    )


def _bootstrap_harnessd_schema(cfg: DroverConfig) -> bool:
    from drover.server.harness.cli import bootstrap_harnessd_schema

    return bootstrap_harnessd_schema(cfg)


def _parse_listen_address(value: str) -> tuple[str, int]:
    from drover.server.harness.cli import parse_listen_address

    return parse_listen_address(value)


@main.group(name="session")
def session_cmd() -> None:
    """Inspect recorded session data."""


@main.group(name="harness")
def harness_cmd() -> None:
    """Audit and migrate Drover harness data."""


@main.group(name="archive")
def archive_cmd() -> None:
    """Capture and compare local archive inventories."""


def _raise_archive_backup_error(error: Exception, fallback: str) -> None:
    """Raise one identifier-free operator category for a backup failure."""
    try:
        category = str(error)
    except Exception:
        category = fallback
    if category not in _ARCHIVE_BACKUP_ERRORS:
        category = fallback
    raise click.ClickException(category) from None


def _run_backup_preflight_for_cli(
    config: BackupConfig, drover_config: DroverConfig
) -> dict[str, object]:
    """Compose the fixed local-only preflight service for Click callbacks."""
    with tempfile.TemporaryDirectory(
        prefix=".drover-backup-preflight-",
        dir=config.receipt_directory,
    ) as workspace_text:
        workspace = Path(workspace_text)
        runtime = RuntimeGuard(drover_config)
        runtime.capture_baseline()
        result = run_backup_preflight(config, drover_config, workspace, runtime)
        runtime.finish()
        return backup_preflight_summary(result)


@archive_cmd.group(name="backup")
def archive_backup_cmd() -> None:
    """Verify, back up, and restore private Pond generations."""


@archive_backup_cmd.command(name="preflight")
@click.option("--config", "backup_config_path", required=True, type=str, metavar="FILE")
@click.pass_context
def archive_backup_preflight_cmd(ctx: click.Context, backup_config_path: str) -> None:
    """Verify the local archive denominator without contacting R2."""
    try:
        config = load_backup_config(backup_config_path)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    try:
        drover_config = _resolve_config(ctx.obj["config_path"])
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    try:
        summary = _run_backup_preflight_for_cli(config, drover_config)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup preflight failed")
    click.echo(json.dumps(summary, sort_keys=True))
    if summary.get("ready") is not True:
        ctx.exit(2)


@archive_backup_cmd.command(name="run")
@click.option("--config", "backup_config_path", required=True, type=str, metavar="FILE")
@click.option("--apply", is_flag=True)
@click.pass_context
def archive_backup_run_cmd(
    ctx: click.Context, backup_config_path: str, apply: bool
) -> None:
    """Preflight locally, or apply one immutable backup generation."""
    try:
        config = load_backup_config(backup_config_path)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    try:
        drover_config = _resolve_config(ctx.obj["config_path"])
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    if not apply:
        try:
            summary = _run_backup_preflight_for_cli(config, drover_config)
        except Exception as error:
            _raise_archive_backup_error(error, "archive backup preflight failed")
        dry_summary = dict(_ARCHIVE_BACKUP_RUN_DRY_SUMMARY)
        dry_summary["preflight_ready"] = summary.get("ready") is True
        click.echo(json.dumps(dry_summary, sort_keys=True))
        if dry_summary["preflight_ready"] is not True:
            ctx.exit(2)
        return
    try:
        receipt = run_backup(config, drover_config)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup preflight failed")
    try:
        summary = backup_run_summary(receipt)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup receipt failed")
    click.echo(json.dumps(summary, sort_keys=True))


@archive_backup_cmd.command(name="restore")
@click.option("--config", "backup_config_path", required=True, type=str, metavar="FILE")
@click.option("--receipt", "receipt_path", required=True, type=str, metavar="FILE")
@click.option(
    "--destination", "destination_path", required=True, type=str, metavar="DIRECTORY"
)
@click.option("--apply", is_flag=True)
@click.pass_context
def archive_backup_restore_cmd(
    ctx: click.Context,
    backup_config_path: str,
    receipt_path: str,
    destination_path: str,
    apply: bool,
) -> None:
    """Validate locally, or restore one receipt into a fresh stopped store."""
    try:
        config = load_backup_config(backup_config_path)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    receipt = Path(receipt_path)
    destination = Path(destination_path)
    if not apply:
        try:
            validate_restore_request(config, receipt, destination)
        except Exception as error:
            _raise_archive_backup_error(error, "archive backup restore failed")
        click.echo(json.dumps(_ARCHIVE_BACKUP_RESTORE_DRY_SUMMARY, sort_keys=True))
        return
    try:
        drover_config = _resolve_config(ctx.obj["config_path"])
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup config failed")
    try:
        result = restore_backup(config, receipt, destination, drover_config)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup restore failed")
    try:
        summary = restore_summary(result)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup restore failed")
    click.echo(json.dumps(summary, sort_keys=True))


@archive_backup_cmd.command(name="inspect-receipt")
@click.option("--receipt", "receipt_path", required=True, type=str, metavar="FILE")
def archive_backup_inspect_receipt_cmd(receipt_path: str) -> None:
    """Validate one private receipt and print only aggregate evidence."""
    try:
        receipt = load_backup_receipt(receipt_path)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup receipt failed")
    try:
        summary = backup_receipt_summary(receipt)
    except Exception as error:
        _raise_archive_backup_error(error, "archive backup receipt failed")
    click.echo(json.dumps(summary, sort_keys=True))


@archive_cmd.command(name="source-inventory")
@click.option("--host-id", required=True, metavar="HOST")
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
def archive_source_inventory_cmd(host_id: str, output: Path) -> None:
    """Capture supported native session-source metadata."""
    try:
        inventory = discover_native_history_inventory(Path.home(), host_id)
        summary = native_inventory_summary(inventory)
        write_private_json(output, inventory.to_wire())
    except Exception:
        raise click.ClickException("archive source inventory failed") from None
    click.echo(json.dumps(summary, sort_keys=True))


@archive_cmd.command(name="source-eligibility")
@click.option("--host-id", required=True, metavar="HOST")
@click.option(
    "--source",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
def archive_source_eligibility_cmd(
    host_id: str,
    source: Path,
    output: Path,
) -> None:
    """Classify one bounded metadata-only Claude source."""
    try:
        receipt = assess_metadata_only_source(Path.home(), source, host_id)
        summary = source_eligibility_summary(receipt)
        write_private_json(output, receipt.to_wire())
    except Exception:
        raise click.ClickException("archive source eligibility failed") from None
    click.echo(json.dumps(summary, sort_keys=True))


@archive_cmd.command(name="pond-inventory")
@click.option(
    "--storage-path",
    required=True,
    type=click.Path(path_type=Path),
    metavar="DIRECTORY",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--pond-binary",
    default=None,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    default="60",
    show_default=True,
    type=str,
    metavar="SECONDS",
)
def archive_pond_inventory_cmd(
    storage_path: Path,
    output: Path,
    pond_binary: Optional[Path],
    timeout_seconds: str,
) -> None:
    """Export supported Pond root-session metadata."""
    try:
        parsed_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        raise click.ClickException("archive pond inventory invalid timeout") from None
    if not math.isfinite(parsed_timeout) or not 5.0 <= parsed_timeout <= 600.0:
        raise click.ClickException("archive pond inventory invalid timeout")
    binary = pond_binary
    if binary is None:
        configured = os.environ.get("POND_BINARY", "").strip()
        if configured:
            binary = Path(configured)
    if binary is None:
        discovered = shutil.which("pond")
        if discovered:
            binary = Path(discovered)
    if binary is None:
        raise click.ClickException("archive pond inventory binary unavailable")
    try:
        inventory = export_pond_inventory(
            binary,
            output,
            storage_path=storage_path,
            timeout_seconds=parsed_timeout,
        )
        summary = pond_inventory_summary(inventory)
    except Exception:
        raise click.ClickException("archive pond inventory failed") from None
    click.echo(json.dumps(summary, sort_keys=True))


@archive_cmd.command(name="coverage")
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--db",
    "duckdb_path",
    default=None,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--source-inventory",
    "source_inventory_paths",
    required=True,
    multiple=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--pond-inventory",
    "pond_inventory_path",
    required=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--prior-source-inventory",
    "prior_source_inventory_paths",
    multiple=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.option(
    "--source-eligibility-receipt",
    "source_eligibility_receipt_paths",
    multiple=True,
    type=click.Path(path_type=Path),
    metavar="FILE",
)
@click.pass_context
def archive_coverage_cmd(
    ctx: click.Context,
    output: Path,
    duckdb_path: Optional[Path],
    source_inventory_paths: tuple[Path, ...],
    pond_inventory_path: Path,
    prior_source_inventory_paths: tuple[Path, ...],
    source_eligibility_receipt_paths: tuple[Path, ...],
) -> None:
    """Compare private source, registry, and Pond identity inventories."""
    try:
        current_sources = tuple(
            load_native_inventory(path) for path in source_inventory_paths
        )
        prior_sources = tuple(
            load_native_inventory(path) for path in prior_source_inventory_paths
        )
        eligibility_receipts = tuple(
            load_source_eligibility_receipt(path)
            for path in source_eligibility_receipt_paths
        )
        pond = load_pond_inventory(pond_inventory_path)
        cfg = _resolve_config(ctx.obj["config_path"])
        registry_path = control_plane_path(duckdb_path or cfg.duckdb_path)
        with _diagnostic_db_path(registry_path) as snapshot_path:
            registry = load_registry_candidates(snapshot_path)
        report = build_coverage_report(
            registry,
            current_sources,
            pond,
            prior_sources=prior_sources,
            eligibility_receipts=eligibility_receipts,
        )
        summary = coverage_summary(report)
        write_private_json(output, report.to_wire())
    except Exception:
        raise click.ClickException("archive coverage failed") from None
    click.echo(json.dumps(summary, sort_keys=True))
    if not report.ready_for_next_writer:
        ctx.exit(2)


@main.command(name="pair")
@click.option("--label", default="New device", show_default=True, help="Device label")
@click.pass_context
def pair_cmd(ctx: click.Context, label: str) -> None:
    """Print a QR code that pairs a phone with this fleet."""
    cfg = _resolve_config(ctx.obj["config_path"])
    minted = _local_api_request(
        cfg, "POST", "/auth/pair-codes", {"scope": "device", "label": label}
    )
    host_port = _advertised_host_port(cfg)
    url = pairing_url(host_port, minted["code"], minted["fleet_name"])

    click.echo("")
    click.echo("  Scan with the Drover app:")
    click.echo("")
    for line in qr_lines(url):
        click.echo("  " + line)
    click.echo("")
    click.echo(f"  Or enter the code manually: {minted['code']}")
    click.echo(f"  {url}")
    minutes = max(minted["expires_in_seconds"] // 60, 1)
    click.echo(f"  Expires in {minutes} min. Single use.")
    if host_port.startswith("127.0.0.1") or host_port.startswith("localhost"):
        click.echo("")
        click.echo(
            "  Warning: this address is only reachable from this machine. Set "
            "[server] advertised_url in ~/.drover/config.toml to a private LAN "
            "or Tailscale address before pairing a phone."
        )
    click.echo("")


@main.command(name="pair-host")
@click.option("--name", required=True, help="Host id for the machine being added")
@click.pass_context
def pair_host_cmd(ctx: click.Context, name: str) -> None:
    """Print the one-liner that joins another machine to this fleet."""
    cfg = _resolve_config(ctx.obj["config_path"])
    minted = _local_api_request(
        cfg,
        "POST",
        "/auth/pair-codes",
        {"scope": "host", "label": name, "host_id": name},
    )
    host_port = _advertised_host_port(cfg)
    url = pairing_url(host_port, minted["code"], minted["fleet_name"])
    minutes = max(minted["expires_in_seconds"] // 60, 1)

    click.echo("")
    click.echo(f"  Run this on the machine you are adding (expires in {minutes} min):")
    click.echo("")
    click.echo(
        "    curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/"
        "install.sh \\"
    )
    click.echo(f"      | bash -s -- --join '{url}'")
    click.echo("")


def _start_update_checker(planner, cfg: DroverConfig, stop) -> None:
    """Poll the release feed on a timer, jittered, until shutdown.

    Refreshing only decides a target; hosts converge on their own heartbeat.
    The hub's own convergence is deliberately left out of this thread: it
    would have to restart the process it is running in, and doing that from a
    daemon thread while requests are in flight is worse than telling the
    operator to run `drover-server update --check` and act.
    """
    import random
    import threading

    interval = max(cfg.update_check_interval_hours, 1) * 3600

    def loop() -> None:
        # Jitter the first check so a fleet restarted together does not poll
        # GitHub in lockstep.
        if stop.wait(random.uniform(0, 60)):
            return
        while not stop.is_set():
            try:
                planner.refresh()
            except Exception:  # noqa: BLE001 - a timer must outlive a bad poll
                log.exception("update check failed; will retry")
            if stop.wait(interval + random.uniform(0, 300)):
                return

    threading.Thread(target=loop, name="drover-update-check", daemon=True).start()


@main.command(name="rollback")
@click.option("--to", "target", default=None, help="Version to activate")
def rollback_cmd(target: str | None) -> None:
    """Point the runtime symlink at an earlier installed version.

    The watchdog already handles a version that cannot come up at all. This is
    for the other case: it starts, registers, and is still wrong.
    """
    layout = RuntimeLayout(config_home())
    installed = layout.installed_versions()
    active = layout.active_version()

    if target is None:
        earlier = [version for version in installed if version != active]
        if not earlier:
            raise click.ClickException(
                "no earlier version is installed "
                f"(have: {', '.join(installed) or 'none'})"
            )
        target = earlier[-1]
    elif target.lstrip("v") not in installed:
        raise click.ClickException(
            f"{target} is not installed (have: {', '.join(installed) or 'none'})"
        )
    else:
        target = target.lstrip("v")

    layout.flip(target)
    # Otherwise the next start sees a marker for a flip we just undid and
    # rolls back again on top of this.
    layout.clear_marker()
    click.echo(f"Activated {target} (was {active or 'unknown'}).")
    click.echo("Restart drover-server and drover-harnessd to pick it up.")


@main.command(name="update")
@click.option("--check", is_flag=True, help="Report the target version and exit")
@click.pass_context
def update_cmd(ctx: click.Context, check: bool) -> None:
    """Report what version this fleet is converging on."""
    cfg = _resolve_config(ctx.obj["config_path"])
    layout = RuntimeLayout(config_home())
    planner = UpdatePlanner(cfg, layout)
    planner.refresh()
    target = planner.target()

    click.echo(f"installed: {', '.join(layout.installed_versions()) or 'none'}")
    click.echo(f"active:    {layout.active_version() or 'unknown'}")
    click.echo(f"target:    {target.version if target else 'up to date'}")
    if cfg.update_pinned_version:
        click.echo(f"pinned:    {cfg.update_pinned_version}")
    if not cfg.update_enabled:
        click.echo("updates:   disabled in config")


@main.group(name="credentials")
def credentials_cmd() -> None:
    """List and revoke paired devices and hosts."""


@credentials_cmd.command(name="list")
@click.pass_context
def credentials_list_cmd(ctx: click.Context) -> None:
    cfg = _resolve_config(ctx.obj["config_path"])
    listing = _local_api_request(cfg, "GET", "/auth/credentials")
    rows = listing.get("credentials") or []
    if not rows:
        click.echo("No credentials are paired. Run `drover-server pair` to add one.")
        return
    for row in rows:
        state = "revoked" if row.get("revoked_at") else "active"
        last_used = row.get("last_used_at") or "never"
        click.echo(
            f"{row['id']}  {row['scope']:<6}  {state:<7}  "
            f"last used {last_used}  {row['label']}"
        )


@credentials_cmd.command(name="revoke")
@click.argument("credential_id")
@click.pass_context
def credentials_revoke_cmd(ctx: click.Context, credential_id: str) -> None:
    cfg = _resolve_config(ctx.obj["config_path"])
    _local_api_request(cfg, "DELETE", f"/auth/credentials/{credential_id}")
    click.echo(f"Revoked {credential_id}.")


def _sequence_command_payload(db_path: Path, *, apply: bool) -> dict[str, Any]:
    report = sequence_health_report(db_path, apply=apply)
    return {"database": str(db_path), **report, "applied": apply}


def _emit_sequence_command_result(
    ctx: click.Context, payload: Mapping[str, Any], *, as_json: bool
) -> None:
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(" ".join(f"{key}={value}" for key, value in payload.items()))
    if payload["mixed_sessions"]:
        ctx.exit(1)


def _duplicate_events_payload(db_path: Path, *, apply: bool) -> dict[str, Any]:
    """Audit, and optionally collapse, duplicate harness events.

    Opens the control-plane store beside the given path, the same resolution
    the sequence commands use, because harness events live there.
    """
    store = control_plane_path(db_path)
    # Read-only for an audit, and no bootstrap either way.
    #
    # `bootstrap_harness_tables` writes: it drops and recreates the client-key
    # index around its column migrations. Calling it from an audit would take a
    # write lock on a store the hub is serving from, and would mutate a store
    # the operator was only asking about. The tables already exist on any store
    # worth auditing; if they do not, the query says so.
    con = duckdb.connect(str(store), read_only=not apply)
    try:
        if apply:
            # The migration writes `dedup_key`, so the column has to exist.
            # #279 removed this call from both paths to make the audit
            # read-only, and took it off the apply path too -- which left the
            # migration failing on a store that predates the column, which is
            # every store it is meant to fix.
            bootstrap_harness_tables(con)
            result = migrate_duplicate_harness_events(con)
            payload: dict[str, Any] = {
                "collapsed_groups": result.collapsed_groups,
                "removed_rows": result.removed_rows,
                "divergent_groups": result.divergent_groups,
            }
        else:
            audit = audit_duplicate_harness_events(con)
            payload = {
                "duplicate_groups": audit.duplicate_groups,
                "collapsible_groups": audit.collapsible_groups,
                "removable_rows": audit.removable_rows,
                "divergent_groups": audit.divergent_groups,
                "divergent_examples": [
                    f"{session_id}#{seq}"
                    for session_id, seq in audit.divergent_examples
                ],
            }
    finally:
        con.close()
    return {"database": str(store), **payload, "applied": apply}


@harness_cmd.command(name="audit-duplicate-events")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Exact DuckDB path to audit. Either the lakehouse path or the "
        "control-plane store beside it; harness events live in the latter."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def harness_audit_duplicate_events_cmd(db_path: Path, as_json: bool) -> None:
    """Report duplicate harness events without mutating the database."""
    payload = _duplicate_events_payload(db_path, apply=False)
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@harness_cmd.command(name="dedupe-events")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Exact DuckDB path to deduplicate. Either the lakehouse path or the "
        "control-plane store beside it; harness events live in the latter."
    ),
)
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    help=(
        "Actually delete. Without it this reports what would go, which is the "
        "same output as audit-duplicate-events."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def harness_dedupe_events_cmd(db_path: Path, apply: bool, as_json: bool) -> None:
    """Collapse duplicate harness events. Deliberately not run at bootstrap.

    Removing tens of thousands of rows from a live control-plane store is not
    something a service start should do on its own, so this is a command an
    operator runs, having first taken a backup. Deduplication is idempotent, so
    running it twice is safe; running it never is also safe, because the events
    it removes are copies.
    """
    payload = _duplicate_events_payload(db_path, apply=apply)
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@harness_cmd.command(name="prune-legacy-tables")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "The lakehouse path -- the pre-split copy lives in the analytical "
        "store, not the control-plane store beside it."
    ),
)
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    help="Actually drop the complete tables. Without it this only reports.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def harness_prune_legacy_tables_cmd(db_path: Path, apply: bool, as_json: bool) -> None:
    """Drop pre-split control-plane copies once the control plane holds them.

    The control-plane split left the old tables in the analytical store as a
    cheap rollback. That copy is also where drover#280 resurrects deleted rows
    from on every start, so once the control plane demonstrably holds a table
    -- tested on identity for harness_events, on the primary key for the rest
    -- this removes it. A table with anything still missing is left alone.
    """
    con = duckdb.connect(str(db_path), read_only=not apply)
    try:
        payload = prune_legacy_control_plane_tables(con, db_path, apply=apply)
    finally:
        con.close()
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@harness_cmd.command(name="audit-sequences")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Exact DuckDB path to audit. Either the lakehouse path or the "
        "control-plane store beside it; harness events live in the latter."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def harness_audit_sequences_cmd(
    ctx: click.Context, db_path: Path, as_json: bool
) -> None:
    """Audit legacy harness event sequences without mutating the database."""
    _emit_sequence_command_result(
        ctx, _sequence_command_payload(db_path, apply=False), as_json=as_json
    )


@harness_cmd.command(name="migrate-sequences")
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Exact DuckDB path to migrate. Either the lakehouse path or the "
        "control-plane store beside it; harness events live in the latter."
    ),
)
@click.option(
    "--apply", is_flag=True, help="Apply sequence migration. Default is dry-run."
)
@click.pass_context
def harness_migrate_sequences_cmd(
    ctx: click.Context, db_path: Path, apply: bool
) -> None:
    """Preview or apply deterministic legacy event sequencing."""
    _emit_sequence_command_result(
        ctx, _sequence_command_payload(db_path, apply=apply), as_json=True
    )


@main.group(name="mcp")
def mcp_cmd() -> None:
    """Call Drover's streamable HTTP MCP endpoint from the CLI."""


@mcp_cmd.command(name="tools")
@click.option(
    "--url",
    default=None,
    help="MCP endpoint URL (default: configured localhost mcp_http_port)",
)
@click.option("--timeout", default=10.0, show_default=True, type=float)
@click.pass_context
def mcp_tools_cmd(ctx: click.Context, url: Optional[str], timeout: float) -> None:
    """List tools exposed by the Drover MCP server."""
    from drover.server.mcp.client import list_tools

    cfg = _resolve_config(ctx.obj["config_path"])
    tools = list_tools(url or _default_mcp_url(cfg), timeout=timeout)
    click.echo(json.dumps(tools, indent=2, sort_keys=True))


@mcp_cmd.command(name="call")
@click.argument("tool_name")
@click.option(
    "--url",
    default=None,
    help="MCP endpoint URL (default: configured localhost mcp_http_port)",
)
@click.option(
    "--args-json",
    default="{}",
    help="JSON object to pass as the tool arguments",
)
@click.option(
    "--arg",
    "arg_pairs",
    multiple=True,
    help="Extra key=value argument; JSON values are accepted",
)
@click.option("--timeout", default=30.0, show_default=True, type=float)
@click.pass_context
def mcp_call_cmd(
    ctx: click.Context,
    tool_name: str,
    url: Optional[str],
    args_json: str,
    arg_pairs: tuple[str, ...],
    timeout: float,
) -> None:
    """Call one Drover MCP tool and print the raw MCP result JSON."""
    from drover.server.mcp.client import call_tool

    try:
        arguments = json.loads(args_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--args-json is not valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise click.ClickException("--args-json must decode to an object")
    arguments.update(_parse_json_arg_pairs(arg_pairs))

    cfg = _resolve_config(ctx.obj["config_path"])
    result = call_tool(
        url or _default_mcp_url(cfg), tool_name, arguments, timeout=timeout
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@session_cmd.command(name="graph")
@click.argument("session_id")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["ascii", "json", "dot"]),
    default="ascii",
    show_default=True,
    help="Output format",
)
@click.option(
    "--max-spans",
    default=5000,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum spans to read for this session",
)
@click.pass_context
def session_graph_cmd(
    ctx: click.Context, session_id: str, output_format: str, max_spans: int
) -> None:
    """Reconstruct a parent/child span tree for SESSION_ID."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    payload = session_graph_payload(
        cfg.duckdb_path, cfg.parquet_dir, session_id, max_spans=max_spans
    )
    if payload["span_count"] == 0:
        raise click.ClickException(f"no spans found for session_id={session_id}")
    if output_format == "json":
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif output_format == "dot":
        click.echo(format_dot(payload), nl=False)
    else:
        click.echo(format_ascii(payload), nl=False)


@main.group(name="decisions")
def decisions_cmd() -> None:
    """Derive and inspect decision records."""


@main.group(name="embeddings")
def embeddings_cmd() -> None:
    """Operate embedding queues and backfills."""


@main.group(name="context")
def context_cmd() -> None:
    """Validate, diff, and import curated metadata bundles."""


@main.group(name="incoming")
def incoming_cmd() -> None:
    """Operate incoming JSONL ingestion."""


@incoming_cmd.command(name="ingest-once")
@click.argument("jsonl_path", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--apply",
    is_flag=True,
    help="Actually ingest and move the file. Default is dry-run.",
)
@click.pass_context
def incoming_ingest_once_cmd(ctx: click.Context, jsonl_path: Path, apply: bool) -> None:
    """Ingest one pending incoming JSONL through the watcher path."""
    cfg = _resolve_config(ctx.obj["config_path"])
    if jsonl_path.suffix != ".jsonl" or ".processed" in jsonl_path.parts:
        raise click.ClickException(
            "JSONL_PATH must be an unprocessed *.jsonl incoming file"
        )
    if not apply:
        click.echo(f"mode=dry-run path={jsonl_path} size={jsonl_path.stat().st_size}")
        return
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    streams = _build_redis_job_streams(cfg)
    ingest_incoming_file_once(
        jsonl_path,
        parquet_dir=cfg.parquet_dir,
        duckdb_path=cfg.duckdb_path,
        summarize_job_stream=streams.get("summarize"),
    )
    click.echo(f"mode=apply ingested={jsonl_path}")


@embeddings_cmd.command(name="drain-once")
@click.option("--limit", default=16, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--apply", is_flag=True, help="Drain pending embed jobs once. Default is dry-run."
)
@click.pass_context
def embeddings_drain_once_cmd(ctx: click.Context, limit: int, apply: bool) -> None:
    """Drain pending embedding jobs once without starting the daemon loop."""
    cfg = _resolve_config(ctx.obj["config_path"])
    if not apply:
        con = open_duckdb_connection(cfg.duckdb_path, read_only=True, role="diagnostic")
        try:
            session_pending = con.execute(
                "SELECT count(*) FROM embed_jobs WHERE status='pending'"
            ).fetchone()[0]
            span_pending = con.execute(
                "SELECT count(*) FROM span_embed_jobs WHERE status='pending'"
            ).fetchone()[0]
        finally:
            con.close()
        click.echo(
            f"mode=dry-run pending_sessions={session_pending} pending_spans={span_pending} limit={limit}"
        )
        return
    backend_cfg = _summarizer_backend_config(cfg)
    embeddings_cfg = _embedding_backend_config(cfg, backend_cfg)
    worker = EmbedWorker(
        duckdb_path=cfg.duckdb_path,
        backend_config=backend_cfg,
        embedding_config=embeddings_cfg,
        batch_size=limit,
    )
    processed = worker.drain_batch(max_jobs=limit)
    click.echo(f"mode=apply processed={processed} limit={limit}")


@embeddings_cmd.command(name="prune-orphan-spans")
@click.option("--limit", default=1000, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--apply", is_flag=True, help="Delete matched orphan jobs. Default is dry-run."
)
@click.pass_context
def embeddings_prune_orphan_spans_cmd(
    ctx: click.Context, limit: int, apply: bool
) -> None:
    """Prune errored span embed jobs whose span rows are absent."""
    cfg = _resolve_config(ctx.obj["config_path"])
    result = _prune_orphan_span_embed_jobs(
        duckdb_path=cfg.duckdb_path, limit=limit, apply=apply
    )
    mode = "apply" if apply else "dry-run"
    click.echo(
        f"mode={mode} matched={result['matched']} deleted={result['deleted']} limit={result['limit']}"
    )


@embeddings_cmd.command(name="enqueue-spans")
@click.option("--limit", default=1000, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--since-days",
    default=None,
    type=click.IntRange(min=0),
    help="Only scan span date partitions newer than this many days.",
)
@click.option(
    "--apply", is_flag=True, help="Actually enqueue jobs. Default is dry-run."
)
@click.pass_context
def embeddings_enqueue_spans_cmd(
    ctx: click.Context, limit: int, since_days: Optional[int], apply: bool
) -> None:
    """Enqueue missing span embedding jobs for existing spans."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    result = enqueue_missing_span_embeds(
        duckdb_path=cfg.duckdb_path,
        parquet_dir=cfg.parquet_dir,
        limit=limit,
        apply=apply,
        since_days=since_days,
    )
    mode = "apply" if apply else "dry-run"
    click.echo(
        f"mode={mode} candidate_count={result['candidate_count']} "
        f"enqueued={result['enqueued']} limit={limit}"
    )


@embeddings_cmd.command(name="reset-stale-spans")
@click.option(
    "--stale-after-hours", default=24, show_default=True, type=click.IntRange(min=1)
)
@click.option("--limit", default=1000, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--apply",
    is_flag=True,
    help="Actually reset stale running span jobs to pending. Default is dry-run.",
)
@click.pass_context
def embeddings_reset_stale_spans_cmd(
    ctx: click.Context, stale_after_hours: int, limit: int, apply: bool
) -> None:
    """Reset stranded running span embedding jobs back to pending.

    Safe operator flow: run without --apply first to preview the number of
    stale running jobs, then rerun with --apply to requeue them.
    """
    cfg = _resolve_config(ctx.obj["config_path"])
    result = reset_stale_span_embed_jobs(
        duckdb_path=cfg.duckdb_path,
        stale_after_hours=stale_after_hours,
        limit=limit,
        apply=apply,
    )
    mode = "apply" if apply else "dry-run"
    click.echo(
        f"mode={mode} matched={result['matched']} reset={result['reset']} "
        f"stale_after_hours={result['stale_after_hours']} limit={result['limit']}"
    )


@embeddings_cmd.command(name="reset-stale-sessions")
@click.option(
    "--stale-after-hours", default=24, show_default=True, type=click.IntRange(min=1)
)
@click.option("--limit", default=1000, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--apply",
    is_flag=True,
    help="Actually reset stale running session jobs to pending. Default is dry-run.",
)
@click.pass_context
def embeddings_reset_stale_sessions_cmd(
    ctx: click.Context, stale_after_hours: int, limit: int, apply: bool
) -> None:
    """Reset stranded running session embedding jobs back to pending.

    Safe operator flow: run without --apply first to preview the number of
    stale running jobs, then rerun with --apply to requeue them.
    """
    cfg = _resolve_config(ctx.obj["config_path"])
    result = reset_stale_session_embed_jobs(
        duckdb_path=cfg.duckdb_path,
        stale_after_hours=stale_after_hours,
        limit=limit,
        apply=apply,
    )
    mode = "apply" if apply else "dry-run"
    click.echo(
        f"mode={mode} matched={result['matched']} reset={result['reset']} "
        f"stale_after_hours={result['stale_after_hours']} limit={result['limit']}"
    )


@decisions_cmd.command(name="derive")
@click.pass_context
def decisions_derive_cmd(ctx: click.Context) -> None:
    """Derive decisions from explicitly marked span attributes."""
    cfg = _resolve_config(ctx.obj["config_path"])
    inserted = derive_decisions(
        duckdb_path=cfg.duckdb_path, parquet_dir=cfg.parquet_dir
    )
    noun = "decision" if inserted == 1 else "decisions"
    click.echo(f"inserted {inserted} {noun}")


@context_cmd.command(name="validate")
@click.argument("bundle_path", type=click.Path(path_type=Path, exists=True))
def context_validate_cmd(bundle_path: Path) -> None:
    """Validate Markdown/YAML context bundle files without a live Drover server."""
    result = load_bundle(bundle_path)
    if result.issues:
        raise click.ClickException(format_validation_issues(result.issues))
    click.echo(
        f"context validate ok files={result.scanned_files} records={len(result.entries)}"
    )


@context_cmd.command(name="diff")
@click.argument("bundle_path", type=click.Path(path_type=Path, exists=True))
@click.pass_context
def context_diff_cmd(ctx: click.Context, bundle_path: Path) -> None:
    """Diff curated bundle files against imported Drover curated records."""
    result = load_bundle(bundle_path)
    if result.issues:
        raise click.ClickException(format_validation_issues(result.issues))
    cfg = _resolve_config(ctx.obj["config_path"])
    _bootstrap_if_missing(cfg)
    summary = diff_bundle(entries=result.entries, duckdb_path=cfg.duckdb_path)
    click.echo(format_diff(summary, heading="context diff"))


@context_cmd.command(name="import")
@click.argument("bundle_path", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--apply",
    is_flag=True,
    help="Persist created/updated curated records. Default is dry-run.",
)
@click.pass_context
def context_import_cmd(ctx: click.Context, bundle_path: Path, apply: bool) -> None:
    """Dry-run or apply curated bundle imports into Drover curated tables."""
    result = load_bundle(bundle_path)
    if result.issues:
        raise click.ClickException(format_validation_issues(result.issues))
    cfg = _resolve_config(ctx.obj["config_path"])
    _bootstrap_if_missing(cfg)
    outcome = import_bundle(
        entries=result.entries, duckdb_path=cfg.duckdb_path, apply=apply
    )
    click.echo(
        format_diff(outcome["summary"], heading=f"context import ({outcome['mode']})")
    )
    click.echo(
        f"applied={outcome['applied']} provenance_rows={outcome['provenance_rows']}"
    )


@main.group(name="ledger")
def ledger_cmd() -> None:
    """Reconcile and replay durable pipeline-ledger jobs."""


@ledger_cmd.command(name="reconcile")
@click.option(
    "--job-kind",
    "job_kinds",
    multiple=True,
    type=click.Choice(sorted(ledger_shadow.SERVING_JOBS)),
    help="Limit to these job kinds (default: all).",
)
@click.option(
    "--stale-after-hours",
    default=None,
    type=click.IntRange(min=0),
    help="Only reclaim leases/running rows older than this. Default: all in-flight.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Actually reclaim leases and reset serving rows. Default is dry-run.",
)
@click.pass_context
def ledger_reconcile_cmd(
    ctx: click.Context,
    job_kinds: tuple[str, ...],
    stale_after_hours: Optional[int],
    apply: bool,
) -> None:
    """Recover crashed in-flight jobs from DuckDB back to runnable.

    Reclaims stale ledger leases (closing the crashed attempt append-only) and
    resets the matching serving rows from ``running`` to ``pending``. Run without
    ``--apply`` first to preview the counts.
    """
    cfg = _resolve_config(ctx.obj["config_path"])
    kinds = list(job_kinds) or sorted(ledger_shadow.SERVING_JOBS)
    stale_before = (
        datetime.now(timezone.utc) - timedelta(hours=stale_after_hours)
        if stale_after_hours is not None
        else None
    )
    mode = "apply" if apply else "dry-run"
    click.echo(f"ledger reconcile ({mode})")
    if apply:
        bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
        for kind in kinds:
            res = ledger_shadow.recover_runnable(
                cfg.duckdb_path, job_kind=kind, stale_before=stale_before
            )
            click.echo(
                f"  {kind}: serving_reset={res['serving_reset']} "
                f"leases_reclaimed={len(res['leases_reclaimed'])}"
            )
        return

    with _diagnostic_db_path(cfg.duckdb_path) as db_path:
        for kind in kinds:
            pending = _reconcile_preview(db_path, kind, stale_before)
            click.echo(
                f"  {kind}: serving_running={pending['serving_running']} "
                f"leased={pending['leased']} (would reset)"
            )


def _reconcile_preview(
    duckdb_path: Path, job_kind: str, stale_before: Optional[datetime]
) -> dict[str, int]:
    """Count in-flight serving/ledger rows a reconcile would reset (read-only)."""
    from drover.server.ledger import Ledger

    binding = ledger_shadow.SERVING_JOBS.get(job_kind)
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        leased = len(
            Ledger(con).list_leased_jobs(job_kind=job_kind, stale_before=stale_before)
        )
        serving_running = 0
        if binding is not None:
            where = "status='running'"
            params: list[Any] = []
            if stale_before is not None:
                where += " AND updated_at < ?"
                params.append(stale_before)
            serving_running = con.execute(
                f"SELECT count(*) FROM {binding.table} WHERE {where}", params
            ).fetchone()[0]
    finally:
        con.close()
    return {"serving_running": int(serving_running), "leased": leased}


@ledger_cmd.command(name="replay")
@click.option(
    "--job-kind",
    required=True,
    type=click.Choice(sorted(ledger_shadow.SERVING_JOBS)),
)
@click.option("--subject", "subject_key", required=True, help="Subject key to replay.")
@click.option(
    "--apply",
    is_flag=True,
    help="Actually promote the job to pending. Default is dry-run.",
)
@click.pass_context
def ledger_replay_cmd(
    ctx: click.Context, job_kind: str, subject_key: str, apply: bool
) -> None:
    """Promote a finished ledger job back to pending without duplicating rows.

    Regenerates the artifact for ``--subject`` by opening a fresh job generation
    (prior winner superseded, attempt lineage preserved) and resetting the
    subject's single serving row to ``pending``.
    """
    cfg = _resolve_config(ctx.obj["config_path"])
    if apply:
        bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
        res = ledger_shadow.replay(
            cfg.duckdb_path, job_kind=job_kind, subject_key=subject_key, apply=True
        )
    else:
        with _diagnostic_db_path(cfg.duckdb_path) as db_path:
            res = ledger_shadow.replay(
                db_path, job_kind=job_kind, subject_key=subject_key, apply=False
            )
    mode = "apply" if apply else "dry-run"
    click.echo(
        f"ledger replay ({mode}) {job_kind}/{subject_key}: "
        f"ledger_status={res['ledger_status']} eligible={res['eligible']} "
        f"serving_reset={res['serving_reset']}"
    )
    if res["ledger_status"] is None:
        click.echo("  no ledger job found for that subject")
    elif not res["eligible"]:
        click.echo("  not eligible (job is leased / in flight — reconcile first)")


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Write a default config file at --config (defaults to ~/.drover/config.toml)."""
    p = Path(ctx.obj["config_path"]) if ctx.obj["config_path"] else _DEFAULT_CONFIG_PATH
    if p.exists():
        click.echo(f"config already exists: {p}", err=True)
        sys.exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    home = os.path.expanduser("~")
    default_agent_id = os.uname().nodename.split(".")[0] + "-agent"
    p.write_text(
        _DEFAULT_CONFIG_TEMPLATE.format(home=home, default_agent_id=default_agent_id)
    )
    click.echo(f"wrote {p}")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print effective config and table row counts."""
    cfg = _resolve_config(ctx.obj["config_path"])
    _bootstrap_if_missing(cfg)

    click.echo(textwrap.dedent(f"""\
        drover-server status
        ===================
        incoming_dir : {cfg.incoming_dir}
        parquet_dir  : {cfg.parquet_dir}
        duckdb_path  : {cfg.duckdb_path}
        control_plane_duckdb_path : {control_plane_path(cfg.duckdb_path)}
        otlp_grpc_port : {cfg.otlp_grpc_port}
        mcp_http_port  : {cfg.mcp_http_port}
        metrics_http_port : {cfg.metrics_http_port}
        agent_id     : {cfg.agent_id}
        principal_id : {cfg.principal_id}
    """))

    with _diagnostic_db_path(cfg.duckdb_path) as db_path:
        con = open_duckdb_connection(db_path, read_only=True, role="diagnostic")
        try:
            for t in EXPECTED_TABLES:
                try:
                    n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except duckdb.Error as e:
                    n = f"error: {e}"
                click.echo(f"  {t:20s} {n}")
            for v in ("agent_events", "spans", "pr_events", "routing"):
                try:
                    n = con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
                except duckdb.Error as e:
                    n = f"error: {e}"
                click.echo(f"  {v:20s} {n}")
        finally:
            con.close()

    # Counted separately because they live in a separate database (#95).
    try:
        with control_plane_connection(cfg.duckdb_path) as con:
            for t in CONTROL_PLANE_TABLES:
                try:
                    n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except duckdb.Error as e:
                    n = f"error: {e}"
                click.echo(f"  {t:20s} {n}")
    except duckdb.Error as exc:
        click.echo(f"  control plane        error: {exc}")


@main.command(name="export-bundle")
@click.option(
    "--repo-owner",
    default=None,
    help="Repository owner for task-scoped export (required unless --task-id missing).",
)
@click.option(
    "--repo-name",
    default=None,
    help="Repository name for task-scoped export (required unless --task-id missing).",
)
@click.option(
    "--branch", default=None, help="Repository branch for task-scoped export."
)
@click.option(
    "--task-id",
    default=None,
    help="16-char task hash. If omitted, computed from repo_owner/repo_name/branch.",
)
@click.option(
    "--session-id",
    default=None,
    help="Build a session-focused export for this session.",
)
@click.option(
    "--max-summaries",
    default=3,
    show_default=True,
    type=click.IntRange(min=1),
    help="How many summaries to include for task-scoped output.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "yaml"]),
    default="markdown",
    show_default=True,
)
@click.option(
    "--with-events",
    is_flag=True,
    help="Include session replay events when exporting a session ID.",
)
@click.option(
    "--session-events",
    default=30,
    type=click.IntRange(min=1),
    show_default=True,
    help="How many events to include when --with-events is set.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional output path. Defaults to stdout.",
)
@click.pass_context
def export_bundle_cmd(
    ctx: click.Context,
    repo_owner: Optional[str],
    repo_name: Optional[str],
    branch: Optional[str],
    task_id: Optional[str],
    session_id: Optional[str],
    max_summaries: int,
    output_format: str,
    with_events: bool,
    session_events: int,
    output_path: Optional[Path],
) -> None:
    """Export markdown or YAML handoff bundles for a task or session."""
    if not session_id and not task_id and not (repo_owner and repo_name):
        raise click.ClickException(
            "either --session-id or one of: --task-id | (--repo-owner and --repo-name)"
        )

    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)

    if session_id:
        bundle = _build_session_bundle(
            duckdb_path=cfg.duckdb_path,
            session_id=session_id,
            with_events=with_events,
            session_events=session_events,
            max_summaries=max_summaries,
        )
    else:
        if not repo_owner or not repo_name:
            raise click.ClickException(
                "--repo-owner and --repo-name are required when --session-id is not set"
            )
        bundle = _build_task_bundle(
            duckdb_path=cfg.duckdb_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            task_id=task_id,
            max_summaries=max_summaries,
        )

    if output_format == "yaml":
        text = _yaml_dump(bundle)
    else:
        text = _format_bundle_markdown(bundle)

    if output_path is None:
        click.echo(text)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text)
        click.echo(f"wrote {output_path}")


@main.command()
@click.option("--no-otlp", is_flag=True, help="Skip starting the OTLP gRPC receiver")
@click.option(
    "--otlp-host",
    default="127.0.0.1",
    show_default=True,
    help="OTLP bind host (set explicitly for a trusted private network)",
)
@click.option("--no-mcp", is_flag=True, help="Skip starting the MCP HTTP server")
@click.option(
    "--mcp-host",
    default="127.0.0.1",
    show_default=True,
    help="MCP HTTP bind host (set explicitly for a trusted private network)",
)
@click.option(
    "--no-metrics", is_flag=True, help="Skip starting the metrics HTTP server"
)
@click.option(
    "--metrics-host",
    default=None,
    help=(
        "Cockpit/metrics HTTP bind host. Overrides [server] metrics_host in "
        "config.toml, which defaults to 127.0.0.1."
    ),
)
@click.option(
    "--no-summarizer", is_flag=True, help="Skip starting the summarizer worker"
)
@click.option(
    "--no-briefs", is_flag=True, help="Skip starting the project-brief worker"
)
@click.option(
    "--no-embeddings", is_flag=True, help="Skip starting the embeddings worker"
)
@click.pass_context
def run(
    ctx: click.Context,
    no_otlp: bool,
    otlp_host: str,
    no_mcp: bool,
    mcp_host: str,
    no_metrics: bool,
    metrics_host: Optional[str],
    no_summarizer: bool,
    no_briefs: bool,
    no_embeddings: bool,
) -> None:
    """Run the watcher + OTLP + MCP + summarizer (foreground).  Ctrl-C to stop."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    # After bootstrap, which is what creates and migrates the control-plane
    # store, and before any worker starts, so the one moment the control plane
    # touches a connect lock is a moment when nothing is scanning. Pins the
    # control-plane store, not the lakehouse. Off by default -- it would hold
    # DuckDB's file lock against a co-resident harnessd, which shares that
    # store -- and db.py logs which way it went. The lock split and the
    # separate database in control_plane_connection apply regardless (#95).
    pin_control_plane_connection(cfg.duckdb_path)
    # Snapshot copies are only cleaned up when a process exits gracefully, and a
    # hub that is killed or restarted by launchd does not. They accumulated
    # across every restart until the volume ran out (#171), so startup is where
    # the previous process's leavings get collected. Anything recent belongs to
    # a co-resident process still using it and is left alone.
    sweep_orphaned_snapshot_scratch(cfg.duckdb_path)

    # An explicit --metrics-host wins; otherwise the bind comes from
    # [server] metrics_host in config.toml. Keeping it in config matters
    # because a service unit's argv is one regenerated unit away from
    # silently reverting the server to loopback, and that failure is
    # invisible until the app stops loading any screen.
    if metrics_host is None:
        metrics_host = cfg.server_metrics_host
    log.info("cockpit HTTP binding to %s:%s", metrics_host, cfg.metrics_http_port)

    stop = threading.Event()

    job_streams: dict[str, RedisJobStream] = {}
    if cfg.redis_jobs_enabled:
        try:
            job_streams = _build_redis_job_streams(cfg)
            seeded = _seed_redis_job_streams(
                duckdb_path=cfg.duckdb_path, streams=job_streams
            )
            log.info(
                "Redis job streams enabled url=%s prefix=%s group=%s seeded=%s",
                cfg.redis_jobs_url,
                cfg.redis_jobs_stream_prefix,
                cfg.redis_jobs_group,
                seeded,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "Redis job streams failed to initialize; continuing with DuckDB queues"
            )
            job_streams = {}

    watcher = IncomingWatcher(
        incoming_dir=cfg.incoming_dir,
        parquet_dir=cfg.parquet_dir,
        duckdb_path=cfg.duckdb_path,
        summarize_job_stream=job_streams.get("summarize"),
        # The setting has existed since the beginning; until now nothing read
        # it, and the audit copies grew without bound (9.7GB on this hub).
        retention_days=cfg.processed_retention_days,
        receipt_retention_days=cfg.receipt_retention_days,
    )
    watcher.start()

    advisory_worker: AdvisoryWorker | None = None
    try:
        advisory_analyzers = operational_analyzers()
        advisory_scheduler = AdvisoryScheduler(
            duckdb_path=cfg.duckdb_path,
            analyzer_ids=(item.analyzer_id for item in advisory_analyzers),
            full_review_interval_seconds=cfg.advisory_full_review_interval_seconds,
            source_version_factory=lambda analyzer_id, target_id: operational_snapshot_source_version(
                cfg.duckdb_path, analyzer_id, target_id
            ),
        )
        advisory_worker = AdvisoryWorker(
            duckdb_path=cfg.duckdb_path,
            repository=AdvisoryRepository(cfg.duckdb_path),
            snapshot_factory=lambda analyzer_id, target_id, source_version: load_operational_snapshot(
                cfg.duckdb_path, analyzer_id, target_id, source_version
            ),
        )
        advisory_worker.start(
            analyzers=advisory_analyzers,
            scheduler=advisory_scheduler,
            shutdown_event=stop,
            poll_interval_seconds=cfg.advisory_poll_interval_seconds,
        )
        log.info(
            "advisory worker ready (full_review_interval=%.0fs)",
            cfg.advisory_full_review_interval_seconds,
        )
    except Exception:  # noqa: BLE001
        log.exception("advisory worker failed to start; continuing without it")
        advisory_worker = None

    receiver: OTLPReceiver | None = None
    if not no_otlp:
        try:
            receiver = OTLPReceiver(
                host=otlp_host,
                port=cfg.otlp_grpc_port,
                parquet_dir=cfg.parquet_dir,
                duckdb_path=cfg.duckdb_path,
                span_job_stream=job_streams.get("embed_span"),
            )
            receiver.start()
        except Exception:  # noqa: BLE001
            log.exception("OTLP receiver failed to start; continuing with watcher only")
            receiver = None

    mcp_thread: threading.Thread | None = None
    if not no_mcp:
        try:
            mcp_backend_cfg = SummarizerBackendConfig.from_runtime(
                api_model=cfg.summarizer_api_model,
                backend_policy=cfg.summarizer_backend_policy,
                harness_model=cfg.summarizer_harness_model,
                local_model=cfg.summarizer_local_model,
                local_ollama_url=cfg.summarizer_local_ollama_url or None,
                gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
                gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
                wake_timeout_s=cfg.summarizer_wake_timeout_s,
            )
            mcp = _build_runtime_mcp_server(
                cfg=cfg,
                host=mcp_host,
                backend_config=mcp_backend_cfg,
                summarize_job_stream=job_streams.get("summarize"),
            )

            def _run_mcp() -> None:
                try:
                    mcp.run(transport="streamable-http")
                except Exception:  # noqa: BLE001
                    log.exception("MCP server thread crashed")

            mcp_thread = threading.Thread(
                target=_run_mcp, name="drover-mcp", daemon=True
            )
            mcp_thread.start()
            log.info("MCP server starting on %s:%d", mcp_host, cfg.mcp_http_port)
        except Exception:  # noqa: BLE001
            log.exception("MCP server failed to start; continuing without it")
            mcp_thread = None

    metrics_server = None
    metrics_collector: MetricsCollector | None = None
    provider_refresh: ProviderRefreshLoop | None = None
    config_path = (
        Path(ctx.obj["config_path"]) if ctx.obj["config_path"] else _DEFAULT_CONFIG_PATH
    )
    if not no_metrics and cfg.metrics_http_port > 0:
        try:
            auth = load_auth(cfg)
            # Register the push sender before the HTTP surface starts taking
            # harness events, so the first awaiting transition after boot is
            # already deliverable.
            _configure_push(cfg, auth)
            metrics_collector = MetricsCollector(
                duckdb_path=cfg.duckdb_path,
                incoming_dir=cfg.incoming_dir,
                summarizer_report=summarize_backend_auth(
                    backend_policy=cfg.summarizer_backend_policy,
                    api_model=cfg.summarizer_api_model,
                    harness_model=cfg.summarizer_harness_model,
                    local_model=cfg.summarizer_local_model,
                    local_ollama_url=cfg.summarizer_local_ollama_url or None,
                    gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
                    gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
                ),
                job_streams=job_streams,
                api_token=auth.api_token if auth.enabled else "",
                favorite_cwds=cfg.harness_favorite_cwds,
                advisory_service=InsightsService(
                    cfg.duckdb_path, config_path=config_path
                ),
            )
            provider_usage = ProviderUsageService(
                duckdb_path=cfg.duckdb_path,
                parquet_dir=cfg.parquet_dir,
                api_token=auth.api_token if auth.enabled else None,
                freshness_threshold_seconds=cfg.provider_freshness_threshold_seconds,
            )
            metrics_collector.cockpit_service = CockpitService(
                duckdb_path=cfg.duckdb_path,
                provider_usage=provider_usage,
            )
            # The first cockpit request after a restart pays to open DuckDB and
            # read the parquet views' metadata: measured at 15.6s cold against
            # 3.9-5.3s warm, and the iOS client abandons the whole response at
            # 15s. Whoever asks first should not be the one paying that, so the
            # server pays it itself, off the request path.
            _warm_cockpit(metrics_collector.cockpit_service)
            # Pairing codes live only in this process's memory, which is why
            # `drover-server pair` reaches the hub over loopback rather than
            # minting in-process. A restart invalidates outstanding codes.
            pairing = PairingCodes()

            # The hub decides what the fleet converges on and publishes it on
            # the heartbeat every harnessd already sends. Attached to the
            # collector rather than threaded through, because the only thing
            # that reads it is the registration response.
            if cfg.update_enabled:
                planner = UpdatePlanner(cfg, RuntimeLayout(config_home()))
                metrics_collector.update_planner = planner
                _start_update_checker(planner, cfg, stop)

            metrics_server = start_metrics_server(
                host=metrics_host,
                port=cfg.metrics_http_port,
                collector=metrics_collector,
                auth=auth,
                pairing=pairing,
            )
            _warm_metrics(metrics_collector)
            provider_refresh = ProviderRefreshLoop(
                provider_usage=provider_usage,
                registry=HarnessRegistry(cfg.duckdb_path),
                shutdown_event=stop,
                fetch=metrics_collector.fetch_harness_provider_usage,
                operational_source_version=provider_usage.operational_source_version,
                on_operational_change=lambda host_id, source_version: enqueue_operational_checks(
                    cfg.duckdb_path,
                    target_id=host_id,
                    source_version=source_version,
                ),
            )
            provider_refresh.start()
            log.info(
                "metrics server starting on %s:%d",
                metrics_host,
                cfg.metrics_http_port,
            )
            if auth.enabled:
                click.echo(
                    "metrics API auth: enabled "
                    "(token from DROVER_API_TOKEN / config [auth] / ~/.drover/api_token)"
                )
            else:
                click.echo("metrics API auth: DISABLED via [auth] enabled=false")
        except Exception:  # noqa: BLE001
            log.exception("metrics server failed to start; continuing without it")
            metrics_server = None

    content_advisory_worker: ContentAnalysisWorker | None = None
    try:
        if metrics_collector is None:
            auth = load_auth(cfg)
            # Metrics off, but harnessd's local emit() path still records
            # awaiting transitions through the registry, so push still applies.
            _configure_push(cfg, auth)
            metrics_collector = MetricsCollector(
                duckdb_path=cfg.duckdb_path,
                incoming_dir=cfg.incoming_dir,
                summarizer_report={},
                job_streams=job_streams,
                api_token=auth.api_token if auth.enabled else "",
                favorite_cwds=cfg.harness_favorite_cwds,
                advisory_service=InsightsService(
                    cfg.duckdb_path, config_path=config_path
                ),
            )
        content_backend_cfg = SummarizerBackendConfig.from_runtime(
            api_model=cfg.summarizer_api_model,
            backend_policy=cfg.summarizer_backend_policy,
            harness_model=cfg.summarizer_harness_model,
            local_model=cfg.summarizer_local_model,
            local_ollama_url=cfg.summarizer_local_ollama_url or None,
            gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
            gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
            wake_timeout_s=cfg.summarizer_wake_timeout_s,
        )
        content_advisory_worker = _create_content_analysis_worker(
            cfg=cfg,
            metrics_collector=metrics_collector,
            backend_config=content_backend_cfg,
            consent_reader=lambda: load_config(config_path).advisory_content,
        )
        content_advisory_worker.start(
            shutdown_event=stop,
            poll_interval_seconds=cfg.advisory_poll_interval_seconds,
        )
        log.info(
            "content advisory worker ready (policy=%s)",
            cfg.advisory_content.backend_policy,
        )
    except Exception:  # noqa: BLE001
        log.exception("content advisory worker failed to start; continuing without it")
        content_advisory_worker = None

    summarizer: SummarizerWorker | None = None
    live_recap: LiveRecapWorker | None = None
    if not no_summarizer:
        try:
            backend_cfg = SummarizerBackendConfig.from_runtime(
                api_model=cfg.summarizer_api_model,
                backend_policy=cfg.summarizer_backend_policy,
                harness_model=cfg.summarizer_harness_model,
                local_model=cfg.summarizer_local_model,
                local_ollama_url=cfg.summarizer_local_ollama_url or None,
                gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
                gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
                wake_timeout_s=cfg.summarizer_wake_timeout_s,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "summarizer backend configuration failed; continuing without it"
            )
        else:
            if not _summarizer_backend_available(backend_cfg):
                log.warning(
                    "summarizer not started: no Anthropic creds "
                    "(ANTHROPIC_API_KEY/ANTHROPIC_OAUTH_TOKEN/Claude credentials) "
                    "and no claude-code CLI on PATH — jobs will remain queued"
                )
            else:
                try:
                    summarizer = SummarizerWorker(
                        duckdb_path=cfg.duckdb_path,
                        backend_config=backend_cfg,
                        job_kind="incremental",
                        batch_size=cfg.summarizer_batch_size,
                        job_stream=job_streams.get("summarize"),
                        brief_job_stream=job_streams.get("brief"),
                        embed_job_stream=job_streams.get("embed_session"),
                    )
                    summarizer.start()
                    log.info(
                        "summarizer ready (policy=%s, api=%s, claude_code=%s)",
                        backend_cfg.backend_policy,
                        "yes" if backend_cfg.has_anthropic_creds else "no",
                        "yes" if backend_cfg.has_harness_backend else "no",
                    )
                except Exception:  # noqa: BLE001
                    log.exception("summarizer failed to start; continuing without it")
                    summarizer = None
                try:
                    live_recap = LiveRecapWorker(
                        duckdb_path=cfg.duckdb_path,
                        backend_config=backend_cfg,
                        job_stream=job_streams.get("live_recap"),
                    )
                    live_recap.start()
                    log.info("live recap worker ready")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "live recap worker failed to start; continuing without it"
                    )
                    live_recap = None

    embeddings: EmbedWorker | None = None
    if not no_embeddings:
        try:
            backend_cfg = SummarizerBackendConfig.from_runtime(
                api_model=cfg.summarizer_api_model,
                backend_policy=cfg.summarizer_backend_policy,
                harness_model=cfg.summarizer_harness_model,
                local_model=cfg.summarizer_local_model,
                local_ollama_url=cfg.summarizer_local_ollama_url or None,
                gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
                gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
                wake_timeout_s=cfg.summarizer_wake_timeout_s,
            )
            embeddings_cfg = EmbeddingBackendConfig.from_runtime(
                api_base_url=cfg.embeddings_api_base_url or None,
                api_key=cfg.embeddings_api_key or None,
                api_model=cfg.embeddings_api_model or None,
                mac_ollama_url=cfg.embeddings_mac_ollama_url or None,
                gpu_rig=backend_cfg.gpu_rig,
                local_model=cfg.embeddings_local_model or None,
            )
            embeddings = EmbedWorker(
                duckdb_path=cfg.duckdb_path,
                backend_config=backend_cfg,
                embedding_config=embeddings_cfg,
                session_job_stream=job_streams.get("embed_session"),
                span_job_stream=job_streams.get("embed_span"),
            )
            embeddings.start()
            if (
                not embeddings_cfg.has_api_embedder
                and not embeddings_cfg.mac_ollama_url
                and backend_cfg.gpu_rig is None
            ):
                log.warning(
                    "embed worker started but no API, Mac-local Ollama, or GPU rig configured — embed jobs will stay pending"
                )
            else:
                log.info(
                    "embed worker ready (api=%s, mac_local=%s, gpu=%s)",
                    "yes" if embeddings_cfg.has_api_embedder else "no",
                    "yes" if embeddings_cfg.mac_ollama_url else "no",
                    "yes" if backend_cfg.gpu_rig else "no",
                )
        except Exception:  # noqa: BLE001
            log.exception("embed worker failed to start; continuing without it")
            embeddings = None

    briefs: BriefWorker | None = None
    if not no_briefs:
        try:
            backend_cfg = SummarizerBackendConfig.from_runtime(
                api_model=cfg.summarizer_api_model,
                backend_policy=cfg.summarizer_backend_policy,
                harness_model=cfg.summarizer_harness_model,
                local_model=cfg.summarizer_local_model,
                local_ollama_url=cfg.summarizer_local_ollama_url or None,
                gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
                gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
                wake_timeout_s=cfg.summarizer_wake_timeout_s,
            )
            if not _summarizer_backend_available(backend_cfg):
                log.warning(
                    "brief worker not started: no Anthropic creds "
                    "and no claude-code CLI on PATH"
                )
            else:
                briefs = BriefWorker(
                    duckdb_path=cfg.duckdb_path,
                    backend_config=backend_cfg,
                    job_stream=job_streams.get("brief"),
                )
                briefs.start()
        except Exception:  # noqa: BLE001
            log.exception("brief worker failed to start; continuing without it")
            briefs = None

    def _on_signal(signum, _frame):
        log.info("received signal %d; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        stop.wait()
    finally:
        if content_advisory_worker is not None:
            content_advisory_worker.join(timeout=10.0)
        if advisory_worker is not None:
            advisory_worker.join(timeout=10.0)
        if briefs is not None:
            briefs.stop()
        if embeddings is not None:
            embeddings.stop()
        if summarizer is not None:
            summarizer.stop()
        if live_recap is not None:
            live_recap.stop()
        if metrics_server is not None:
            metrics_server.shutdown()
        if receiver is not None:
            receiver.stop()
        watcher.stop()
        # Last, so nothing is still reading through it. A pin outlives every
        # worker by design, and a restart would otherwise race the database
        # lock against its own predecessor.
        close_control_plane_connections()


@main.command()
@click.option(
    "--apply",
    "apply_sweep",
    is_flag=True,
    help="Reclaim the processed spool now. Never touches anything else.",
)
@click.pass_context
def reclaim(ctx: click.Context, apply_sweep: bool) -> None:
    """Report reclaimable disk, and optionally sweep the processed spool.

    Two kinds of space live under the Drover home and they are not the same
    kind of thing. The processed spool is Drover's own audit copies, governed
    by `processed_retention_days`, and Drover sweeps it. Everything else here
    belongs to whoever made it -- build output, test scratch, hand-made
    backups -- and is only reported, because deleting an operator's files
    because they are large is not a decision this command gets to make.
    """

    cfg = _resolve_config(ctx.obj["config_path"])
    home = cfg.incoming_dir.parent

    reclaimable_files = 0
    reclaimable_bytes = 0
    if cfg.processed_retention_days > 0:
        cutoff = time.time() - cfg.processed_retention_days * 86400
        for path in Path(cfg.incoming_dir).glob("*/.processed/*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file() and stat.st_mtime < cutoff:
                reclaimable_files += 1
                reclaimable_bytes += stat.st_size

    click.echo("Drover-owned (governed by processed_retention_days):")
    click.echo(
        f"  processed spool   {reclaimable_bytes / 1_073_741_824:8.2f} GB "
        f"in {reclaimable_files} file(s) older than {cfg.processed_retention_days}d"
    )

    if apply_sweep:
        result = sweep_processed(
            cfg.incoming_dir, retention_days=cfg.processed_retention_days
        )
        click.echo(
            f"  reclaimed         {result.bytes / 1_073_741_824:8.2f} GB "
            f"in {result.files} file(s)"
        )

    click.echo("\nOwned by you (reported only, never deleted):")
    for name in ("worktrees", "DerivedData", "backups"):
        target = home / name
        if target.exists():
            click.echo(f"  {name:17} {_dir_size(target) / 1_073_741_824:8.2f} GB")
    for target in sorted(home.glob("pytest-*")) + sorted(home.glob("parquet-backup-*")):
        click.echo(f"  {target.name:17} {_dir_size(target) / 1_073_741_824:8.2f} GB")


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Audit the lakehouse and print a row-count + drift report."""
    cfg = _resolve_config(ctx.obj["config_path"])
    _bootstrap_if_missing(cfg)
    with _diagnostic_db_path(cfg.duckdb_path) as db_path:
        report = audit_lakehouse(
            parquet_dir=cfg.parquet_dir,
            duckdb_path=db_path,
            incoming_dir=cfg.incoming_dir,
        )
    click.echo("drover-server doctor")
    click.echo("===================")
    click.echo(f"  agent_events     : {report['agent_events_total']:>10d}")
    click.echo(f"  spans            : {report['spans_total']:>10d}")
    click.echo(f"  sessions         : {report['sessions_total']:>10d}")
    click.echo(f"  tasks            : {report['tasks_total']:>10d}")
    click.echo(f"  session_summaries: {report['summaries_total']:>10d}")
    if report["agent_events_by_partition"]:
        click.echo("\nper-(date, agent_id):")
        for (date, agent), n in sorted(report["agent_events_by_partition"].items()):
            click.echo(f"  {date}  {agent or '?':25s} {n:>10d}")
    if report["processed_files"]:
        click.echo("\nprocessed manifests:")
        for host, n in sorted(report["processed_files"].items()):
            click.echo(f"  {host:25s} {n:>10d}")
    if report["warnings"]:
        click.echo("\nwarnings:")
        for w in report["warnings"]:
            click.echo(f"  ⚠ {w}")
    else:
        click.echo("\nno warnings")


@main.command()
@click.option(
    "--dedup-column",
    default="dedup_key",
    show_default=True,
    help="Column to dedup on while compacting (use '' to disable dedup)",
)
@click.pass_context
def compact(ctx: click.Context, dedup_column: str) -> None:
    """Combine small parquet files within each leaf partition."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    dedup = dedup_column or None
    summary = compact_table(cfg.parquet_dir, dedup_column=dedup)
    click.echo(
        f"compacted {summary['partitions']} partitions: "
        f"{summary['files_before']} → {summary['files_after']} files, "
        f"{summary['rows']} rows"
    )


def _date_strings_between(start: datetime, end: datetime) -> list[str]:
    """Return UTC date partitions touched by [start, end]."""
    dates: list[str] = []
    day = start.date()
    last = end.date()
    while day <= last:
        dates.append(day.isoformat())
        day += timedelta(days=1)
    return dates


def _recent_span_rows(
    *,
    duckdb_path: Path,
    parquet_dir: Path,
    since: datetime,
    limit: int,
    service: Optional[str],
    agent: Optional[str],
    trace_id: Optional[str],
    name_contains: Optional[str],
) -> list[dict[str, Any]]:
    """Read recent spans using bounded date-partition macros, never broad spans scans."""
    if limit < 1:
        raise click.UsageError("--limit must be >= 1")

    now = datetime.now(timezone.utc)
    partitions = [
        date
        for date in _date_strings_between(since.astimezone(timezone.utc), now)
        if any((parquet_dir / "spans" / f"date={date}").glob("*.parquet"))
    ]
    if not partitions:
        return []
    union_sql = "\nUNION ALL\n".join(
        "SELECT * FROM spans_for_date(?)" for _ in partitions
    )
    where = ["start_time >= ?"]
    params: list[Any] = [*partitions, since]
    if service:
        where.append("service_name = ?")
        params.append(service)
    if agent:
        where.append("agent_id = ?")
        params.append(canonicalize(agent))
    if trace_id:
        where.append("trace_id = ?")
        params.append(trace_id)
    if name_contains:
        where.append("name ILIKE ?")
        params.append(f"%{name_contains}%")
    params.append(limit)

    sql = f"""
WITH recent_spans AS (
{union_sql}
)
SELECT
  trace_id,
  span_id,
  parent_span_id,
  name,
  service_name,
  start_time,
  end_time,
  duration_ms,
  session_id,
  task_id,
  agent_id,
  cost_usd
FROM recent_spans
WHERE {' AND '.join(where)}
ORDER BY start_time DESC NULLS LAST
LIMIT ?
"""
    con = open_duckdb_connection(duckdb_path, read_only=True, role="diagnostic")
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _format_span_row(row: dict[str, Any]) -> str:
    start = row.get("start_time")
    if isinstance(start, datetime):
        start_s = start.isoformat(timespec="seconds")
    else:
        start_s = str(start or "?")
    duration = row.get("duration_ms")
    duration_s = "?ms" if duration is None else f"{float(duration):.1f}ms"
    service = row.get("service_name") or "?"
    agent = row.get("agent_id") or "?"
    name = row.get("name") or "?"
    trace = row.get("trace_id") or "?"
    span = row.get("span_id") or "?"
    session = row.get("session_id") or "-"
    task = row.get("task_id") or "-"
    return (
        f"{start_s} {duration_s:>10s} {service} {agent} {name} "
        f"trace={trace} span={span} session={session} task={task}"
    )


def _yaml_dump(value: Any, indent: int = 0) -> str:
    """Render a small object as YAML (good enough for CLI bundle export artifacts)."""
    spacer = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines: list[str] = []
        for key, item in value.items():
            child = _yaml_dump(item, indent + 1)
            if "\n" in child:
                lines.append(f"{spacer}{key}:")
                lines.extend(child.splitlines())
            else:
                lines.append(f"{spacer}{key}: {child}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            child = _yaml_dump(item, indent + 1)
            if "\n" in child:
                lines.append(f"{spacer}-")
                lines.extend(child.splitlines())
            else:
                lines.append(f"{spacer}- {child}")
        return "\n".join(lines)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, tuple):
        return _yaml_dump(list(value), indent)
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    return json.dumps(str(value))


def _build_task_bundle(
    *,
    duckdb_path: Path,
    repo_owner: Optional[str],
    repo_name: Optional[str],
    branch: Optional[str],
    task_id: Optional[str],
    max_summaries: int,
) -> dict[str, Any]:
    """Build a project/task handoff bundle from existing MCP-backed readers."""
    tid = task_id or compute_task_id(None, repo_owner, repo_name, branch)
    payload = mcp_tools.drover_handoff(
        duckdb_path=duckdb_path,
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
        task_id=tid,
        max_summaries=max_summaries,
    )
    try:
        files_payload = mcp_tools.drover_files_touched(
            duckdb_path=duckdb_path, task_id=tid
        )
    except Exception:
        files_payload = {"task_id": tid, "files": []}
    project_payload = None
    if repo_owner and repo_name:
        project_payload = mcp_tools.drover_project_brief(
            duckdb_path=duckdb_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
        )
    return {
        "kind": "project",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "task_id": tid,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "branch": branch,
        },
        "project_brief": project_payload,
        "summaries": payload.get("summaries", []),
        "active_sessions": payload.get("active_sessions", []),
        "files_touched": files_payload.get("files", []),
        "source": {
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "branch": branch,
        },
    }


def _build_session_bundle(
    *,
    duckdb_path: Path,
    session_id: str,
    with_events: bool,
    session_events: int,
    max_summaries: int,
) -> dict[str, Any]:
    """Build a session-centric handoff bundle; include task context when available."""
    summary = mcp_tools.drover_session_summary(
        duckdb_path=duckdb_path, session_id=session_id
    )
    if summary is None:
        raise click.ClickException(
            f"no session_summary exists for session_id={session_id!r}; "
            "run a summarize pass before export"
        )

    task_id = summary.get("task_id")
    task_scope = None
    if task_id:
        task_scope = _build_task_bundle(
            duckdb_path=duckdb_path,
            repo_owner=summary.get("repo_owner"),
            repo_name=summary.get("repo_name"),
            branch=summary.get("branch"),
            task_id=task_id,
            max_summaries=max_summaries,
        )

    events = []
    if with_events:
        events_payload = mcp_tools.drover_session_replay(
            duckdb_path=duckdb_path, session_id=session_id, last_n_turns=session_events
        )
        events = events_payload.get("events", [])

    return {
        "kind": "session",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "summary": summary,
        "events": events,
        "task_context": task_scope,
    }


def _format_bundle_markdown(bundle: dict[str, Any]) -> str:
    """Render a compact markdown handoff artifact for agent review."""
    kind = bundle.get("kind")
    lines = ["# Drover context bundle", ""]
    lines.append(f"Kind: `{kind}`")
    lines.append(f"Generated: `{bundle.get('generated_at')}`")
    lines.append("")
    scope = bundle.get("scope", {})
    if kind == "project":
        lines.append("## Scope")
        task_id = scope.get("task_id")
        project = "unknown"
        if scope.get("repo_owner") and scope.get("repo_name"):
            project = f"{scope['repo_owner']}/{scope['repo_name']}"
        branch = scope.get("branch")
        lines.append(f"- Project: `{project}`")
        if branch:
            lines.append(f"- Branch: `{branch}`")
        if task_id:
            lines.append(f"- Task: `{task_id}`")
        lines.append("")
        brief = bundle.get("project_brief")
        if brief:
            lines.append("## Project brief")
            lines.append(brief.get("brief_md") or "(no brief text)")
            lines.append("")
            if brief.get("next_steps_md"):
                lines.append("### Next steps")
                lines.append(brief["next_steps_md"])
                lines.append("")
        files = bundle.get("files_touched") or []
        if files:
            lines.append("## Files touched")
            for path in files:
                lines.append(f"- `{path}`")
            lines.append("")
        lines.append("## Recent summaries")
        for summary in bundle.get("summaries", []):
            lines.append(
                f"### {summary.get('agent_id', 'agent')} · "
                f"`{summary.get('session_id', '-')}`"
            )
            lines.append(f"- Ended: `{summary.get('ended_at')}`")
            if summary.get("files_touched"):
                lines.append(
                    "- Files: "
                    + ", ".join(f"`{path}`" for path in summary["files_touched"])
                )
            if summary.get("summary_md"):
                lines.append("")
                lines.append(summary["summary_md"])
            if summary.get("next_steps_md"):
                lines.append("")
                lines.append(f"**Next steps:** {summary['next_steps_md']}")
            if summary.get("open_questions"):
                lines.append("")
                lines.append("**Open questions:**")
                for question in summary["open_questions"]:
                    lines.append(f"- {question}")
            lines.append("")
        active = bundle.get("active_sessions", [])
        if active:
            lines.append("## Active sessions")
            for session in active:
                lines.append(
                    f"- `{session.get('session_id', '-')}` (agent {session.get('agent_id', '?')})"
                )
        return "\n".join(lines) + "\n"

    lines.append("## Session")
    summary = bundle.get("summary", {})
    lines.append(f"- Session: `{bundle.get('session_id')}`")
    lines.append(f"- Task: `{summary.get('task_id') or 'unknown'}`")
    lines.append(f"- Agent: `{summary.get('agent_id', 'unknown')}`")
    lines.append(f"- Ended: `{summary.get('ended_at')}`")
    lines.append("")
    if summary.get("summary_md"):
        lines.append("## Session summary")
        lines.append(summary["summary_md"])
        lines.append("")
    if summary.get("next_steps_md"):
        lines.append("## Session next steps")
        lines.append(summary["next_steps_md"])
        lines.append("")
    if summary.get("open_questions"):
        lines.append("## Open questions")
        for question in summary["open_questions"]:
            lines.append(f"- {question}")
        lines.append("")

    events = bundle.get("events", [])
    if events:
        lines.append("## Replay (most recent first)")
        for event in events:
            role = event.get("role", "unknown")
            ts = event.get("timestamp")
            rows = str(event.get("content", "")).splitlines()
            if rows:
                lines.append(f"- {ts} · {role}: {rows[0]}")
                if len(rows) > 1:
                    for row in rows[1:]:
                        lines.append(f"  {row}")
            else:
                lines.append(f"- {ts} · {role}: [empty]")
        lines.append("")
    return "\n".join(lines) + "\n"


def _trace_tail_impl(
    ctx: click.Context,
    since_minutes: float,
    limit: int,
    service: Optional[str],
    agent: Optional[str],
    trace_id: Optional[str],
    name_contains: Optional[str],
    interval: float,
    count: Optional[int],
    as_json: bool,
) -> None:
    if since_minutes <= 0:
        raise click.UsageError("--since-minutes must be > 0")
    if since_minutes > 7 * 24 * 60:
        raise click.UsageError("--since-minutes is capped at 10080 (7 days)")
    if interval < 0:
        raise click.UsageError("--interval must be >= 0")

    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)

    remaining = 1 if interval == 0 else count
    seen: set[tuple[Any, Any]] = set()
    while True:
        since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        rows = _recent_span_rows(
            duckdb_path=cfg.duckdb_path,
            parquet_dir=cfg.parquet_dir,
            since=since,
            limit=limit,
            service=service,
            agent=agent,
            trace_id=trace_id,
            name_contains=name_contains,
        )
        for row in reversed(rows):
            key = (row.get("trace_id"), row.get("span_id"))
            if interval and key in seen:
                continue
            seen.add(key)
            if as_json:
                click.echo(json.dumps(row, default=_json_default, sort_keys=True))
            else:
                click.echo(_format_span_row(row))

        if remaining is not None:
            remaining -= 1
            if remaining <= 0:
                break
        if interval == 0:
            break
        time.sleep(interval)


def _trace_tail_options(func):
    func = click.option("--json", "as_json", is_flag=True, help="Emit JSON Lines")(func)
    func = click.option(
        "--count",
        type=int,
        default=None,
        help="Polling iterations when --interval is set (default: forever)",
    )(func)
    func = click.option(
        "--interval",
        type=float,
        default=0.0,
        show_default=True,
        help="Poll every N seconds; 0 runs once",
    )(func)
    func = click.option(
        "--name-contains", default=None, help="Substring filter on span name"
    )(func)
    func = click.option("--trace-id", default=None, help="Exact trace_id filter")(func)
    func = click.option("--agent", default=None, help="Exact agent_id filter")(func)
    func = click.option("--service", default=None, help="Exact service_name filter")(
        func
    )
    func = click.option(
        "--limit",
        type=int,
        default=20,
        show_default=True,
        help="Maximum spans to show per query",
    )(func)
    func = click.option(
        "--since-minutes",
        type=float,
        default=60.0,
        show_default=True,
        help="Recent lookback window in minutes (capped at 7 days)",
    )(func)
    return click.pass_context(func)


@main.command(name="trace-tail")
@_trace_tail_options
def trace_tail_cmd(*args, **kwargs) -> None:
    """Show or poll recent spans from bounded date partitions."""
    _trace_tail_impl(*args, **kwargs)


@main.command(name="recent-traces")
@_trace_tail_options
def recent_traces_cmd(*args, **kwargs) -> None:
    """Alias for trace-tail."""
    _trace_tail_impl(*args, **kwargs)


@main.command(name="audit-sessions")
@click.option(
    "--db",
    "duckdb_path",
    type=click.Path(path_type=Path),
    default=None,
    help="DuckDB database path (default: configured paths.duckdb_path)",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def audit_sessions_cmd(
    ctx: click.Context, duckdb_path: Optional[Path], as_json: bool
) -> None:
    """Read-only session consistency audit.

    Exits non-zero when drift is found or when sessions is a legacy base table.
    No repair or backfill is attempted.
    """
    cfg = _resolve_config(ctx.obj["config_path"])
    with _diagnostic_db_path(duckdb_path or cfg.duckdb_path) as db_path:
        report = audit_session_consistency_db(db_path)
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(format_session_audit(report))
    if report.get("status") != "ok":
        sys.exit(2)


@main.command(name="runtime-audit")
@click.option(
    "--db",
    "duckdb_path",
    type=click.Path(path_type=Path),
    default=None,
    help="DuckDB database path (default: configured paths.duckdb_path)",
)
@click.option(
    "--incoming-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Incoming directory to scan (default: configured paths.incoming_dir)",
)
@click.option(
    "--hours",
    default=24,
    show_default=True,
    help="Lookback window for repo attribution percentage",
)
@click.option(
    "--deep",
    is_flag=True,
    help="Include expensive attribution and full session drift diagnostics.",
)
@click.pass_context
def runtime_audit_cmd(
    ctx: click.Context,
    duckdb_path: Optional[Path],
    incoming_dir: Optional[Path],
    hours: int,
    deep: bool,
) -> None:
    """Read-only operational audit of Drover runtime state."""
    cfg = _resolve_config(ctx.obj["config_path"])
    source_db = duckdb_path or cfg.duckdb_path
    with _diagnostic_db_path(source_db) as db_path:
        diagnostic_db = db_path if Path(db_path) != Path(source_db) else None
        report = runtime_audit(
            duckdb_path=db_path,
            incoming_dir=incoming_dir or cfg.incoming_dir,
            hours=hours,
            source_duckdb_path=source_db,
            diagnostic_db_path=diagnostic_db,
            deep=deep,
        )
    click.echo(format_runtime_audit(report))


@main.command(name="quality")
@click.option(
    "--db",
    "duckdb_path",
    type=click.Path(path_type=Path),
    default=None,
    help="DuckDB database path (default: configured paths.duckdb_path)",
)
@click.option(
    "--incoming-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Incoming directory to scan (default: configured paths.incoming_dir)",
)
@click.option(
    "--hours",
    default=24,
    show_default=True,
    help="Lookback window for quality percentages and freshness checks",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.option(
    "--prometheus",
    "as_prometheus",
    is_flag=True,
    help="Emit Prometheus text exposition",
)
@click.option(
    "--required-agent",
    "required_agent_ids",
    multiple=True,
    help=(
        "Agent ID expected to be actively producing events; may be repeated. "
        "Also accepts DROVER_QUALITY_REQUIRED_AGENTS as comma-separated defaults."
    ),
)
@click.option(
    "--deep",
    is_flag=True,
    help="Include expensive attribution and full session drift diagnostics.",
)
@click.pass_context
def quality_cmd(
    ctx: click.Context,
    duckdb_path: Optional[Path],
    incoming_dir: Optional[Path],
    hours: int,
    as_json: bool,
    as_prometheus: bool,
    required_agent_ids: tuple[str, ...],
    deep: bool,
) -> None:
    """Read-only Drover data-quality snapshot."""
    if as_json and as_prometheus:
        raise click.UsageError("choose only one output mode: --json or --prometheus")
    cfg = _resolve_config(ctx.obj["config_path"])
    with _diagnostic_db_path(duckdb_path or cfg.duckdb_path) as db_path:
        snapshot = quality_snapshot(
            duckdb_path=db_path,
            incoming_dir=incoming_dir or cfg.incoming_dir,
            hours=hours,
            required_agent_ids=required_agent_ids,
            deep=deep,
        )
    if as_prometheus:
        click.echo(format_prometheus(snapshot), nl=False)
    else:
        click.echo(json.dumps(snapshot, indent=2, sort_keys=True))


@main.command(name="observatory")
@click.option(
    "--db",
    "duckdb_path",
    type=click.Path(path_type=Path),
    default=None,
    help="DuckDB database path (default: configured paths.duckdb_path)",
)
@click.option(
    "--incoming-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Incoming directory to scan for the paired quality snapshot",
)
@click.option(
    "--max-artifacts",
    default=10,
    show_default=True,
    help="Maximum saved summaries and briefs to include",
)
@click.option(
    "--max-projects",
    default=10,
    show_default=True,
    help="Maximum project readiness rows to include",
)
@click.pass_context
def observatory_cmd(
    ctx: click.Context,
    duckdb_path: Optional[Path],
    incoming_dir: Optional[Path],
    max_artifacts: int,
    max_projects: int,
) -> None:
    """Read-only Pipeline Observatory artifact and project drilldown."""
    cfg = _resolve_config(ctx.obj["config_path"])
    with _diagnostic_db_path(duckdb_path or cfg.duckdb_path) as db_path:
        quality = quality_snapshot(
            duckdb_path=db_path,
            incoming_dir=incoming_dir or cfg.incoming_dir,
            deep=False,
        )
        payload = pipeline_observatory_snapshot(
            duckdb_path=db_path,
            runtime_audit=quality.get("runtime_audit", {}),
            max_artifacts=max_artifacts,
            max_projects=max_projects,
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@main.command()
@click.option(
    "--project",
    "project_key",
    default=None,
    help="Single project key '<owner>/<name>' (default: all attributed projects with recent activity)",
)
@click.option(
    "--hours",
    default=168,
    show_default=True,
    help="Lookback window in hours when --project is not given",
)
@click.pass_context
def brief(ctx: click.Context, project_key: Optional[str], hours: int) -> None:
    """Enqueue project_briefs for attributed projects."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    if project_key:
        outcome = enqueue_brief(cfg.duckdb_path, project_key)
        click.echo(f"{project_key}: {outcome}")
        return
    results = enqueue_briefs_for_active_projects(cfg.duckdb_path, hours=hours)
    if not results:
        click.echo(f"no attributed projects with activity in last {hours}h")
        return
    for pk, outcome in results:
        click.echo(f"{pk}: {outcome}")


@main.command("summarizer-doctor")
@click.pass_context
def summarizer_doctor(ctx: click.Context) -> None:
    """Print summarizer backend/auth diagnostics without making an LLM call."""
    cfg = _resolve_config(ctx.obj["config_path"])
    report = summarize_backend_auth(
        backend_policy=cfg.summarizer_backend_policy,
        api_model=cfg.summarizer_api_model,
        harness_model=cfg.summarizer_harness_model,
        local_model=cfg.summarizer_local_model,
        local_ollama_url=cfg.summarizer_local_ollama_url or None,
        gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
        gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
    )
    creds = report["auth_sources"]["claude_credentials"]
    click.echo("drover-server summarizer-doctor")
    click.echo("==============================")
    click.echo(f"  API model       : {report['api_model']}")
    click.echo(f"  Harness model   : {report['harness_model']}")
    click.echo(f"  Policy          : {report['backend_policy']}")
    click.echo(f"  Anthropic ready : {'yes' if report['anthropic_ready'] else 'no'}")
    click.echo(
        f"  claude-code CLI : {'yes' if report['harness_cli_present'] else 'no'}"
    )
    click.echo(f"  Harness ready   : {'yes' if report['harness_ready'] else 'no'}")
    click.echo(f"  Effective auth  : {report['effective_auth'] or 'none'}")
    click.echo("\nauth sources:")
    click.echo(
        "  ANTHROPIC_API_KEY   : "
        f"{'present' if report['auth_sources']['ANTHROPIC_API_KEY']['present'] else 'missing'}"
    )
    click.echo(
        "  ANTHROPIC_OAUTH_TOKEN: "
        f"{'present' if report['auth_sources']['ANTHROPIC_OAUTH_TOKEN']['present'] else 'missing'}"
    )
    click.echo(
        "  Claude credentials : "
        f"path={creds['path']} exists={creds['exists']} readable={creds['readable']} "
        f"token_present={creds['token_present']} expired={creds['expired']}"
    )
    if report["warnings"]:
        click.echo("\nwarnings:")
        for warning in report["warnings"]:
            click.echo(f"  ⚠ {warning}")


@main.command("retry-summarize-jobs")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Actually reset matching jobs to pending (default is dry-run)",
)
@click.option(
    "--include-validation",
    is_flag=True,
    help="Also retry JSON/schema validation failures",
)
@click.option(
    "--limit", type=int, default=None, help="Maximum number of errored jobs to match"
)
@click.option(
    "--db",
    "duckdb_path",
    type=click.Path(path_type=Path),
    default=None,
    help="DuckDB database path (default: configured paths.duckdb_path)",
)
@click.pass_context
def retry_summarize_jobs(
    ctx: click.Context,
    apply_changes: bool,
    include_validation: bool,
    limit: Optional[int],
    duckdb_path: Optional[Path],
) -> None:
    """Requeue errored summarize_jobs caused by auth/rate-limit/runtime failures."""
    cfg = _resolve_config(ctx.obj["config_path"])
    db_path = duckdb_path or cfg.duckdb_path
    if duckdb_path is None:
        bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    result = retry_errored_jobs(
        db_path,
        apply=apply_changes,
        include_validation=include_validation,
        limit=limit,
    )
    mode = "apply" if apply_changes else "dry-run"
    click.echo(f"retry-summarize-jobs ({mode})")
    click.echo(f"matched: {result['count']}")
    if result["matched"]:
        click.echo("jobs:")
        for session_id in result["matched"]:
            marker = "updated" if session_id in result["updated"] else "would-reset"
            click.echo(f"  {session_id}  {marker}")
    elif not include_validation:
        click.echo(
            "no retryable auth/rate-limit/runtime errors found (validation errors skipped)"
        )


@main.command()
@click.pass_context
def rollup(ctx: click.Context) -> None:
    """Refresh tasks.session_count, total_cost_usd, and back-filled repo fields."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
    con = open_duckdb_connection(cfg.duckdb_path)
    try:
        n = rollup_tasks(con)
    finally:
        con.close()
    click.echo(f"rolled up {n} task rows")


if __name__ == "__main__":  # pragma: no cover
    main()
