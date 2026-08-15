"""Tests for src/drover/config.py."""

from pathlib import Path
import pytest
from drover.config import FavoriteCwd, load_config, default_config

FIXTURE = Path(__file__).parent / "fixtures" / "nexus_config.toml"


def test_load_from_path():
    cfg = load_config(FIXTURE)
    assert cfg.incoming_dir == Path("/tmp/nexus-test/incoming")
    assert cfg.parquet_dir == Path("/tmp/nexus-test/parquet")
    assert cfg.duckdb_path == Path("/tmp/nexus-test/nexus.duckdb")
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077
    assert cfg.metrics_http_port == 7080
    assert cfg.agent_id == "test-agent"
    assert cfg.principal_id == "test-user"
    assert cfg.processed_retention_days == 7
    assert cfg.summarizer_backend_policy == "harness"
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
    assert ".drover" in str(cfg.incoming_dir)
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077
    assert cfg.metrics_http_port == 7080
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


def test_loads_harness_summarizer_model(tmp_path):
    cfg_file = tmp_path / "summarizer.toml"
    cfg_file.write_text("[summarizer]\nharness_model = 'sonnet'\n")

    cfg = load_config(cfg_file)

    assert cfg.summarizer_harness_model == "sonnet"
    assert default_config().summarizer_harness_model == "haiku"


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


def test_harness_favorite_cwds_default_empty():
    assert default_config().harness_favorite_cwds == ()


def test_loads_harness_favorite_cwds(tmp_path):
    cfg_file = tmp_path / "harness.toml"
    cfg_file.write_text(
        "[harness]\nfavorite_cwds = ['/home/me/dev', '/home/me/projects', '  ']\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.harness_favorite_cwds == (
        FavoriteCwd("/home/me/dev", None),
        FavoriteCwd("/home/me/projects", None),
    )


# -- favourites scoped to a host (issue #187) ------------------------------
#
# A bare string keeps its existing meaning, "offer this on every host", so
# configs written before this change load unchanged. An inline table binds a
# path to the one host whose filesystem actually has it, which is what a fleet
# of differently-shaped hosts needs.


