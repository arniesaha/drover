"""Skinny CLI entry point for the Meta Harness host daemon."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click
import duckdb

from drover.config import DroverConfig, default_config, default_config_path, load_config
from drover.schema import bootstrap
from drover.server.harness.daemon import run_harnessd

log = logging.getLogger("drover.harnessd")

DEFAULT_CONFIG_PATH = default_config_path()


def resolve_config(path: str | None) -> DroverConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        return load_config(p)
    return default_config()


def bootstrap_harnessd_schema(cfg: DroverConfig) -> bool:
    try:
        bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)
        return True
    except duckdb.IOException as exc:
        message = str(exc)
        if "Could not set lock" not in message and "Conflicting lock" not in message:
            raise
        log.warning(
            "continuing harnessd startup without schema bootstrap; "
            "registry writes will be best-effort while DuckDB is locked"
        )
        return False


def parse_listen_address(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise click.BadParameter("listen must be formatted as host:port")
    host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise click.BadParameter("listen port must be an integer") from exc
    return host, port


def run_harnessd_from_options(
    *,
    config_path: str | None,
    host_id: str,
    display_name: str | None,
    kind: str,
    listen: str,
    local_url: str | None,
    tailscale_url: str | None,
    central_url: str | None,
    host_token: str | None,
) -> None:
    cfg = resolve_config(config_path)
    bootstrap_harnessd_schema(cfg)
    listen_host, listen_port = parse_listen_address(listen)
    click.echo(f"drover-harnessd {host_id} listening on {listen_host}:{listen_port}")
    run_harnessd(
        host_id=host_id,
        display_name=display_name or host_id,
        kind=kind,
        duckdb_path=cfg.duckdb_path,
        listen_host=listen_host,
        listen_port=listen_port,
        local_url=local_url,
        tailscale_url=tailscale_url,
        central_url=central_url,
        host_token=host_token,
    )


@click.command(name="drover-harnessd")
@click.option("--config", "config_path", default=None, help="Path to config TOML")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging")
@click.option("--host-id", required=True, help="Stable host id, e.g. nas")
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
        "Shared Drover API token (falls back to DROVER_API_TOKEN/NEXUS_API_TOKEN, "
        "then ~/.drover/api_token)"
    ),
)
def main(
    config_path: str | None,
    verbose: bool,
    host_id: str,
    display_name: str | None,
    kind: str,
    listen: str,
    local_url: str | None,
    tailscale_url: str | None,
    central_url: str | None,
    host_token: str | None,
) -> None:
    """Run the Meta Harness host daemon without loading full drover-server."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run_harnessd_from_options(
        config_path=config_path,
        host_id=host_id,
        display_name=display_name,
        kind=kind,
        listen=listen,
        local_url=local_url,
        tailscale_url=tailscale_url,
        central_url=central_url,
        host_token=host_token,
    )


if __name__ == "__main__":
    main()
