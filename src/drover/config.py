"""Drover runtime configuration.

Single source of truth: ~/.drover/config.toml.  Falls back to sensible
defaults for any missing field so a brand-new install Just Works after
`drover-server init` writes the default file.
"""

from __future__ import annotations

import _thread
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("drover.config")

# How a host activates a version it has downloaded. See `update_activation`.
ACTIVATION_SYMLINK = "symlink"
ACTIVATION_IN_PLACE = "in_place"
ACTIVATION_MODES = (ACTIVATION_SYMLINK, ACTIVATION_IN_PLACE)

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def config_home() -> Path:
    return Path(os.path.expanduser("~/.drover"))


def default_config_file(name: str) -> Path:
    return config_home() / name


def default_config_path() -> Path:
    return default_config_file("config.toml")


def resolve_api_token_env() -> str:
    return os.environ.get("DROVER_API_TOKEN", "").strip()


def default_token_file() -> Path:
    return config_home() / "api_token"


@dataclass(frozen=True)
class AdvisoryContentConfig:
    """Explicit consent and bounds for content-sensitive advisory analysis."""

    enabled: bool
    backend_policy: str
    external_consent: bool
    targets: tuple[str, ...]
    allowed_roots: tuple[Path, ...]
    max_file_bytes: int
    max_bundle_bytes: int
    excerpt_max_chars: int

    def __post_init__(self) -> None:
        for field_name in ("enabled", "external_consent"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"advisory_content.{field_name} must be a boolean")
        if self.backend_policy not in {"local", "cloud"}:
            raise ValueError("advisory_content.backend_policy must be local or cloud")
        if self.backend_policy == "cloud" and not self.external_consent:
            raise ValueError(
                "advisory_content.external_consent must be true for cloud analysis"
            )
        for field_name in (
            "max_file_bytes",
            "max_bundle_bytes",
            "excerpt_max_chars",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"advisory_content.{field_name} must be positive")


@dataclass(frozen=True)
class FavoriteCwd:
    """A "favorite" working directory offered in the New Session sheet.

    ``host_id`` is the host whose filesystem has this path. ``None`` means
    "offer it on every host", which is what a bare string in the config file
    means and is the only shape favorites had before host scoping. On a fleet
    whose hosts do not share a layout, an unscoped favorite is offered for
    hosts that have no such directory, and picking one anchors a session to a
    root that does not exist (see the cwd validation in structured/workspace).
    """

    path: str
    host_id: str | None = None

    @classmethod
    def parse(cls, entry: object) -> "FavoriteCwd | None":
        """Read one config entry, or ``None`` if it names no path.

        Accepts either a bare string or a table with ``path`` and an optional
        ``host_id``; anything else is ignored rather than fatal, so one
        malformed favorite cannot stop the server from starting.
        """
        if isinstance(entry, str):
            path, host_id = entry, ""
        elif isinstance(entry, Mapping):
            path = str(entry.get("path", ""))
            host_id = str(entry.get("host_id", "") or "")
        else:
            return None
        path = path.strip()
        if not path:
            return None
        return cls(path=path, host_id=host_id.strip() or None)