def test_loads_host_scoped_harness_favorite_cwd(tmp_path):
    cfg_file = tmp_path / "harness.toml"
    cfg_file.write_text(
        "[harness]\n" "favorite_cwds = [{path = '/home/Arnab/dev', host_id = 'nas'}]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.harness_favorite_cwds == (FavoriteCwd("/home/Arnab/dev", "nas"),)


def test_bare_and_host_scoped_favorite_cwds_mix_in_one_list(tmp_path):
    cfg_file = tmp_path / "harness.toml"
    cfg_file.write_text(
        "[harness]\n"
        "favorite_cwds = [\n"
        "  '/srv/shared',\n"
        "  {path = '/Users/arnabmac/drover', host_id = 'mac-mini'},\n"
        "]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.harness_favorite_cwds == (
        FavoriteCwd("/srv/shared", None),
        FavoriteCwd("/Users/arnabmac/drover", "mac-mini"),
    )


def test_host_scoped_favorite_cwd_with_a_blank_host_applies_everywhere(tmp_path):
    cfg_file = tmp_path / "harness.toml"
    cfg_file.write_text(
        "[harness]\nfavorite_cwds = [{path = '/srv/shared', host_id = '  '}]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.harness_favorite_cwds == (FavoriteCwd("/srv/shared", None),)


def test_favorite_cwd_entries_without_a_path_are_dropped(tmp_path):
    cfg_file = tmp_path / "harness.toml"
    cfg_file.write_text(
        "[harness]\n"
        "favorite_cwds = [{host_id = 'nas'}, {path = '  ', host_id = 'nas'}]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.harness_favorite_cwds == ()


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


def test_summarizer_launchd_overrides_default_empty():
    cfg = default_config()
    assert cfg.summarizer_local_ollama_launchd_label == ""
    assert cfg.summarizer_local_ollama_launchd_plist == ""


def test_loads_summarizer_launchd_overrides(tmp_path):
    cfg_file = tmp_path / "summarizer.toml"
    cfg_file.write_text(
        "[summarizer]\n"
        "local_ollama_launchd_label = 'com.custom.ollama'\n"
        "local_ollama_launchd_plist = '/tmp/com.custom.ollama.plist'\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.summarizer_local_ollama_launchd_label == "com.custom.ollama"
    assert cfg.summarizer_local_ollama_launchd_plist == "/tmp/com.custom.ollama.plist"


def test_content_analysis_defaults_to_disabled_and_local():
    cfg = default_config().advisory_content

    assert cfg.enabled is False
    assert cfg.backend_policy == "local"
    assert cfg.external_consent is False
    assert cfg.targets == ()
    assert cfg.allowed_roots == ()
    assert cfg.max_file_bytes == 131072
    assert cfg.max_bundle_bytes == 524288
    assert cfg.excerpt_max_chars == 320


def test_loads_explicit_local_content_analysis_configuration(tmp_path):
    cfg_file = tmp_path / "content.toml"
    cfg_file.write_text(
        "[advisory_content]\n"
        "enabled = true\n"
        "backend_policy = 'local'\n"
        "external_consent = false\n"
        "targets = ['global-agents', 'codex-skill']\n"
        "allowed_roots = ['/srv/prompts', '/srv/skills']\n"
        "max_file_bytes = 4096\n"
        "max_bundle_bytes = 8192\n"
        "excerpt_max_chars = 160\n"
    )

    cfg = load_config(cfg_file).advisory_content

    assert cfg.enabled is True
    assert cfg.backend_policy == "local"
    assert cfg.external_consent is False
    assert cfg.targets == ("global-agents", "codex-skill")
    assert cfg.allowed_roots == (Path("/srv/prompts"), Path("/srv/skills"))
    assert cfg.max_file_bytes == 4096
    assert cfg.max_bundle_bytes == 8192
    assert cfg.excerpt_max_chars == 160


def test_cloud_content_analysis_requires_separate_external_consent(tmp_path):
    cfg_file = tmp_path / "content.toml"
    cfg_file.write_text(
        "[advisory_content]\n"
        "enabled = true\n"
        "backend_policy = 'cloud'\n"
        "external_consent = false\n"
    )

    with pytest.raises(ValueError, match="external_consent"):
        load_config(cfg_file)


@pytest.mark.parametrize("field", ["enabled", "external_consent"])
def test_content_analysis_rejects_string_booleans(tmp_path, field):
    cfg_file = tmp_path / "content.toml"
    cfg_file.write_text(f"[advisory_content]\n{field} = 'false'\n")

    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)


@pytest.mark.parametrize(
    "field, value",
    [
        ("backend_policy", "'hybrid'"),
        ("max_file_bytes", "0"),
        ("max_bundle_bytes", "-1"),
        ("excerpt_max_chars", "0"),
    ],
)
def test_content_analysis_rejects_unsafe_configuration(tmp_path, field, value):
    cfg_file = tmp_path / "content.toml"
    cfg_file.write_text(f"[advisory_content]\n{field} = {value}\n")

    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)


def test_server_metrics_host_defaults_to_loopback():
    """Hardened default: binding wide open must be a deliberate act."""
    assert default_config().server_metrics_host == "127.0.0.1"


def test_server_metrics_host_is_read_from_config(tmp_path):
    """The installer writes the detected private address here.

    It must be a real config key, not decoration: --metrics-host lives only in
    a service unit's argv, and a regenerated unit that dropped the flag would
    silently revert the server to loopback. That failure is invisible until
    the app stops loading any screen.
    """
    path = tmp_path / "config.toml"
    path.write_text('[server]\nmetrics_host = "100.64.0.10"\n', encoding="utf-8")
    assert load_config(path).server_metrics_host == "100.64.0.10"
