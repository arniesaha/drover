"""Skinny CLI entry point for the Drover host daemon."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click
import duckdb

import drover
from drover.config import DroverConfig, default_config, default_config_path, load_config
from drover.schema import bootstrap_control_plane_store
from drover.server.db import duckdb_connect_lock, pin_control_plane_connection
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
        # Keep the configured host spool a valid, local DuckDB file even when
        # its inherited config names PostgreSQL for the hub.  The harness
        # registry lives in the sibling control store, but a bare ``touch``
        # would leave an invalid database for a later local lake bootstrap.
        # This only initializes DuckDB's empty catalog; it deliberately does
        # not call the lakehouse bootstrap or create lake schemas/views.
        if not cfg.duckdb_path.exists():
            cfg.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
            with duckdb_connect_lock(cfg.duckdb_path):
                if not cfg.duckdb_path.exists():
                    con = duckdb.connect(str(cfg.duckdb_path))
                    con.close()
        # harnessd owns only a control registry/spool.  Never open the
        # lakehouse here: catalog/view bootstrap scans historical data and is
        # neither needed by the daemon nor safe on its startup path.
        bootstrap_control_plane_store(cfg.duckdb_path)
        # A PostgreSQL-configured hub cannot be serving this local DuckDB
        # companion as its control plane. That proves this harnessd owns the
        # host-local spool, so keep one connection open instead of creating a
        # DuckDB instance and checkpointing it for every registry window.
        # Legacy DuckDB hubs may share this file with harnessd (Studio), and
        # therefore retain the safe connection-per-window fallback.
        pin_control_plane_connection(
            cfg.duckdb_path, enabled=cfg.control_store.backend == "postgres"
        )
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
    relay: bool = False,
) -> None:
    cfg = resolve_config(config_path)
    resolved_config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    bootstrap_harnessd_schema(cfg)
    listen_host, listen_port = parse_listen_address(listen)
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
        relay=relay,
        advisory_content=cfg.advisory_content,
        content_consent_path=resolved_config_path.with_name(
            f".{resolved_config_path.name}.harness-content-consent.json"
        ),
        cfg=cfg,
    )


@click.command(name="drover-harnessd")
@click.version_option(version=drover.__version__, prog_name="drover-harnessd")
@click.option("--config", "config_path", default=None, help="Path to config TOML")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging")
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
@click.option(
    "--relay",
    is_flag=True,
    default=False,
    help="Dial out to --central-url instead of relying on inbound reachability",
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
    relay: bool,
) -> None:
    """Run the Drover host daemon without loading full drover-server."""
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
        relay=relay,
    )


if __name__ == "__main__":
    main()