@dataclass(frozen=True)
class DroverConfig:
    incoming_dir: Path
    parquet_dir: Path
    duckdb_path: Path
    processed_retention_days: int
    receipt_retention_days: int
    otlp_grpc_port: int
    mcp_http_port: int
    metrics_http_port: int
    agent_id: str
    principal_id: str
    # Summarizer backend knobs (all optional — sensible fallbacks via env)
    summarizer_backend_policy: str
    summarizer_api_model: str
    summarizer_harness_model: str
    summarizer_local_model: str
    summarizer_local_ollama_url: str  # empty string = disabled; no wake relay
    summarizer_gpu_relay_url: str  # empty string = disabled
    summarizer_gpu_ollama_url: str  # empty string = disabled
    summarizer_mac_ollama_url: str  # empty string = disabled
    summarizer_wake_timeout_s: float
    summarizer_batch_size: int
    # launchd unit backing the Mac-local Ollama; empty string = code default
    summarizer_local_ollama_launchd_label: str
    summarizer_local_ollama_launchd_plist: str
    # Embedding backend knobs: remote API first, Mac-local Ollama, then GPU fallback.
    embeddings_api_base_url: str
    embeddings_api_key: str
    embeddings_api_model: str
    embeddings_mac_ollama_url: str
    embeddings_local_model: str
    # Redis Streams shadow ingestion (off by default; mirrors events, never
    # the source of truth). See drover.server.redis_shadow.
    redis_shadow_enabled: bool
    redis_shadow_url: str
    redis_shadow_stream: str
    redis_shadow_maxlen: int
    # Redis Streams derived-job coordination (off by default). This is the
    # production path for summarize/brief/embed/span worker consumer groups.
    redis_jobs_enabled: bool
    redis_jobs_url: str
    redis_jobs_stream_prefix: str
    redis_jobs_group: str
    redis_jobs_max_deliveries: int
    redis_jobs_visibility_timeout_ms: int
    redis_jobs_maxlen: int
    redis_jobs_high_water: int
    # Central API auth (see drover.server.web.auth). Token resolution order:
    # DROVER_API_TOKEN env > this field > auto-generated ~/.drover/api_token.
    auth_enabled: bool
    auth_api_token: str
    # False stops accepting the one shared cluster token, leaving only
    # per-device credentials. Defaults true so upgrading never locks anyone out.
    auth_legacy_token_enabled: bool
    # Address the pairing QR points clients at. Empty means "not configured";
    # the installer writes the detected private address here.
    server_advertised_url: str
    # Bind address for the cockpit HTTP surface. Lives in config rather than
    # only in --metrics-host, because a service unit's argv is one regenerated
    # unit away from silently reverting the server to loopback, and that
    # failure is invisible until the app stops loading any screen. An
    # explicitly passed --metrics-host still wins.
    server_metrics_host: str
    # Fleet auto-update. The hub picks a target version and hosts converge on
    # it over the heartbeat they already send. A non-empty pinned_version
    # freezes the whole fleet on that version.
    update_enabled: bool
    update_check_interval_hours: int
    update_pinned_version: str
    update_quiesce_timeout_hours: int
    update_keep_versions: int
    update_repo: str
    # How a host puts a downloaded version into service. "symlink" flips
    # runtime/current at the new venv and restarts, which is what every Linux
    # host does and is strictly safer. "in_place" installs the new version
    # into an existing venv instead, for the one host that cannot exec a new
    # one: macOS keys its TCC grant for the external volume to the executable,
    # so a new venv there dies at interpreter startup with EPERM reading its
    # own pyvenv.cfg. Opt-in, and the venv must be named explicitly --
    # guessing one is how the wrong environment gets overwritten.
    update_activation: str
    update_in_place_venv: str
    # Other service units that exec the same venv as this daemon. In-place
    # activation rewrites that venv underneath them, so they are restarted
    # before this process restarts itself. Empty means "nothing else shares
    # it", which is true of every host but the mac-mini hub. Names are the
    # service manager's own: launchd labels on macOS, systemd units on Linux.
    update_restart_units: tuple[str, ...]
    # "Favorite" cwd suggestions surfaced in the New Session sheet, on top of
    # recent-session cwds. Empty by default — set per install, never in code.
    # Each carries the host it belongs to, or None for every host.
    harness_favorite_cwds: tuple[FavoriteCwd, ...]
    # Provider account freshness. Successful identical observations advance
    # this fetch clock without duplicating immutable quota snapshots.
    provider_freshness_threshold_seconds: float
    # Deterministic advisory checks. Content/model analysis remains separately
    # consented and is not enabled by these operational scheduler settings.
    advisory_full_review_interval_seconds: float
    advisory_poll_interval_seconds: float
    advisory_content: AdvisoryContentConfig
    # APNs push for "needs you" transitions. Disabled unless an auth key is
    # present; see the [apns] block in _DEFAULTS.
    apns_enabled: bool
    apns_key_path: str
    apns_key_id: str
    apns_team_id: str
    apns_bundle_id: str


