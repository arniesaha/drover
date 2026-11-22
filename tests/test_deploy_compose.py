"""The Compose stack is the documented one-command hub; keep its contract."""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _compose():
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text())


def test_hub_is_published_on_loopback_only():
    hub = _compose()["services"]["hub"]
    assert hub["ports"] == ["127.0.0.1:7080:7080"]


def test_postgres_has_pgvector_pinned_like_ci():
    image = _compose()["services"]["postgres"]["image"]
    ci = (DEPLOY.parent / ".github/workflows/ci.yml").read_text()
    assert image.startswith("pgvector/pgvector:") and "@sha256:" in image
    assert image in ci


def test_schema_init_gates_the_hub():
    services = _compose()["services"]
    init = services["control-store-init"]
    assert init["command"][-2:] == ["control-store", "init"]
    assert init["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert services["hub"]["depends_on"] == {
        "control-store-init": {"condition": "service_completed_successfully"}
    }
    assert init["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}


def test_secrets_come_from_the_environment_only():
    text = (DEPLOY / "compose.yaml").read_text()
    assert "${DROVER_POSTGRES_PASSWORD:?" in text
    assert "${DROVER_API_TOKEN:?" in text
    example = (DEPLOY / "compose.env.example").read_text()
    assert "DROVER_POSTGRES_PASSWORD=\n" in example
    assert "DROVER_API_TOKEN=\n" in example
    config = (DEPLOY / "compose" / "config.toml").read_text()
    assert 'api_token = ""' in config
    assert 'dsn_env = "DROVER_CONTROL_DSN"' in config


def test_compose_config_loads_with_container_paths():
    from drover.config import load_config

    cfg = load_config(DEPLOY / "compose" / "config.toml")
    assert cfg.server_metrics_host == "0.0.0.0"
    assert str(cfg.duckdb_path).startswith("/var/lib/drover/")
