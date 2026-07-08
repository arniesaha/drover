"""Drover runtime configuration.

Single source of truth: ~/.drover/config.toml.  Falls back to sensible
defaults for any missing field so a brand-new install Just Works after
`drover-server init` writes the default file.

Transition compat (Drover, formerly Nexus): for one release the loader also
resolves the legacy locations — ~/.nexus/config.toml, ~/.nexus/api_token,
and the NEXUS_API_TOKEN env var — preferring the Drover names when both
exist. Remove in the post-cutover cleanup (porting-and-cutover.md §7.6).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import os

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def _home_drover() -> Path:
    return Path(os.path.expanduser("~/.drover"))


def _home_nexus() -> Path:
    """Legacy config home (transition compat only)."""
    return Path(os.path.expanduser("~/.nexus"))


def config_home() -> Path:
    """Active config home: ~/.drover, else legacy ~/.nexus, else ~/.drover."""
    drover = _home_drover()
    if drover.exists():
        return drover
    legacy = _home_nexus()
    if legacy.exists():
        return legacy
    return drover


def default_config_file(name: str) -> Path:
    """~/.drover/<name>, falling back to legacy ~/.nexus/<name>."""
    new = _home_drover() / name
    if new.exists():
        return new
    legacy = _home_nexus() / name
    if legacy.exists():
        return legacy
    return new


def default_config_path() -> Path:
    """~/.drover/config.toml, falling back to legacy ~/.nexus/config.toml."""
    return default_config_file("config.toml")


def resolve_api_token_env() -> str:
    """DROVER_API_TOKEN, falling back to legacy NEXUS_API_TOKEN."""
    return (
        os.environ.get("DROVER_API_TOKEN", "").strip()
        or os.environ.get("NEXUS_API_TOKEN", "").strip()
    )


def default_token_file() -> Path:
    """~/.drover/api_token, falling back to legacy ~/.nexus/api_token."""
    new = _home_drover() / "api_token"
    if new.exists():
        return new
    legacy = _home_nexus() / "api_token"
    if legacy.exists():
        return legacy
    return new


def _default_duckdb_path(home: Path) -> Path:
    """drover.duckdb, honoring a pre-migration legacy nexus.duckdb if present."""
    new = home / "drover.duckdb"
    legacy = home / "nexus.duckdb"
    if not new.exists() and legacy.exists():
        return legacy
    return new


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
    summarizer_local_model: str
    summarizer_local_ollama_url: str  # empty string = disabled; no wake relay
    summarizer_gpu_relay_url: str  # empty string = disabled
    summarizer_gpu_ollama_url: str  # empty string = disabled
    summarizer_mac_ollama_url: str  # empty string = disabled
    summarizer_wake_timeout_s: float
    summarizer_batch_size: int
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
    # DROVER_API_TOKEN env > legacy NEXUS_API_TOKEN env > this field >
    # auto-generated ~/.drover/api_token (legacy ~/.nexus/api_token honored).
    auth_enabled: bool
    auth_api_token: str


_DEFAULTS = {
    "paths": {
        "incoming_dir": str(config_home() / "incoming"),
        "parquet_dir": str(config_home() / "parquet"),
        "duckdb_path": str(_default_duckdb_path(config_home())),
        "processed_retention_days": 7,
    },
    "server": {
        "otlp_grpc_port": 4317,
        "mcp_http_port": 7077,
        "metrics_http_port": 0,
    },
    "agent": {
        "agent_id": "unknown-agent",
        "principal_id": "unknown",
    },
    "summarizer": {
        "backend_policy": "hybrid",
        "api_model": "claude-haiku-4-5-20251001",
        "local_model": "qwen3.5:35b-a3b",
        "local_ollama_url": "",
        "gpu_relay_url": "",
        "gpu_ollama_url": "",
        "mac_ollama_url": "",
        "wake_timeout_s": 120.0,
        "batch_size": 8,
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
        summarizer_local_model=s["local_model"],
        summarizer_local_ollama_url=s["local_ollama_url"],
        summarizer_gpu_relay_url=s["gpu_relay_url"],
        summarizer_gpu_ollama_url=s["gpu_ollama_url"],
        summarizer_mac_ollama_url=s["mac_ollama_url"],
        summarizer_wake_timeout_s=float(s["wake_timeout_s"]),
        summarizer_batch_size=int(s["batch_size"]),
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


# Transition compat: legacy class name (remove in post-cutover cleanup).
NexusConfig = DroverConfig