_DEFAULTS = {
    "paths": {
        "incoming_dir": str(config_home() / "incoming"),
        "parquet_dir": str(config_home() / "parquet"),
        "duckdb_path": str(config_home() / "drover.duckdb"),
        "processed_retention_days": 7,
        # Shadow receipts only (agent_event, otlp_span): written by
        # ledger_shadow and read by nothing, with Parquet carrying the
        # authoritative dedup. Seven days to match the spool above, which is
        # the window an operator already has in mind for this store. See #255.
        "receipt_retention_days": 7,
    },
    "server": {
        "otlp_grpc_port": 4317,
        "mcp_http_port": 7077,
        "metrics_http_port": 7080,
        "advertised_url": "",
        # Hardened default: binding beyond loopback must be a deliberate act.
        "metrics_host": "127.0.0.1",
    },
    "agent": {
        "agent_id": "unknown-agent",
        "principal_id": "unknown",
    },
    "summarizer": {
        # harness: summarize through the claude-code CLI already installed and
        # authenticated on the host. No API key, and no local model that cannot
        # hold the response schema.
        "backend_policy": "harness",
        "api_model": "claude-haiku-4-5-20251001",
        "harness_model": "haiku",
        "local_model": "qwen3.5:35b-a3b",
        "local_ollama_url": "",
        "gpu_relay_url": "",
        "gpu_ollama_url": "",
        "mac_ollama_url": "",
        "wake_timeout_s": 120.0,
        "batch_size": 8,
        "local_ollama_launchd_label": "",
        "local_ollama_launchd_plist": "",
    },
    "embeddings": {
        "api_base_url": "",
        "api_key": "",
        "api_model": "text-embedding-3-small",
        "mac_ollama_url": "",
        "local_model": "nomic-embed-text",
    },
    "redis_shadow": {
        "enabled": False,
        "url": "redis://127.0.0.1:6379/0",
        "stream": "drover:events",
        "maxlen": 100000,
    },
    "redis_jobs": {
        "enabled": False,
        "url": "redis://127.0.0.1:6379/0",
        "stream_prefix": "drover:jobs",
        "group": "workers",
        "max_deliveries": 5,
        "visibility_timeout_ms": 60000,
        "maxlen": 100000,
        "high_water": 1000,
    },
    "auth": {
        "enabled": True,
        "api_token": "",
        "legacy_token_enabled": True,
    },
    "update": {
        "enabled": True,
        "check_interval_hours": 6,
        # Non-empty freezes the fleet on that version.
        "pinned_version": "",
        # After this long waiting for a host to go idle, the host reports
        # update_blocked rather than forcing anything. It never interrupts.
        "quiesce_timeout_hours": 6,
        "keep_versions": 2,
        "repo": "arniesaha/drover",
        "activation": ACTIVATION_SYMLINK,
        "in_place_venv": "",
        "restart_units": [],
    },
    "harness": {
        "favorite_cwds": [],
    },
    # Off until the operator drops the .p8 auth key from the Apple developer
    # portal onto the host and fills in the ids beside it. With `enabled` off
    # (or any field blank) the push path is inert and the app falls back to
    # its foreground watcher and BGTask poller.
    "apns": {
        "enabled": False,
        "key_path": "",
        "key_id": "",
        "team_id": "",
        "bundle_id": "com.arnab.drover",
    },
    "provider": {
        "freshness_threshold_seconds": 600.0,
    },
    "advisory": {
        "full_review_interval_seconds": 86400.0,
        "poll_interval_seconds": 5.0,
    },
    "advisory_content": {
        "enabled": False,
        "backend_policy": "local",
        "external_consent": False,
        "targets": [],
        "allowed_roots": [],
        "max_file_bytes": 131072,
        "max_bundle_bytes": 524288,
        "excerpt_max_chars": 320,
    },
}


def _activation_mode(raw) -> str:
    """Normalise `update.activation`, falling back rather than raising.

    A typo here must never stop the daemon starting: the fallback is the
    default every host has always used, so the cost of getting it wrong is an
    update that activates the ordinary way, not a host that is down.
    """
    mode = str(raw or "").strip()
    if mode in ACTIVATION_MODES:
        return mode
    log.warning(
        "unknown update.activation %r; falling back to %r", mode, ACTIVATION_SYMLINK
    )
    return ACTIVATION_SYMLINK


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) for k, v in base.items()}
    for section, values in override.items():
        out.setdefault(section, {}).update(values)
    return out


