"""Tests for src/drover/config.py."""

from pathlib import Path
import pytest
from drover.config import NexusConfig, load_config, default_config

FIXTURE = Path(__file__).parent / "fixtures" / "nexus_config.toml"


def test_load_from_path():
    cfg = load_config(FIXTURE)
    assert cfg.incoming_dir == Path("/tmp/nexus-test/incoming")
    assert cfg.parquet_dir == Path("/tmp/nexus-test/parquet")
    assert cfg.duckdb_path == Path("/tmp/nexus-test/nexus.duckdb")
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077
    assert cfg.metrics_http_port == 0
    assert cfg.agent_id == "test-agent"
    assert cfg.principal_id == "test-user"
    assert cfg.processed_retention_days == 7
    assert cfg.summarizer_backend_policy == "hybrid"
    assert cfg.summarizer_local_ollama_url == ""
    assert cfg.embeddings_api_base_url == ""
    assert cfg.embeddings_api_model == "text-embedding-3-small"
    assert cfg.embeddings_mac_ollama_url == ""
    assert cfg.embeddings_local_model == "nomic-embed-text"
    assert cfg.summarizer_gpu_relay_url == ""
    assert cfg.summarizer_gpu_ollama_url == ""
    assert cfg.summarizer_mac_ollama_url == ""


def test_default_config_uses_home_dir():
    cfg = default_config()
    assert cfg.incoming_dir.is_absolute()
    # config_home() prefers ~/.drover and falls back to legacy ~/.nexus
    assert (".drover" in str(cfg.incoming_dir)) or (".nexus" in str(cfg.incoming_dir))
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077
    assert cfg.metrics_http_port == 0
    assert cfg.summarizer_local_ollama_url == ""
    assert cfg.embeddings_api_base_url == ""


def test_loads_local_ollama_summarizer_config(tmp_path):
    cfg_file = tmp_path / "summarizer.toml"
    cfg_file.write_text(
        "[summarizer]\n"
        "backend_policy = 'local'\n"
        "local_model = 'qwen2.5:7b'\n"
        "local_ollama_url = 'http://127.0.0.1:11435'\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.summarizer_backend_policy == "local"
    assert cfg.summarizer_local_model == "qwen2.5:7b"
    assert cfg.summarizer_local_ollama_url == "http://127.0.0.1:11435"


def test_redis_shadow_defaults_off():
    cfg = default_config()
    assert cfg.redis_shadow_enabled is False
    assert cfg.redis_shadow_stream == "drover:events"
    assert cfg.redis_shadow_maxlen == 100000
    assert cfg.redis_jobs_enabled is False
    assert cfg.redis_jobs_stream_prefix == "drover:jobs"
    assert cfg.redis_jobs_group == "workers"


def test_loads_redis_shadow_config(tmp_path):
    cfg_file = tmp_path / "redis.toml"
    cfg_file.write_text(
        "[redis_shadow]\n"
        "enabled = true\n"
        "url = 'redis://example:6380/2'\n"
        "stream = 'nexus:shadow'\n"
        "maxlen = 5000\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.redis_shadow_enabled is True
    assert cfg.redis_shadow_url == "redis://example:6380/2"
    assert cfg.redis_shadow_stream == "nexus:shadow"
    assert cfg.redis_shadow_maxlen == 5000


def test_loads_metrics_port(tmp_path):
    cfg_file = tmp_path / "server.toml"
    cfg_file.write_text("[server]\nmetrics_http_port = 7080\n")
    cfg = load_config(cfg_file)
    assert cfg.metrics_http_port == 7080


def test_loads_redis_jobs_config(tmp_path):
    cfg_file = tmp_path / "redis-jobs.toml"
    cfg_file.write_text(
        "[redis_jobs]\n"
        "enabled = true\n"
        "url = 'redis://example:6380/3'\n"
        "stream_prefix = 'nexus:test:jobs'\n"
        "group = 'dogfood-workers'\n"
        "max_deliveries = 7\n"
        "visibility_timeout_ms = 12345\n"
        "maxlen = 50000\n"
        "high_water = 250\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.redis_jobs_enabled is True
    assert cfg.redis_jobs_url == "redis://example:6380/3"
    assert cfg.redis_jobs_stream_prefix == "nexus:test:jobs"
    assert cfg.redis_jobs_group == "dogfood-workers"
    assert cfg.redis_jobs_max_deliveries == 7
    assert cfg.redis_jobs_visibility_timeout_ms == 12345
    assert cfg.redis_jobs_maxlen == 50000
    assert cfg.redis_jobs_high_water == 250


def test_loads_embedding_api_config(tmp_path):
    cfg_file = tmp_path / "embedding.toml"
    cfg_file.write_text(
        "[embeddings]\n"
        "api_base_url = 'https://embeddings.example/v1'\n"
        "api_key = 'embed-key'\n"
        "api_model = 'embed-model'\n"
        "mac_ollama_url = 'http://127.0.0.1:11435'\n"
        "local_model = 'local-embed'\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.embeddings_api_base_url == "https://embeddings.example/v1"
    assert cfg.embeddings_api_key == "embed-key"
    assert cfg.embeddings_api_model == "embed-model"
    assert cfg.embeddings_mac_ollama_url == "http://127.0.0.1:11435"
    assert cfg.embeddings_local_model == "local-embed"


def test_loads_summarizer_mac_ollama_url(tmp_path):
    cfg_file = tmp_path / "summarizer.toml"
    cfg_file.write_text(
        "[summarizer]\n"
        "api_model = 'claude-haiku-4-5-20251001'\n"
        "local_model = 'qwen3.5:35b-a3b'\n"
        "gpu_relay_url = ''\n"
        "gpu_ollama_url = ''\n"
        "mac_ollama_url = 'http://127.0.0.1:11434'\n"
        "wake_timeout_s = 120\n"
        "batch_size = 5\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.summarizer_mac_ollama_url == "http://127.0.0.1:11434"


def test_missing_optional_field_uses_default(tmp_path):
    cfg_file = tmp_path / "minimal.toml"
    cfg_file.write_text("[paths]\nincoming_dir = '/tmp/x'\n")
    cfg = load_config(cfg_file)
    assert cfg.incoming_dir == Path("/tmp/x")
    # All other fields fall back to defaults
    assert cfg.otlp_grpc_port == 4317
    assert cfg.processed_retention_days == 7


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")
