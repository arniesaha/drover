"""Drover runtime configuration.

Single source of truth: ~/.drover/config.toml.  Falls back to sensible
defaults for any missing field so a brand-new install Just Works after
`drover-server init` writes the default file.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import math
import os

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
class DroverConfig:
    incoming_dir: Path
    parquet_dir: Path
    duckdb_path: Path
    processed_retention_days: int
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
    # "Favorite" cwd suggestions surfaced in the New Session sheet, on top of
    # recent-session cwds. Empty by default — set per install, never in code.
    harness_favorite_cwds: tuple[str, ...]
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
        harness_favorite_cwds=tuple(
            str(p) for p in d["harness"]["favorite_cwds"] if str(p).strip()
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


def load_config(path: Path) -> DroverConfig:
    """Load config from a TOML file, falling back to defaults for missing keys."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("rb") as f:
        loaded = tomllib.load(f)
    merged = _merge(_DEFAULTS, loaded)
    return _from_dict(merged)