def _from_dict(d: dict) -> DroverConfig:
    s = d["summarizer"]
    e = d["embeddings"]
    r = d["redis_shadow"]
    j = d["redis_jobs"]
    content = d["advisory_content"]
    provider_freshness_threshold = d["provider"]["freshness_threshold_seconds"]
    if (
        type(provider_freshness_threshold) not in (int, float)
        or not math.isfinite(provider_freshness_threshold)
        or provider_freshness_threshold <= 0
    ):
        raise ValueError(
            "provider.freshness_threshold_seconds must be a finite positive number"
        )
    provider_freshness_threshold_seconds = float(provider_freshness_threshold)
    return DroverConfig(
        incoming_dir=Path(d["paths"]["incoming_dir"]),
        parquet_dir=Path(d["paths"]["parquet_dir"]),
        duckdb_path=Path(d["paths"]["duckdb_path"]),
        processed_retention_days=int(d["paths"]["processed_retention_days"]),
        receipt_retention_days=int(d["paths"].get("receipt_retention_days", 7)),
        otlp_grpc_port=int(d["server"]["otlp_grpc_port"]),
        mcp_http_port=int(d["server"]["mcp_http_port"]),
        metrics_http_port=int(d["server"]["metrics_http_port"]),
        agent_id=d["agent"]["agent_id"],
        principal_id=d["agent"]["principal_id"],
        summarizer_backend_policy=s["backend_policy"],
        summarizer_api_model=s["api_model"],
        summarizer_harness_model=s["harness_model"],
        summarizer_local_model=s["local_model"],
        summarizer_local_ollama_url=s["local_ollama_url"],
        summarizer_gpu_relay_url=s["gpu_relay_url"],
        summarizer_gpu_ollama_url=s["gpu_ollama_url"],
        summarizer_mac_ollama_url=s["mac_ollama_url"],
        summarizer_wake_timeout_s=float(s["wake_timeout_s"]),
        summarizer_batch_size=int(s["batch_size"]),
        summarizer_local_ollama_launchd_label=s["local_ollama_launchd_label"],
        summarizer_local_ollama_launchd_plist=s["local_ollama_launchd_plist"],
        embeddings_api_base_url=e["api_base_url"],
        embeddings_api_key=e["api_key"],
        embeddings_api_model=e["api_model"],
        embeddings_mac_ollama_url=e["mac_ollama_url"],
        embeddings_local_model=e["local_model"],
        redis_shadow_enabled=bool(r["enabled"]),
        redis_shadow_url=r["url"],
        redis_shadow_stream=r["stream"],
        redis_shadow_maxlen=int(r["maxlen"]),
        redis_jobs_enabled=bool(j["enabled"]),
        redis_jobs_url=j["url"],
        redis_jobs_stream_prefix=j["stream_prefix"],
        redis_jobs_group=j["group"],
        redis_jobs_max_deliveries=int(j["max_deliveries"]),
        redis_jobs_visibility_timeout_ms=int(j["visibility_timeout_ms"]),
        redis_jobs_maxlen=int(j["maxlen"]),
        redis_jobs_high_water=int(j["high_water"]),
        auth_enabled=bool(d["auth"]["enabled"]),
        auth_api_token=d["auth"]["api_token"],
        auth_legacy_token_enabled=bool(d["auth"]["legacy_token_enabled"]),
        server_advertised_url=str(d["server"]["advertised_url"]),
        server_metrics_host=str(d["server"]["metrics_host"]),
        update_enabled=bool(d["update"]["enabled"]),
        update_check_interval_hours=int(d["update"]["check_interval_hours"]),
        update_pinned_version=str(d["update"]["pinned_version"]).strip().lstrip("v"),
        update_quiesce_timeout_hours=int(d["update"]["quiesce_timeout_hours"]),
        update_keep_versions=int(d["update"]["keep_versions"]),
        update_repo=str(d["update"]["repo"]),
        update_activation=_activation_mode(d["update"]["activation"]),
        update_in_place_venv=str(d["update"]["in_place_venv"]).strip(),
        update_restart_units=tuple(
            name
            for name in (str(entry).strip() for entry in d["update"]["restart_units"])
            if name
        ),
        harness_favorite_cwds=tuple(
            favorite
            for favorite in (
                FavoriteCwd.parse(entry) for entry in d["harness"]["favorite_cwds"]
            )
            if favorite is not None
        ),
        provider_freshness_threshold_seconds=provider_freshness_threshold_seconds,
        advisory_full_review_interval_seconds=float(
            d["advisory"]["full_review_interval_seconds"]
        ),
        advisory_poll_interval_seconds=float(d["advisory"]["poll_interval_seconds"]),
        advisory_content=AdvisoryContentConfig(
            enabled=content["enabled"],
            backend_policy=str(content["backend_policy"]),
            external_consent=content["external_consent"],
            targets=tuple(str(target) for target in content["targets"]),
            allowed_roots=tuple(Path(root) for root in content["allowed_roots"]),
            max_file_bytes=int(content["max_file_bytes"]),
            max_bundle_bytes=int(content["max_bundle_bytes"]),
            excerpt_max_chars=int(content["excerpt_max_chars"]),
        ),
        apns_enabled=bool(d["apns"]["enabled"]),
        apns_key_path=str(d["apns"]["key_path"]),
        apns_key_id=str(d["apns"]["key_id"]),
        apns_team_id=str(d["apns"]["team_id"]),
        apns_bundle_id=str(d["apns"]["bundle_id"]),
    )


