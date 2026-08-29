"""Runtime construction tests for optional local Pond recall."""

from __future__ import annotations

from dataclasses import replace

import pytest
import requests

from drover.config import default_config
from drover.server import __main__ as server_main
from drover.server.archive import PondArchiveClient
from drover.server.summarizer.backends import SummarizerBackendConfig


def _runtime_config(*, enabled: bool):
    defaults = default_config()
    return replace(
        defaults,
        archive=replace(
            defaults.archive,
            enabled=enabled,
            base_url="http://127.0.0.1:8585" if enabled else "",
        ),
    )


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "enabled"])
def test_archive_client_construction_never_performs_a_network_request(
    monkeypatch, enabled: bool
) -> None:
    def fail_request(*_args, **_kwargs):
        pytest.fail("Pond must not be probed during runtime construction")

    monkeypatch.setattr(requests.Session, "request", fail_request)

    archive = server_main._archive_client_from_config(_runtime_config(enabled=enabled))

    if enabled:
        assert isinstance(archive, PondArchiveClient)
    else:
        assert archive is None


def test_runtime_mcp_factory_injects_the_exact_enabled_archive_client(
    monkeypatch,
) -> None:
    cfg = _runtime_config(enabled=True)
    archive = object()
    built_server = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        server_main, "_archive_client_from_config", lambda actual: archive
    )

    def capture_build(**kwargs):
        captured.update(kwargs)
        return built_server

    monkeypatch.setattr(server_main, "build_mcp_server", capture_build)
    backend_config = SummarizerBackendConfig()
    summarize_job_stream = object()

    result = server_main._build_runtime_mcp_server(
        cfg=cfg,
        host="127.0.0.1",
        backend_config=backend_config,
        summarize_job_stream=summarize_job_stream,
    )

    assert result is built_server
    assert captured == {
        "duckdb_path": cfg.duckdb_path,
        "host": "127.0.0.1",
        "port": cfg.mcp_http_port,
        "backend_config": backend_config,
        "summarize_job_stream": summarize_job_stream,
        "archive_config": cfg.archive,
        "archive": archive,
    }


def test_runtime_mcp_factory_injects_disabled_config_without_a_client(
    monkeypatch,
) -> None:
    cfg = _runtime_config(enabled=False)
    built_server = object()
    captured: dict[str, object] = {}

    def capture_build(**kwargs):
        captured.update(kwargs)
        return built_server

    monkeypatch.setattr(server_main, "build_mcp_server", capture_build)
    backend_config = SummarizerBackendConfig()

    result = server_main._build_runtime_mcp_server(
        cfg=cfg,
        host="127.0.0.1",
        backend_config=backend_config,
        summarize_job_stream=None,
    )

    assert result is built_server
    assert captured == {
        "duckdb_path": cfg.duckdb_path,
        "host": "127.0.0.1",
        "port": cfg.mcp_http_port,
        "backend_config": backend_config,
        "summarize_job_stream": None,
        "archive_config": cfg.archive,
        "archive": None,
    }