def default_config() -> DroverConfig:
    return _from_dict(_DEFAULTS)


class ConfigUnreadable(RuntimeError):
    """The config path is present but could not be read.

    Distinct from ``FileNotFoundError``, which means "not configured yet".
    This means "configured, and the bytes did not arrive" -- an unmounted or
    permission-gated data volume, which is the case that used to hang.
    """


#: How long a local config read may take before we call it a failure. This is
#: a file on local storage; seconds here are already generous. #265: every
#: daemon blocked forever inside this ``open()`` when macOS stopped letting
#: launchd-spawned processes read the external volume holding ``~/.drover``.
#: They stayed alive at three file descriptors with no CPU and no log line, so
#: ``KeepAlive`` never fired and health checks read "running". Failing is what
#: makes the supervisor useful.
CONFIG_READ_TIMEOUT_SECONDS = 5.0


def read_config_text(
    path: Path, timeout_seconds: float = CONFIG_READ_TIMEOUT_SECONDS
) -> str:
    """Read ``path`` as text, or raise rather than block indefinitely.

    An ``open()`` stuck in the kernel cannot be cancelled, so the only thing
    the caller can do is stop waiting and let the process exit to be
    restarted. That makes the reader inherently abandonable, and ``_thread``
    rather than ``threading`` is the honest way to express it: the raw thread
    is never registered with ``threading``, so nothing can join it, and it
    cannot hold up interpreter shutdown. It also keeps a config read from
    depending on ``threading`` module state, which callers and tests do
    legitimately replace.

    ``exists()`` runs on that thread too. It is a ``stat``, and a ``stat`` can
    hang for the same reason an ``open()`` can, so testing it up front would
    only move the hang one line earlier. In the live case ``stat`` was
    permitted while ``open`` was not, which is exactly why the launch agent's
    own ``until [ -d ... ]`` guard passed and told us nothing.
    """

    path = Path(path)
    outcome: dict[str, Any] = {}
    finished = _thread.allocate_lock()
    finished.acquire()

    def _read() -> None:
        try:
            if not path.exists():
                raise FileNotFoundError(f"config file not found: {path}")
            outcome["text"] = path.read_text()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            outcome["error"] = exc
        finally:
            finished.release()

    _thread.start_new_thread(_read, ())

    if not finished.acquire(True, timeout_seconds):
        raise ConfigUnreadable(
            f"could not read {path} within {timeout_seconds:g}s. The volume "
            "holding it may be unmounted, or this process may lack permission "
            "to read it. A process started from a terminal can succeed where a "
            "service-managed one does not; that difference is a permission "
            "grant, not a fault in drover."
        )
    error = outcome.get("error")
    if isinstance(error, FileNotFoundError):
        raise error
    if error is not None:
        raise ConfigUnreadable(f"could not read {path}: {error}") from error
    return outcome["text"]


def load_config(path: Path) -> DroverConfig:
    """Load config from a TOML file, falling back to defaults for missing keys."""
    path = Path(path)
    loaded = tomllib.loads(read_config_text(path))
    merged = _merge(_DEFAULTS, loaded)
    return _from_dict(merged)
